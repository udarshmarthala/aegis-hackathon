"""The deterministic guard: code decides phases and confidence, never the model."""

from __future__ import annotations

from typing import Any

import pytest
from test_horizon_support import INCIDENT_ID, build_rig

from aegis.agents.horizon.memory_store import InMemoryHorizonStore
from aegis.agents.horizon.phases import (
    can_diagnose,
    can_plan,
    derive_confidence,
    guard_after_step,
    reassessment_done,
)
from aegis.agents.horizon.ports import BrainDecision, BrainRequest, ToolCall
from aegis.agents.horizon.scripted import ScriptedBrain
from aegis.agents.horizon.tools_observe import HealthSample, SustainedVerifier
from aegis.agents.horizon.tools_self_edit import UPDATE_HYPOTHESES, SelfEditor
from aegis.domain.enums import ClaimOutcome
from aegis.domain.horizon import (
    ActionAttempt,
    EvidenceCard,
    HorizonEventType,
    HorizonHypothesis,
    HorizonPhase,
    HorizonState,
    IllegalPhaseTransition,
    Source,
    assert_phase_transition,
)


def card(cid: str, *, weight: float = 0.9, origin: Source = Source.PROMETHEUS,
         tool: str = "query_metrics", claim: str = "x", step: int = 1) -> EvidenceCard:
    return EvidenceCard(id=cid, step=step, tool=tool, source=Source.RULE, origin=origin,
                        claim=claim, weight=weight)


def state(**kw: Any) -> HorizonState:
    return HorizonState(run_id="run_1", incident_id=INCIDENT_ID, service="local:default:checkout",
                        **kw)


def test_cannot_reach_diagnosing_with_two_distinct_observe_tools() -> None:
    s = state(phase=HorizonPhase.INVESTIGATING,
              observe_tools_run=["query_metrics", "instance_status"])
    assert not can_diagnose(s)[0]
    assert guard_after_step(s)[0] is HorizonPhase.INVESTIGATING
    s.observe_tools_run = [*s.observe_tools_run, "deployment_history"]
    assert guard_after_step(s)[0] is HorizonPhase.DIAGNOSING


def test_cannot_plan_with_one_hypothesis_or_low_confidence() -> None:
    cards = [card("e1"), card("e2", origin=Source.RUNTIME, weight=0.8)]
    s = state(phase=HorizonPhase.DIAGNOSING, evidence=cards)
    s.hypotheses = [HorizonHypothesis(id="h1", statement="a", supporting=["e1", "e2"],
                                      confidence=0.95)]
    assert not can_plan(s)[0]  # one hypothesis
    weak = card("e3", weight=0.2, origin=Source.RUNTIME, tool="container_logs")
    s.evidence = [weak]
    s.hypotheses = [
        HorizonHypothesis(id="h1", statement="a", supporting=["e3"]),
        HorizonHypothesis(id="h2", statement="b", supporting=["e3"]),
    ]
    for h in s.hypotheses:
        h.confidence = derive_confidence(h, s.evidence)
    assert max(h.confidence for h in s.hypotheses) < 0.6
    assert guard_after_step(s)[0] is HorizonPhase.DIAGNOSING


@pytest.mark.asyncio
async def test_model_claimed_confidence_is_ignored() -> None:
    s = state(phase=HorizonPhase.DIAGNOSING, evidence=[card("e1", weight=0.3)])
    editor = SelfEditor(InMemoryHorizonStore())
    result = await editor.apply(
        UPDATE_HYPOTHESES,
        {"hypotheses": [{"id": "h1", "statement": "pool leak", "supporting": ["e1"],
                         "refuting": [], "suggested_action": None, "confidence": 0.99}]},
        s, {c.id: c for c in s.evidence},
    )
    assert result.ok
    derived = derive_confidence(s.hypotheses[0], s.evidence)
    assert s.hypotheses[0].confidence == derived
    assert derived < 0.6


def test_refutation_lowers_derived_confidence() -> None:
    cards = [card("e1"), card("e2", origin=Source.RUNTIME, weight=0.8), card("r1")]
    h = HorizonHypothesis(id="h", statement="s", supporting=["e1", "e2"])
    before = derive_confidence(h, cards)
    h.refuting = ["r1"]
    assert derive_confidence(h, cards) < before
    # A gap card cited as support earns nothing.
    gap = card("g1", weight=0.0, claim="UNAVAILABLE: prometheus not consulted")
    assert derive_confidence(HorizonHypothesis(id="g", statement="s", supporting=["g1"]),
                             [gap]) == 0.0


