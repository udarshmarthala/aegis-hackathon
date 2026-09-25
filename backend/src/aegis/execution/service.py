"""The remediation execution service.

Owns the second half of the chain the gate started:

    ... -> lease -> EXECUTE -> VERIFY -> COMMIT or ROLLBACK

and guarantees four things regardless of how any individual step fails:

* **The lease is always released.** It runs in a ``finally``. A worker that
  crashes mid-flight still loses its lease at expiry, but the normal path never
  relies on that.
* **Authorisation is re-checked immediately before the write.** Validation and
  execution are separated by real time - an approval or a lease can lapse in
  between - so the permission is confirmed once more at the last possible
  moment rather than trusted from when it was granted.
* **Verification failure triggers rollback, and a failed rollback escalates
  loudly.** A remediation that cannot be undone and did not work is the worst
  state the system can be in; it is recorded explicitly rather than swallowed.
* **Nothing is retried automatically unless the action profile says it is
  idempotent.** Replaying a non-idempotent write is how one incident becomes
  two.

The service takes a ``ValidatedAction``. There is no entry point that accepts a
proposal, so the gate chain cannot be skipped by calling a different method.

This module also owns ``RecordingSandboxRunner``, which is the only sandbox
runner the composition root hands out. Recording is done by wrapping rather than
by asking every caller to remember, because "the caller that forgot" is exactly
how an execution ends up with no audit trail.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final

from aegis.core.clock import SYSTEM_CLOCK, Clock
from aegis.core.config import Settings
from aegis.core.errors import AegisError, AuthorizationError, LeaseConflict
from aegis.core.logging import get_logger
from aegis.domain.enums import ActionState, ActionType, VerificationVerdict
from aegis.domain.models import VerificationPlan
from aegis.execution.executors import ExecutionOutcome, ExecutionPorts
from aegis.execution.registry import executor_for
from aegis.execution.sandbox import SandboxResult, SandboxRunner, SandboxSpec
from aegis.execution.validated import ValidatedAction
from aegis.persistence.actions import ActionRepository
from aegis.persistence.audit import AuditEvent, AuditLog
from aegis.persistence.patches import (
    DeploymentRepository,
    DeploymentState,
    SandboxRunRepository,
)
from aegis.policy.tiers import profile_for
from aegis.verification.engine import VerificationEngine, VerificationRun, claims_from_plan
from aegis.verification.store import VerificationStore

log = get_logger(__name__)

# Action types that change what is running for a service, and are therefore
# deployments in the sense the ``/deployments`` page and
# ``RuntimePortBridge.deployment_history`` mean. A restart, a health probe or a
# cache eviction changes no version and no replica count, so recording one as a
# deployment attempt would pad the history with entries nothing can roll back to.
DEPLOYMENT_ACTIONS: Final[frozenset[ActionType]] = frozenset(
    {
        ActionType.ROLLBACK_DEPLOYMENT,
        ActionType.SCALE_SERVICE,
        ActionType.SCALE_UP_BOUNDED,
        ActionType.UPDATE_CONFIG,
        ActionType.PROMOTE_PATCH,
    }
)

# How the change reaches the workload. Named from the action rather than
# defaulted to "rolling": claiming a rolling deployment for an in-place replica
# change would be a fact nobody observed.
DEPLOYMENT_STRATEGIES: Final[dict[ActionType, str]] = {
    ActionType.ROLLBACK_DEPLOYMENT: "rollback",
    ActionType.SCALE_SERVICE: "scale",
    ActionType.SCALE_UP_BOUNDED: "scale",
    ActionType.UPDATE_CONFIG: "config_change",
    ActionType.PROMOTE_PATCH: "patch_promotion",
}


class RecordingSandboxRunner(SandboxRunner):
    """A ``SandboxRunner`` that persists every run it completes.

    Subclassed rather than composed so it is substitutable everywhere a
    ``SandboxRunner`` is expected - the MCP tool boundary included - and no
    caller can end up holding the unrecorded runner by accident. Recording by
    wrapping is deliberate: "the caller that forgot" is how an execution ends up
    with no audit trail.

    Failures, timeouts and kills are all recorded, because a remediation that
    was tested and failed matters to the audit trail as much as one that passed.
    A sandbox that could not start at all raises instead, from the runner - no
    container ran, so there is no run to record, and the caller reports that as
    an unavailable source rather than as a failed test.

    A persistence failure propagates. Postgres is the system of record, and a
    sandbox execution that could not be written did not happen as far as the
    audit trail is concerned; returning it regardless would let an unrecorded
    run decide a patch's state. The container is already removed by then, so
    nothing is left running behind the error.
    """

    __slots__ = ("_runs",)

    def __init__(
        self,
        settings: Settings,
        runs: SandboxRunRepository,
        *,
        clock: Clock = SYSTEM_CLOCK,
    ) -> None:
        super().__init__(settings, clock=clock)
        self._runs = runs

    async def run(self, spec: SandboxSpec) -> SandboxResult:
        result = await super().run(spec)
        await self._runs.record(
            result,
            incident_id=spec.incident_id,
            action_id=spec.action_id,
            purpose=spec.purpose,
        )
        return result


@dataclass(frozen=True, slots=True)
class ExecutionReport:
    """The complete outcome of one remediation attempt.

    Deliberately not a boolean. "It ran but we could not verify it" and "it ran
    and we proved it worked" are different operational states and the caller
    must be able to tell them apart.
    """

    action_id: str
    incident_id: str
    executed: bool
    outcome: ExecutionOutcome | None
    verification: VerificationRun | None
    rolled_back: bool
    rollback_outcome: ExecutionOutcome | None
    final_state: ActionState
    escalated: bool
    escalation_reason: str
    started_at: datetime
    finished_at: datetime

    @property
    def succeeded(self) -> bool:
        """Executed, and verification positively confirmed recovery.

        An unverifiable success is not a success here. Treating it as one is how
        an unproven change acquires a green badge.
        """
        return (
            self.executed
            and self.verification is not None
            and self.verification.verdict.is_success
        )

    def as_json(self) -> dict[str, Any]:
        return {
            "action_id": self.action_id,
            "incident_id": self.incident_id,
            "executed": self.executed,
            "succeeded": self.succeeded,
            "final_state": self.final_state.value,
            "performed": self.outcome.performed if self.outcome else None,
            "changed": self.outcome.changed if self.outcome else None,
            "error": self.outcome.error if self.outcome else None,
            "verdict": self.verification.verdict.value if self.verification else None,
            "verification_id": self.verification.id if self.verification else None,
            "rolled_back": self.rolled_back,
            "rollback_error": (
                self.rollback_outcome.error if self.rollback_outcome else None
            ),
            "escalated": self.escalated,
            "escalation_reason": self.escalation_reason,
            "started_at": self.started_at.isoformat(),
            "finished_at": self.finished_at.isoformat(),
        }


class ExecutionService:
    """Executes validated actions and proves - or disproves - that they worked."""

    __slots__ = (
        "_actions", "_audit", "_clock", "_deployments", "_leases", "_verification",
        "_verification_store",
    )

    def __init__(
        self,
        *,
        actions: ActionRepository,
        audit: AuditLog,
        leases: Any,
        verification: VerificationEngine,
        verification_store: VerificationStore,
        deployments: DeploymentRepository | None = None,
        clock: Clock = SYSTEM_CLOCK,
    ) -> None:
        self._actions = actions
        self._audit = audit
        self._leases = leases
        self._verification = verification
        self._verification_store = verification_store
        # Optional so a process without the repository still executes. Its
        # absence costs the deployment record, never the execution itself.
        self._deployments = deployments
        self._clock = clock

    async def execute(
        self,
        validated: ValidatedAction,
        ports: ExecutionPorts,
        *,
        settle_seconds: float = 30.0,
        observation_window_s: int | None = None,
    ) -> ExecutionReport:
        """Run one validated action end to end.

        ``settle_seconds`` is the pause between acting and measuring. Without
        it, a restarted service is sampled mid-recovery and a working
        remediation reports as a failure.
        """
        started_at = self._clock.now()
        action_id = validated.action.id
        incident_id = validated.action.incident_id
        outcome: ExecutionOutcome | None = None
        verification: VerificationRun | None = None
        rollback_outcome: ExecutionOutcome | None = None
        rolled_back = False
        escalated = False
        escalation_reason = ""
        executed = False
        attempt_id: str | None = None

        # Last-moment authorisation check. Validation happened earlier; a lease
        # or an approval may have lapsed in the interval.
        if not validated.still_valid(self._clock.now()):
            await self._release(validated)
            raise AuthorizationError(
                "authorisation lapsed between validation and execution",
                context={
                    "action_id": action_id,
                    "lease_expires_at": validated.lease.expires_at.isoformat(),
                    "approval_id": (
                        validated.approval.id if validated.approval else None
                    ),
                },
            )

        plan = self._plan_for(validated)
        claims = claims_from_plan(
            plan,
            resource_id=validated.target.resource_id,
            service_id=validated.target.service_id,
        )

        try:
            # Baseline BEFORE the write. Measuring afterwards and comparing
            # against a remembered number would absorb whatever the action did.
            baseline = await self._verification.capture_baseline(
                claims, window_seconds=plan.observation_window_s
            )

            await self._actions.transition(
                action_id, to=ActionState.EXECUTING, expected=ActionState.APPROVED
            )
            await self._audit.record(
                event_type=AuditEvent.ACTION_EXECUTION_STARTED,
                actor="system:execution_service",
                actor_type="system",
                incident_id=incident_id,
                resource_type=validated.target.resource_type,
                resource_id=validated.target.resource_id,
                detail={
                    "action_id": action_id,
                    "action_type": validated.action_type.value,
                    "autonomous": not validated.was_human_approved,
                    "approved_by": (
                        validated.approval.decided_by if validated.approval else None
                    ),
                },
                correlation_id=validated.correlation_id,
            )

            # The executor is resolved first so a type with none registered -
            # PROMOTE_PATCH, every tier-3 action - fails before a deployment row
            # exists. An attempt recorded for something that was never dispatched
            # would read as an observed deployment failure rather than as the
            # wiring gap it is.
            executor = executor_for(validated.action_type)

            # Opened before the write, not after it. A worker killed mid-change
            # then leaves an IN_PROGRESS row an operator can see, rather than no
            # trace of a change that is now live.
            attempt_id = await self._start_deployment(validated)

            outcome = await executor.execute(validated, ports)
            executed = True

            if not outcome.ok:
                await self._fail(validated, outcome, escalate="execution reported failure")
                await self._finish_deployment(
                    attempt_id,
                    DeploymentState.FAILED,
                    outcome=outcome,
                    error=outcome.error or "execution reported failure",
                )
                finished_at = self._clock.now()
                return ExecutionReport(
                    action_id=action_id,
                    incident_id=incident_id,
                    executed=True,
                    outcome=outcome,
                    verification=None,
                    rolled_back=False,
                    rollback_outcome=None,
                    final_state=ActionState.FAILED,
                    escalated=True,
                    escalation_reason=outcome.error or "execution reported failure",
                    started_at=started_at,
                    finished_at=finished_at,
                )

            await self._audit.record(
                event_type=AuditEvent.ACTION_EXECUTION_SUCCEEDED,
                actor="system:execution_service",
                actor_type="system",
                incident_id=incident_id,
                resource_type=validated.target.resource_type,
                resource_id=validated.target.resource_id,
                detail={
                    "action_id": action_id,
                    "performed": outcome.performed,
                    "changed": outcome.changed,
                    **outcome.detail,
                },
                correlation_id=validated.correlation_id,
            )

            # ---- verify --------------------------------------------------- #
            await self._actions.transition(
                action_id, to=ActionState.VERIFYING, expected=ActionState.EXECUTING
            )
            await self._audit.record(
                event_type=AuditEvent.VERIFICATION_STARTED,
                actor="system:verification_engine",
                actor_type="system",
                incident_id=incident_id,
                resource_type="action",
                resource_id=action_id,
                detail={"claims": len(claims)},
                correlation_id=validated.correlation_id,
            )
            verification = await self._verification.verify(
                incident_id=incident_id,
                claims=claims,
                baseline=baseline,
                action_id=action_id,
                observation_window_s=observation_window_s or plan.observation_window_s,
                settle_seconds=settle_seconds,
            )
            await self._verification_store.save(verification)
            await self._audit.record(
                event_type=(
                    AuditEvent.VERIFICATION_REGRESSION
                    if verification.verdict is VerificationVerdict.REGRESSION_DETECTED
                    else AuditEvent.VERIFICATION_COMPLETED
                ),
                actor="system:verification_engine",
                actor_type="system",
                incident_id=incident_id,
                resource_type="action",
                resource_id=action_id,
                detail={
                    "verification_id": verification.id,
                    "verdict": verification.verdict.value,
                    "summary": verification.notes,
                    "unavailable_claims": len(verification.unavailable),
                },
                correlation_id=validated.correlation_id,
            )

            # ---- commit or roll back -------------------------------------- #
            if verification.verdict.is_success:
                await self._actions.transition(
                    action_id,
                    to=ActionState.SUCCESS,
                    expected=ActionState.VERIFYING,
                    result=verification.as_json(),
                    mark_completed=True,
                )
                final_state = ActionState.SUCCESS
                await self._finish_deployment(
                    attempt_id,
                    DeploymentState.VERIFIED,
                    outcome=outcome,
                    verification_id=verification.id,
                )
            elif verification.verdict.requires_rollback:
                rolled_back, rollback_outcome, escalated, escalation_reason = (
                    await self._rollback(validated, ports, outcome, verification)
                )
                final_state = (
                    ActionState.ROLLED_BACK if rolled_back else ActionState.FAILED
                )
                await self._finish_deployment(
                    attempt_id,
                    (
                        DeploymentState.ROLLED_BACK
                        if rolled_back
                        else DeploymentState.FAILED
                    ),
                    outcome=outcome,
                    verification_id=verification.id,
                    error=escalation_reason or f"verification {verification.verdict.value}",
                )
            else:
                # PARTIALLY_VERIFIED or INCONCLUSIVE. The change stands, because
                # rolling back on incomplete information could itself cause an
                # outage - but it is escalated, never quietly marked successful.
                await self._actions.transition(
                    action_id,
                    to=ActionState.FAILED,
                    expected=ActionState.VERIFYING,
                    result=verification.as_json(),
                    error=f"verification {verification.verdict.value}",
                    mark_completed=True,
                )
                final_state = ActionState.FAILED
                escalated = True
                escalation_reason = (
                    f"verification was {verification.verdict.value}: "
                    f"{verification.notes}. The change was left in place; a human "
                    "must confirm or revert it."
                )
                # DEPLOYED, not VERIFIED: the change is live and unproven. The
                # two must stay distinguishable on the deployments page, because
                # collapsing them is how an unproven change acquires a green
                # badge.
                await self._finish_deployment(
                    attempt_id,
                    DeploymentState.DEPLOYED,
                    outcome=outcome,
                    verification_id=verification.id,
                    error=f"verification {verification.verdict.value}",
                )
                log.warning(
                    "remediation could not be verified",
                    action_id=action_id,
                    incident_id=incident_id,
                    verdict=verification.verdict.value,
                    unavailable=len(verification.unavailable),
                )

        except LeaseConflict:
            raise
        except AegisError as exc:
            await self._fail(validated, outcome, escalate=str(exc))
            await self._finish_deployment(
                attempt_id, DeploymentState.FAILED, outcome=outcome, error=str(exc)
            )
            final_state = ActionState.FAILED
            escalated = True
            escalation_reason = str(exc)
            log.error(
                "remediation failed",
                action_id=action_id,
                incident_id=incident_id,
                error=str(exc),
                code=exc.code,
            )
        finally:
            await self._release(validated)

        finished_at = self._clock.now()
        return ExecutionReport(
            action_id=action_id,
            incident_id=incident_id,
            executed=executed,
            outcome=outcome,
            verification=verification,
            rolled_back=rolled_back,
            rollback_outcome=rollback_outcome,
            final_state=final_state,
            escalated=escalated,
            escalation_reason=escalation_reason,
            started_at=started_at,
            finished_at=finished_at,
        )

    # ---- helpers ---------------------------------------------------------- #

    async def _start_deployment(self, validated: ValidatedAction) -> str | None:
        """Open a deployment attempt for actions that change what is running.

        Returns ``None`` when there is nothing to record or the row could not be
        written. Bookkeeping never blocks a gated write: an attempt row that was
        never opened costs a line on a page, whereas refusing to execute an
        already-authorised action because a secondary insert failed would be a
        far worse failure mode.
        """
        if self._deployments is None or validated.action_type not in DEPLOYMENT_ACTIONS:
            return None
        service_id = validated.target.service_id or validated.target.resource_id
        if not service_id:
            return None
        try:
            attempt = await self._deployments.start(
                environment=validated.target.environment,
                service_id=service_id,
                incident_id=validated.action.incident_id,
                action_id=validated.action.id,
                strategy=DEPLOYMENT_STRATEGIES.get(validated.action_type, "rolling"),
                state=DeploymentState.IN_PROGRESS,
                detail={
                    "action_type": validated.action_type.value,
                    "autonomous": not validated.was_human_approved,
                    "correlation_id": validated.correlation_id,
                },
            )
        except Exception as exc:  # noqa: BLE001 - never block an authorised write
            log.error(
                "deployment attempt not opened",
                action_id=validated.action.id,
                service_id=service_id,
                error=str(exc),
            )
            return None
        return attempt.id

    async def _finish_deployment(
        self,
        attempt_id: str | None,
        state: DeploymentState,
        *,
        outcome: ExecutionOutcome | None = None,
        verification_id: str | None = None,
        error: str | None = None,
    ) -> None:
        """Close an attempt with what was observed, never with what was asked for.

        Versions come from the executor's outcome detail, which the executors
        populate from the runtime's own view. A proposal's ``to_version`` is a
        request; writing it here would record an intention as a fact.

        Never raises: the execution report is the caller's answer and must
        survive a failed bookkeeping write. An attempt left IN_PROGRESS is
        visible and wrong-looking, which is the honest outcome.
        """
        if attempt_id is None or self._deployments is None:
            return
        detail = dict(outcome.detail) if outcome else {}
        try:
            await self._deployments.finish(
                attempt_id,
                state=state,
                verification_id=verification_id,
                error=error,
                from_version=_version(detail.get("from_version")),
                to_version=_version(detail.get("to_version")),
                detail={
                    **detail,
                    "performed": outcome.performed if outcome else None,
                    "changed": outcome.changed if outcome else None,
                },
            )
        except Exception as exc:  # noqa: BLE001 - bookkeeping, not control flow
            log.error(
                "deployment attempt not closed",
                deployment_id=attempt_id,
                state=state.value,
                error=str(exc),
            )

    @staticmethod
    def _plan_for(validated: ValidatedAction) -> VerificationPlan:
        """The verification plan the action was proposed with.

        Read from the proposal rather than reconstructed, so the success
        criteria cannot drift between what policy assessed and what is measured.
        """
        return validated.proposal.verification

    async def _rollback(
        self,
        validated: ValidatedAction,
        ports: ExecutionPorts,
        outcome: ExecutionOutcome | None,
        verification: VerificationRun,
    ) -> tuple[bool, ExecutionOutcome | None, bool, str]:
        """Undo a change that failed verification.

        A rollback that itself fails is the most dangerous state in the system:
        the environment is changed, the change did not work, and it cannot be
        reversed automatically. It is recorded at ERROR with its own audit event
        and always escalates.
        """
        action_id = validated.action.id
        incident_id = validated.action.incident_id
        log.warning(
            "verification failed; rolling back",
            action_id=action_id,
            verdict=verification.verdict.value,
            regressions=[r.claim.id for r in verification.regressions],
        )

        if outcome is None:
            return False, None, True, "nothing was executed, so nothing was rolled back"

        try:
            executor = executor_for(validated.action_type)
            rollback_outcome = await executor.rollback(validated, ports, outcome)
        except AegisError as exc:
            await self._actions.transition(
                action_id,
                to=ActionState.FAILED,
                error=f"rollback failed: {exc}",
                result=verification.as_json(),
                mark_completed=True,
            )
            await self._audit.record(
                event_type=AuditEvent.ACTION_ROLLBACK_FAILED,
                actor="system:execution_service",
                actor_type="system",
                incident_id=incident_id,
                resource_type="action",
                resource_id=action_id,
                detail={"error": str(exc), "verdict": verification.verdict.value},
                correlation_id=validated.correlation_id,
            )
            log.error(
                "ROLLBACK FAILED - environment is changed and unreverted",
                action_id=action_id,
                incident_id=incident_id,
                resource_id=validated.target.resource_id,
                error=str(exc),
            )
            return (
                False,
                None,
                True,
                f"rollback failed ({exc}); the environment is changed and "
                "requires manual intervention",
            )

        if not rollback_outcome.ok:
            await self._actions.transition(
                action_id,
                to=ActionState.FAILED,
                error=f"rollback reported failure: {rollback_outcome.error}",
                result=verification.as_json(),
                mark_completed=True,
            )
            await self._audit.record(
                event_type=AuditEvent.ACTION_ROLLBACK_FAILED,
                actor="system:execution_service",
                actor_type="system",
                incident_id=incident_id,
                resource_type="action",
                resource_id=action_id,
                detail={"error": rollback_outcome.error},
                correlation_id=validated.correlation_id,
            )
            log.error(
                "ROLLBACK REPORTED FAILURE - manual intervention required",
                action_id=action_id,
                incident_id=incident_id,
                error=rollback_outcome.error,
            )
            return (
                False,
                rollback_outcome,
                True,
                f"rollback did not succeed: {rollback_outcome.error}",
            )

        await self._actions.transition(
            action_id,
            to=ActionState.ROLLED_BACK,
            result={
                "verification": verification.as_json(),
                "rollback": {
                    "performed": rollback_outcome.performed,
                    "changed": rollback_outcome.changed,
                    "detail": rollback_outcome.detail,
                },
            },
            error=f"verification {verification.verdict.value}",
            mark_completed=True,
        )
        await self._audit.record(
            event_type=AuditEvent.ACTION_ROLLED_BACK,
            actor="system:execution_service",
            actor_type="system",
            incident_id=incident_id,
            resource_type="action",
            resource_id=action_id,
            detail={
                "performed": rollback_outcome.performed,
                "verdict": verification.verdict.value,
            },
            correlation_id=validated.correlation_id,
        )
        log.info("rolled back", action_id=action_id, incident_id=incident_id)
        regression = verification.verdict is VerificationVerdict.REGRESSION_DETECTED
        return (
            True,
            rollback_outcome,
            regression,
            "a regression was detected and reverted" if regression else "",
        )

    async def _fail(
        self,
        validated: ValidatedAction,
        outcome: ExecutionOutcome | None,
        *,
        escalate: str,
    ) -> None:
        """Mark an action failed and audit why. Never raises."""
        try:
            await self._actions.transition(
                validated.action.id,
                to=ActionState.FAILED,
                error=escalate[:2000],
                result={"performed": outcome.performed} if outcome else None,
                mark_completed=True,
            )
        except AegisError as exc:
            log.error(
                "could not record action failure",
                action_id=validated.action.id,
                error=str(exc),
            )
        await self._audit.record(
            event_type=AuditEvent.ACTION_EXECUTION_FAILED,
            actor="system:execution_service",
            actor_type="system",
            incident_id=validated.action.incident_id,
            resource_type="action",
            resource_id=validated.action.id,
            detail={"error": escalate[:1000]},
            correlation_id=validated.correlation_id,
        )

    async def _release(self, validated: ValidatedAction) -> None:
        """Release the lease. Runs in a finally, so it must never raise."""
        try:
            await self._leases.release(
                validated.lease, correlation_id=validated.correlation_id
            )
        except Exception as exc:  # noqa: BLE001 - finally-path cleanup
            log.error(
                "lease release raised; it will be reaped at expiry",
                lease_id=validated.lease.id,
                error=str(exc),
            )

    @staticmethod
    def is_retryable(validated: ValidatedAction) -> bool:
        """Whether a failed attempt may be retried automatically.

        Driven by the static action profile, never by the error text. A
        non-idempotent action is never auto-retried, because replaying it is how
        one incident becomes two (CLAUDE.md 4).
        """
        return profile_for(validated.action_type).idempotent


def _version(value: Any) -> str | None:
    """Render an observed version for the deployment row, or nothing at all.

    Only actions that move a service between versions report one; a scale
    records replica counts instead, and those stay in ``detail`` rather than
    being dressed up as versions. ``None`` stays ``None`` rather than becoming
    the string "None", which would read as a real version in the UI and would
    make a scale look like a rollback target.
    """
    if value is None:
        return None
    return str(value)[:200]


__all__ = [
    "DEPLOYMENT_ACTIONS",
    "ExecutionReport",
    "ExecutionService",
    "RecordingSandboxRunner",
]
