"""Nothing the brain says reaches the environment without the gate chain.

* citing an evidence id that does not exist is rejected and changes nothing;
* an unknown action type never reaches the gate;
* a tier-3 action reaches the gate and is blocked by policy - never executed;
* an approved action is re-gated in full, and only a human can release an
  action that is waiting for one.
"""

from __future__ import annotations

from typing import Any

import pytest
from test_horizon_support import INCIDENT_ID, build_rig, now

from aegis.agents.horizon.memory_store import InMemoryHorizonStore
from aegis.agents.horizon.ports import BrainDecision, BrainRequest, ToolCall
from aegis.agents.horizon.scripted import ScriptedBrain
from aegis.agents.horizon.tools_self_edit import UPDATE_HYPOTHESES, SelfEditor
from aegis.domain.enums import ActionState, ActionType, MetricDirection, Severity
from aegis.domain.horizon import (
    EvidenceCard,
    HorizonEventType,
    HorizonPhase,
    HorizonState,
    Source,
)
from aegis.domain.models import (
    ActionProposal,
    BlastRadius,
    ExpectedEffect,
    ResourceRef,
    RollbackPlan,
    VerificationPlan,
)
from aegis.execution.validated import GateRejection, ValidatedAction


@pytest.mark.asyncio
async def test_citing_an_unknown_evidence_id_is_rejected_and_state_unchanged() -> None:
    card = EvidenceCard(id="ev_real", step=1, tool="query_metrics", source=Source.RULE,
                        claim="x", weight=0.9)
    s = HorizonState(run_id="r", incident_id=INCIDENT_ID, service="local:default:checkout",
                     evidence=[card])
    before = s.model_dump()
    result = await SelfEditor(InMemoryHorizonStore()).apply(
        UPDATE_HYPOTHESES,
        {"hypotheses": [{"id": None, "statement": "invented", "supporting": ["ev_real", "ev_fake"],
                         "refuting": [], "suggested_action": None}]},
        s, {card.id: card},
    )
    assert not result.ok
    assert result.event_type is HorizonEventType.SELF_EDIT_REJECTED
    assert result.payload["unknown"] == ["ev_fake"]
    assert s.model_dump() == before


class PlannerBrain:
    """Scripted until PLANNING, then proposes whatever the test asks for."""

    def __init__(self, proposals: list[dict[str, Any]]) -> None:
        self._inner = ScriptedBrain(component="sqlpool", version="2.3.1")
        self._proposals = proposals

    def status(self) -> dict[str, Any]:
        return {}

    async def step(self, request: BrainRequest, state: HorizonState) -> BrainDecision:
        if state.phase is HorizonPhase.PLANNING and self._proposals:
            hyp = state.top_hypothesis()
            assert hyp is not None
            spec = self._proposals.pop(0)
            return BrainDecision(tool_calls=(ToolCall("c", "propose_remediation", {
                "target": "checkout",
                "arguments": {"to_version": None, "replica_delta": None, "cache_key": None},
                "hypothesis_id": hyp.id, "evidence_ids": spec.pop("evidence_ids", hyp.supporting),
                **spec}),), source=Source.SCRIPTED)
        if state.phase is HorizonPhase.PLANNING:
            return BrainDecision(tool_calls=(), source=Source.SCRIPTED)
        return await self._inner.step(request, state)


@pytest.mark.asyncio
async def test_unknown_action_type_and_tier_three_never_execute() -> None:
    rig = build_rig(max_steps=8)
    brain = PlannerBrain([
        {"action_type": "reboot_the_datacentre"},
        {"action_type": ActionType.DELETE_DATA.value},
        {"action_type": ActionType.RESTART_INSTANCE.value, "evidence_ids": ["ev_invented"]},
    ])
    await rig.orchestrator(brain).run(INCIDENT_ID)
    assert rig.world.executed == []
    events = await rig.events()
    rejections = [e.message for e in events if e.event_type is HorizonEventType.SELF_EDIT_REJECTED]
    assert any("unknown action type" in m for m in rejections)
    assert any("existing, non-gap card ids" in m for m in rejections)
    # delete_data went through the real gate and policy blocked it.
    rows = list(rig.actions.by_id.values())
    assert [r.action_type for r in rows] == [ActionType.DELETE_DATA]
    assert rows[0].state is ActionState.BLOCKED
    assert rig.leases.acquired == 0


