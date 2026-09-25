"""Crash safety: a killed run resumes at the same step and phase and finishes.

``Killed`` derives from ``BaseException`` so nothing in the loop can catch it -
the closest a unit test gets to ``kill -9``: the process stops mid-step and the
only thing that survives is the last checkpoint.
"""

from __future__ import annotations

from typing import Any

import pytest
from test_horizon_support import INCIDENT_ID, build_rig

from aegis.agents.horizon.ports import BrainDecision, BrainRequest
from aegis.agents.horizon.scripted import ScriptedBrain
from aegis.domain.horizon import HorizonEventType, HorizonPhase, HorizonState


class Killed(BaseException):
    """Not an Exception: the orchestrator must not be able to swallow it."""


class CrashingBrain:
    def __init__(self, at_step: int) -> None:
        self._inner = ScriptedBrain(component="sqlpool", version="2.3.1")
        self._at = at_step

    def status(self) -> dict[str, Any]:
        return self._inner.status()

    async def step(self, request: BrainRequest, state: HorizonState) -> BrainDecision:
        if state.step == self._at:
            raise Killed
        return await self._inner.step(request, state)


@pytest.mark.asyncio
@pytest.mark.parametrize("crash_at", [2, 3, 5, 6])
async def test_killed_run_resumes_at_the_same_step_and_phase(crash_at: int) -> None:
    rig = build_rig()
    with pytest.raises(Killed):
        await rig.orchestrator(CrashingBrain(crash_at)).run(INCIDENT_ID)

    saved = await rig.store.load_latest(INCIDENT_ID)
    assert saved is not None
    assert saved.step == crash_at - 1

    # A brand-new orchestrator: nothing carried over but the store.
    resumed = await rig.orchestrator().run(INCIDENT_ID)

    events = await rig.events()
    resumed_ev = [e for e in events if e.event_type is HorizonEventType.RESUMED]
    assert resumed_ev[0].payload == {"step": saved.step, "phase": saved.phase.value}
    assert resumed.phase is HorizonPhase.AWAITING_APPROVAL
    # The autonomous restart ran exactly once across the crash.
    assert rig.world.restarts == 1
    assert resumed.actions[-1].action_type == "rollback_deployment"


@pytest.mark.asyncio
async def test_run_k_steps_then_a_new_orchestrator_finishes() -> None:
    rig = build_rig()
    first = await rig.orchestrator().run(INCIDENT_ID, max_steps=3)
    assert first.step == 3
    assert first.phase is HorizonPhase.VERIFYING  # killed between execute and verify

    second = await rig.orchestrator().run(INCIDENT_ID)
    assert second.phase is HorizonPhase.AWAITING_APPROVAL
    assert rig.world.restarts == 1, "resuming at VERIFYING never re-executes"

    rig.approvals.decide(second.pending_action_id or "", "approved")
    final = await rig.orchestrator().resume_after_approval(
        INCIDENT_ID, second.pending_action_id or "", approved=True
    )
    assert final.phase is HorizonPhase.RESOLVED


@pytest.mark.asyncio
async def test_duplicate_approval_job_is_idempotent() -> None:
    rig = build_rig()
    state = await rig.orchestrator().run(INCIDENT_ID)
    action_id = state.pending_action_id or ""
    rig.approvals.decide(action_id, "approved")
    done = await rig.orchestrator().resume_after_approval(INCIDENT_ID, action_id, approved=True)
    again = await rig.orchestrator().resume_after_approval(INCIDENT_ID, action_id, approved=True)
    assert done.phase is again.phase is HorizonPhase.RESOLVED
    assert again.step == done.step
    assert rig.world.version == "1.4.1"
    assert sum(1 for v in rig.world.executed if v.action_type.value == "rollback_deployment") == 1