def test_reassessment_requires_citing_the_failed_verification() -> None:
    failed = card("v1", tool="verify_recovery", claim="FAILED: sample 1/5 outside thresholds")
    s = state(phase=HorizonPhase.REASSESSING, evidence=[card("e1"), failed])
    s.hypotheses = [HorizonHypothesis(id="h1", statement="pool", supporting=["e1"])]
    assert not reassessment_done(s)[0]
    assert guard_after_step(s)[0] is HorizonPhase.REASSESSING
    s.hypotheses[0].refuting = ["v1"]
    assert guard_after_step(s)[0] is HorizonPhase.DIAGNOSING


def test_illegal_phase_moves_raise() -> None:
    with pytest.raises(IllegalPhaseTransition):
        assert_phase_transition(HorizonPhase.INVESTIGATING, HorizonPhase.EXECUTING)
    with pytest.raises(IllegalPhaseTransition):
        assert_phase_transition(HorizonPhase.PLANNING, HorizonPhase.RESOLVED)


class RestartForeverBrain:
    """Scripted, except the planner insists on the restart that already failed."""

    def __init__(self) -> None:
        self._inner = ScriptedBrain(component="sqlpool", version="2.3.1")

    def status(self) -> dict[str, Any]:
        return {}

    async def step(self, request: BrainRequest, state: HorizonState) -> BrainDecision:
        if state.phase is HorizonPhase.PLANNING:
            ev = next(h for h in state.hypotheses if h.id == "h_pool").supporting
            return BrainDecision(tool_calls=(ToolCall("c", "propose_remediation", {
                "action_type": "restart_instance", "target": "checkout",
                "arguments": {"to_version": None, "replica_delta": None, "cache_key": None},
                "hypothesis_id": "h_pool", "evidence_ids": ev}),), source=Source.SCRIPTED)
        return await self._inner.step(request, state)


@pytest.mark.asyncio
async def test_identical_failed_action_is_rejected_by_code() -> None:
    rig = build_rig(max_steps=14)
    final = await rig.orchestrator(RestartForeverBrain()).run(INCIDENT_ID)
    assert rig.world.restarts == 1
    rejected = [e for e in await rig.events()
                if e.event_type is HorizonEventType.SELF_EDIT_REJECTED
                and "identical action" in e.message]
    assert rejected
    assert final.phase is HorizonPhase.ESCALATED  # step budget, not a spin


@pytest.mark.asyncio
async def test_three_failed_cycles_escalate() -> None:
    rig = build_rig()
    s = state(phase=HorizonPhase.VERIFYING, step=20, remediation_cycle=3)
    s.actions = [ActionAttempt(action_id="act_x", action_type="restart_instance",
                               target="checkout-1", cycle=3, outcome="executed")]
    await rig.store.save_checkpoint(s)
    final = await rig.orchestrator().run(INCIDENT_ID, max_steps=1)
    assert final.phase is HorizonPhase.ESCALATED
    assert final.escalation_reason and "3 remediation cycles" in final.escalation_reason
    escalated = [e for e in await rig.events() if e.event_type is HorizonEventType.ESCALATED]
    assert escalated and "reason" in escalated[0].payload


@pytest.mark.asyncio
async def test_unavailable_sample_is_never_a_pass() -> None:
    class Probe:
        def __init__(self) -> None:
            self.n = 0

        async def sample(self, service: str) -> HealthSample:
            self.n += 1
            if self.n == 5:
                return HealthSample(120.0, None, 0.3)
            return HealthSample(120.0, 0.001, 0.3)

    async def nosleep(_s: float) -> None:
        return None

    result = await SustainedVerifier(Probe(), required=5, interval_s=0, sleep=nosleep).verify("c")
    assert not result.passed
    assert result.outcomes[-1] == ClaimOutcome.UNAVAILABLE.value
    assert (await SustainedVerifier(None, required=5, interval_s=0).verify("c")).passed is False


@pytest.mark.asyncio
async def test_unreadable_health_never_resolves_the_incident() -> None:
    rig = build_rig(max_steps=30, health_unavailable=True)
    state_ = await rig.orchestrator().run(INCIDENT_ID)
    if state_.phase is HorizonPhase.AWAITING_APPROVAL:
        rig.approvals.decide(state_.pending_action_id or "", "approved")
        state_ = await rig.orchestrator().resume_after_approval(
            INCIDENT_ID, state_.pending_action_id or "", approved=True)
    assert rig.world.version == "1.4.1", "the rollback really ran and really healed"
    assert state_.phase is not HorizonPhase.RESOLVED
    types = {e.event_type for e in await rig.events()}
    assert HorizonEventType.RESOLVED not in types
    assert rig.memory.writes == [], "nothing unverified reaches incident memory"