def _proposal(action_type: ActionType, evidence: list[str]) -> ActionProposal:
    return ActionProposal(
        id="act_regate", incident_id=INCIDENT_ID, action_type=action_type,
        target=ResourceRef(resource_type="service", resource_id="local:default:checkout",
                           environment="local", service_id="local:default:checkout"),
        reason="x", arguments={"to_version": "1.4.1"}, supporting_evidence=evidence,
        expected_effect=ExpectedEffect(metric="error_rate", direction=MetricDirection.DECREASE,
                                       threshold=0.02),
        blast_radius=BlastRadius(directly_affected=["local:default:checkout"]),
        rollback=RollbackPlan(strategy="compensating_action", description="roll forward"),
        verification=VerificationPlan(target_metric="error_rate",
                                      direction=MetricDirection.DECREASE, threshold=0.02),
        idempotency_key="hz:regate:rollback-0001", proposed_at=now(),
    )


async def _evidence(rig: Any) -> list[str]:
    from aegis.domain.enums import EvidenceType, SourceType

    ids = []
    for st in (SourceType.METRICS, SourceType.RUNTIME, SourceType.DEPLOYMENT,
               SourceType.METRICS, SourceType.RUNTIME):
        item = await rig.evidence.record(incident_id=INCIDENT_ID, source="t", source_type=st,
                                         evidence_type=EvidenceType.METRIC_SERIES, summary="s")
        ids.append(item.id)
    return ids


async def _validate(rig: Any, p: ActionProposal) -> ValidatedAction | GateRejection:
    return await rig.gate.validate(p, severity=Severity.P2, diagnosis_confidence=0.95,
                                   has_abstained_diagnosis=False)


@pytest.mark.asyncio
async def test_parked_action_is_regated_and_needs_a_live_human_approval() -> None:
    rig = build_rig()
    p = _proposal(ActionType.ROLLBACK_DEPLOYMENT, await _evidence(rig))
    first = await _validate(rig, p)
    assert isinstance(first, GateRejection) and first.needs_approval

    # Re-gating without a decision stays parked: same approval, no lease.
    again = await _validate(rig, p)
    assert isinstance(again, GateRejection) and again.needs_approval
    assert again.approval_id == first.approval_id
    assert rig.leases.acquired == 0

    rig.approvals.decide(first.action_id, "approved")
    approved = await _validate(rig, p)
    assert isinstance(approved, ValidatedAction)
    assert approved.was_human_approved
    assert rig.actions.by_id[first.action_id].state is ActionState.APPROVED

    # Once it has progressed, a further re-gate is refused, never re-minted.
    later = await _validate(rig, p)
    assert isinstance(later, GateRejection) and later.matched_rule == "already_in_progress"


@pytest.mark.asyncio
async def test_a_parked_action_is_not_released_autonomously_when_policy_relaxes() -> None:
    """Tier 1 parked for a human (low confidence) must still wait for that human."""
    rig = build_rig()
    p = _proposal(ActionType.RESTART_INSTANCE, await _evidence(rig))
    parked = await rig.gate.validate(p, severity=Severity.P2, diagnosis_confidence=0.5,
                                     has_abstained_diagnosis=False)
    assert isinstance(parked, GateRejection) and parked.needs_approval
    # Confidence now clears the floor, so policy alone would ALLOW...
    relaxed = await _validate(rig, p)
    # ...but an operator is already looking at this exact action.
    assert isinstance(relaxed, GateRejection) and relaxed.needs_approval
    assert rig.leases.acquired == 0
