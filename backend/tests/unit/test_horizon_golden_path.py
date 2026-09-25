"""INC-043 end to end in scripted mode, through the real gate chain.

restart (tier 1, autonomous) -> sustained verification fails -> reassess with
external and historical evidence -> rollback (tier 2) -> approval -> re-gate ->
verified -> resolved, memory card written, incident map attached.
"""

from __future__ import annotations

import pytest
from test_horizon_support import INCIDENT_ID, build_rig

from aegis.domain.enums import ActionState, ActionType, IncidentState
from aegis.domain.horizon import HorizonEventType, HorizonPhase
from aegis.execution.validated import ValidatedAction


@pytest.mark.asyncio
async def test_scripted_golden_path_reaches_resolved_through_approval() -> None:
    rig = build_rig()
    orch = rig.orchestrator()

    state = await orch.run(INCIDENT_ID)

    # First remedy was autonomous (tier 1) and did not hold.
    assert state.phase is HorizonPhase.AWAITING_APPROVAL
    restart = state.actions[0]
    assert restart.action_type == ActionType.RESTART_INSTANCE.value
    assert restart.outcome == "failed"
    assert restart.signature in state.excluded_actions
    assert rig.world.restarts == 1
    assert rig.world.version == "1.4.2"

    # The reassessment cited the failed verification as refuting evidence and
    # the hypotheses flipped on evidence, not on the model's say-so.
    pool = next(h for h in state.hypotheses if h.id == "h_pool")
    deploy = next(h for h in state.hypotheses if h.id == "h_deploy")
    assert deploy.confidence > pool.confidence
    assert deploy.confidence >= 0.8
    failed_card = next(c for c in state.evidence if c.tool == "verify_recovery")
    assert failed_card.claim.startswith("FAILED")
    assert failed_card.id in pool.refuting
    assert rig.known.queries, "known-issue search ran during reassessment"

    # The rollback was held for a human by POLICY (tier 2), with the payload the UI reads.
    rollback = state.actions[-1]
    assert rollback.action_type == ActionType.ROLLBACK_DEPLOYMENT.value
    assert rollback.outcome == "awaiting_approval"
    assert state.pending_action_id == rollback.action_id
    events = await rig.events()
    required = [e for e in events if e.event_type is HorizonEventType.APPROVAL_REQUIRED]
    assert len(required) == 1
    payload = required[0].payload
    assert {"approval_id", "action_id", "action_type", "target", "reason", "confidence",
            "evidence_ids", "risk_tier"} <= set(payload)
    assert payload["risk_tier"] == 2
    assert rig.actions.by_id[rollback.action_id or ""].state is ActionState.HUMAN_REQUIRED

    # A human approves; a NEW orchestrator (as a worker job would build) resumes
    # the same checkpoint and re-gates.
    rig.approvals.decide(rollback.action_id or "", "approved", by="user_oncall")
    leases_before = rig.leases.acquired
    final = await rig.orchestrator().resume_after_approval(
        INCIDENT_ID, rollback.action_id or "", approved=True
    )
    await rig.orchestrator().drain_background()

    assert final.phase is HorizonPhase.RESOLVED
    assert rig.world.version == "1.4.1"
    assert rig.leases.acquired == leases_before + 1, "the approval was re-gated, not replayed"
    executed = [v for v in rig.world.executed if isinstance(v, ValidatedAction)]
    assert [v.action_type for v in executed] == [
        ActionType.RESTART_INSTANCE, ActionType.ROLLBACK_DEPLOYMENT,
    ]
    assert executed[-1].was_human_approved
    assert final.actions[-1].outcome == "verified"

    # Memory: the strict incident-memory write accepted it, and a card exists.
    assert len(rig.memory.writes) == 1
    assert rig.memory.writes[0]["approved_by"] == "user_oncall"
    cards = await rig.store.memory_cards()
    assert cards and cards[0].successful_action == ActionType.ROLLBACK_DEPLOYMENT.value
    assert ActionType.RESTART_INSTANCE.value in cards[0].failed_actions
    assert rig.rawtree.cards

    # The incident lifecycle followed the phases through legal transitions only.
    assert rig.incidents.incident.state is IncidentState.RESOLVED
    verifications = [e.payload for e in await rig.events()
                     if e.event_type is HorizonEventType.VERIFICATION_RESULT]
    assert [v["verified"] for v in verifications] == [False, True]
    assert {v["symptom"] for v in verifications} == {"pool_exhaustion"}
    assert verifications[-1]["action_type"] == "rollback_deployment"
    assert verifications[-1]["recovery_s"] is not None and verifications[0]["recovery_s"] is None
    types = {e.event_type for e in await rig.events()}
    assert {HorizonEventType.MEMORY_CARD_WRITTEN, HorizonEventType.RESOLVED,
            HorizonEventType.APPROVAL_RESOLVED, HorizonEventType.VERIFICATION_RESULT} <= types


@pytest.mark.asyncio
async def test_incident_map_is_attached_after_resolution() -> None:
    rig = build_rig()
    orch = rig.orchestrator()
    state = await orch.run(INCIDENT_ID)
    rig.approvals.decide(state.pending_action_id or "", "approved")
    orch2 = rig.orchestrator()
    await orch2.resume_after_approval(INCIDENT_ID, state.pending_action_id or "", approved=True)
    await orch2.drain_background()
    card = (await rig.store.memory_cards())[0]
    assert card.image_status == "ready"
    assert card.image_url and card.image_url.endswith(card.id)
    maps = [e for e in await rig.events() if e.event_type is HorizonEventType.INCIDENT_MAP]
    assert maps and maps[-1].payload["status"] == "ready"


@pytest.mark.asyncio
async def test_denial_returns_to_diagnosis_with_the_action_excluded() -> None:
    rig = build_rig(max_steps=24)
    state = await rig.orchestrator().run(INCIDENT_ID)
    action_id = state.pending_action_id or ""
    rig.approvals.decide(action_id, "rejected")
    after = await rig.orchestrator().resume_after_approval(INCIDENT_ID, action_id, approved=False)
    denied = next(a for a in after.actions if a.action_id == action_id)
    assert denied.outcome == "denied"
    assert denied.signature in after.excluded_actions
    assert rig.world.version == "1.4.2", "a denied rollback never ran"
    # With both remedies excluded the scripted policy cannot act, and the hard
    # step budget ends the run in escalation rather than a spin.
    assert after.phase is HorizonPhase.ESCALATED
