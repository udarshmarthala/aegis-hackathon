"""Execution, verification and the rollback paths.

The happy path is the least interesting thing here. These tests exist for the
cases that decide whether an autonomous system is safe to run unattended: what
happens when verification fails, when rollback itself fails, when authorisation
lapsed while we were waiting, and when telemetry went dark.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

import pytest
from tests.unit.test_execution_gate import (
    NOW,
    FakeAudit,
    FakeLeases,
    FixedClock,
    build_gate,
    proposal,
    run_gate,
    stored,
)

from aegis.core.errors import AuthorizationError, ExternalServiceError
from aegis.domain.enums import (
    ActionState,
    ActionType,
    ClaimOutcome,
    ServiceHealth,
    VerificationTestKind,
    VerificationVerdict,
)
from aegis.execution.executors import ExecutionPorts
from aegis.execution.service import ExecutionService
from aegis.execution.validated import ValidatedAction
from aegis.verification.claims import ClaimResult, ClaimTest, VerificationClaim
from aegis.verification.engine import Baseline, VerificationRun


@dataclass
class FakeRuntime:
    """Both halves of the runtime port, with scripted outcomes."""

    available: bool = True
    restart_ok: bool = True
    scale_calls: list[int] = field(default_factory=list)
    restart_calls: list[str] = field(default_factory=list)
    raise_on_rollback: bool = False

    async def restart_instance(self, instance_id: str, **_kw: Any) -> Any:
        self.restart_calls.append(instance_id)
        from aegis.execution.ports import OperationResult

        return OperationResult(
            ok=self.restart_ok,
            performed=f"restart {instance_id}",
            error=None if self.restart_ok else "docker refused",
        )

    async def scale(self, service_id: str, *, replicas: int, **_kw: Any) -> Any:
        from aegis.execution.ports import OperationResult

        if self.raise_on_rollback and self.scale_calls:
            raise ExternalServiceError("daemon unreachable", retryable=False)
        self.scale_calls.append(replicas)
        return OperationResult(ok=True, performed=f"scale {service_id}={replicas}")

    async def drain_instance(self, instance_id: str, **_kw: Any) -> Any:
        from aegis.execution.ports import OperationResult

        return OperationResult(ok=True, performed=f"drain {instance_id}")

    async def rollback_deployment(self, service_id: str, **_kw: Any) -> Any:
        from aegis.execution.ports import OperationResult

        return OperationResult(ok=True, performed=f"rollback {service_id}")

    async def update_config(self, service_id: str, **_kw: Any) -> Any:
        from aegis.execution.ports import OperationResult

        return OperationResult(ok=True, performed=f"config {service_id}")

    async def health(self, _service_id: str) -> ServiceHealth:
        return ServiceHealth.HEALTHY

    async def current_deployment(self, service_id: str) -> Any:
        from aegis.execution.ports import DeploymentInfo

        return DeploymentInfo(
            deployment_id="d1", service_id=service_id, version="v1",
            replicas_desired=2, replicas_ready=2,
        )

    async def deployment_history(self, service_id: str, *, limit: int = 10) -> list[Any]:
        return [await self.current_deployment(service_id)]

    async def list_services(self) -> list[Any]:
        return []

    async def list_instances(self, _service_id: str) -> list[Any]:
        return []

    async def get_instance(self, _instance_id: str) -> Any:
        raise ExternalServiceError("not found")


def claim_result(outcome: ClaimOutcome, *, protected: bool = False) -> ClaimResult:
    claim = VerificationClaim(
        id=("protected:" if protected else "goal:") + "error_rate",
        statement="error rate falls",
        test=ClaimTest(kind=VerificationTestKind.METRIC_THRESHOLD, metric="error_rate"),
        protected=protected,
    )
    return ClaimResult(
        claim=claim, outcome=outcome, before_value=0.2, after_value=0.01,
        threshold=0.05, evidence_ids=[], detail="", observed_at=NOW,
    )


@dataclass
class FakeVerification:
    """Returns a scripted verdict so every branch of the service is reachable."""

    verdict: VerificationVerdict = VerificationVerdict.VERIFIED
    results: list[ClaimResult] = field(default_factory=list)
    baseline_calls: int = 0

    async def capture_baseline(self, _claims: list[Any], **_kw: Any) -> Baseline:
        self.baseline_calls += 1
        return Baseline(captured_at=NOW, window_start=0.0, window_end=1.0)

    async def verify(self, *, incident_id: str, action_id: str | None = None, **_kw: Any):
        results = self.results or [claim_result(ClaimOutcome.PASS)]
        return VerificationRun(
            id="ver_01", incident_id=incident_id, action_id=action_id, kind="action",
            verdict=self.verdict, results=results,
            baseline=Baseline(captured_at=NOW, window_start=0.0, window_end=1.0),
            started_at=NOW, completed_at=NOW, notes="scripted",
        )


class FakeVerificationStore:
    def __init__(self) -> None:
        self.saved: list[str] = []

    async def save(self, run: VerificationRun) -> str:
        self.saved.append(run.id)
        return run.id


async def make_validated(**over: Any) -> ValidatedAction:
    """Mint a real ValidatedAction through the real gate.

    The service tests deliberately do not fabricate one: if the gate stopped
    producing them these tests would fail too, which is the point.
    """
    gate, parts = build_gate(**over.pop("gate_parts", {}))
    result = await run_gate(gate, over.pop("proposal", None))
    assert isinstance(result, ValidatedAction), result
    return result


def build_service(
    verification: FakeVerification | None = None,
    leases: FakeLeases | None = None,
) -> tuple[ExecutionService, dict[str, Any]]:
    from tests.unit.test_execution_gate import FakeActions

    parts: dict[str, Any] = {
        "actions": FakeActions(action=stored(state=ActionState.APPROVED)),
        "audit": FakeAudit(),
        "leases": leases or FakeLeases(),
        "verification": verification or FakeVerification(),
        "verification_store": FakeVerificationStore(),
    }
    service = ExecutionService(clock=FixedClock(NOW), **parts)
    return service, parts


def ports(runtime: FakeRuntime | None = None) -> ExecutionPorts:
    rt = runtime or FakeRuntime()
    return ExecutionPorts(runtime_read=rt, runtime_write=rt, cache=None, timeout_s=5.0)


class ReleasingLeases(FakeLeases):
    def __init__(self) -> None:
        super().__init__()
        self.released: list[str] = []

    async def release(self, lease: Any, **_kw: Any) -> None:
        self.released.append(lease.id)


# --------------------------------------------------------------------------- #
# the success path, stated narrowly                                            #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_verified_remediation_succeeds_and_releases_the_lease() -> None:
    leases = ReleasingLeases()
    validated = await make_validated()
    service, parts = build_service(leases=leases)
    runtime = FakeRuntime()

    report = await service.execute(validated, ports(runtime), settle_seconds=0)

    assert report.succeeded
    assert report.final_state is ActionState.SUCCESS
    assert runtime.restart_calls == ["payment-1"]
    assert leases.released == ["lse_01"]
    assert "action.execution_succeeded" in parts["audit"].events
    assert "verification.completed" in parts["audit"].events


@pytest.mark.asyncio
async def test_baseline_is_captured_before_the_write() -> None:
    """Measuring after the fact would absorb whatever the action changed."""
    verification = FakeVerification()
    validated = await make_validated()
    service, _ = build_service(verification=verification, leases=ReleasingLeases())
    await service.execute(validated, ports(), settle_seconds=0)
    assert verification.baseline_calls == 1


# --------------------------------------------------------------------------- #
# unverifiable is not success                                                  #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "verdict",
    [VerificationVerdict.PARTIALLY_VERIFIED, VerificationVerdict.INCONCLUSIVE],
)
@pytest.mark.asyncio
async def test_unverifiable_remediation_is_not_reported_as_success(
    verdict: VerificationVerdict,
) -> None:
    """An unproven change must never acquire a green badge."""
    validated = await make_validated()
    service, _ = build_service(
        verification=FakeVerification(verdict=verdict), leases=ReleasingLeases()
    )
    report = await service.execute(validated, ports(), settle_seconds=0)

    assert not report.succeeded
    assert report.executed
    assert report.escalated
    assert report.final_state is ActionState.FAILED
    assert "human" in report.escalation_reason.lower()


@pytest.mark.asyncio
async def test_telemetry_outage_does_not_read_as_a_healthy_service() -> None:
    """The single worst failure mode a verification system can have."""
    validated = await make_validated()
    service, _ = build_service(
        verification=FakeVerification(
            verdict=VerificationVerdict.INCONCLUSIVE,
            results=[claim_result(ClaimOutcome.UNAVAILABLE)],
        ),
        leases=ReleasingLeases(),
    )
    report = await service.execute(validated, ports(), settle_seconds=0)
    assert not report.succeeded
    assert report.escalated


# --------------------------------------------------------------------------- #
# rollback                                                                     #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_failed_verification_triggers_rollback() -> None:
    validated = await make_validated(
        proposal=proposal(
            action_type=ActionType.SCALE_UP_BOUNDED, arguments={"replica_delta": 1}
        )
    )
    service, parts = build_service(
        verification=FakeVerification(
            verdict=VerificationVerdict.FAILED,
            results=[claim_result(ClaimOutcome.FAIL)],
        ),
        leases=ReleasingLeases(),
    )
    runtime = FakeRuntime()
    report = await service.execute(validated, ports(runtime), settle_seconds=0)

    assert not report.succeeded
    assert report.rolled_back
    assert report.final_state is ActionState.ROLLED_BACK
    # Scaled 2 -> 3, then back to the observed baseline of 2.
    assert runtime.scale_calls == [3, 2]
    assert "action.rolled_back" in parts["audit"].events


@pytest.mark.asyncio
async def test_a_regression_on_a_protected_metric_rolls_back_and_escalates() -> None:
    """Fixing the symptom while breaking something else is not a success."""
    validated = await make_validated(
        proposal=proposal(
            action_type=ActionType.SCALE_UP_BOUNDED, arguments={"replica_delta": 1}
        )
    )
    service, parts = build_service(
        verification=FakeVerification(
            verdict=VerificationVerdict.REGRESSION_DETECTED,
            results=[
                claim_result(ClaimOutcome.PASS),
                claim_result(ClaimOutcome.FAIL, protected=True),
            ],
        ),
        leases=ReleasingLeases(),
    )
    report = await service.execute(validated, ports(), settle_seconds=0)

    assert report.rolled_back
    assert report.escalated
    assert "regression" in report.escalation_reason.lower()
    assert "verification.regression_detected" in parts["audit"].events


@pytest.mark.asyncio
async def test_a_failed_rollback_escalates_loudly_and_is_never_silent() -> None:
    """Changed, did not work, cannot be reverted - the worst possible state."""
    validated = await make_validated(
        proposal=proposal(
            action_type=ActionType.SCALE_UP_BOUNDED, arguments={"replica_delta": 1}
        )
    )
    service, parts = build_service(
        verification=FakeVerification(
            verdict=VerificationVerdict.FAILED,
            results=[claim_result(ClaimOutcome.FAIL)],
        ),
        leases=ReleasingLeases(),
    )
    runtime = FakeRuntime(raise_on_rollback=True)
    report = await service.execute(validated, ports(runtime), settle_seconds=0)

    assert not report.rolled_back
    assert report.escalated
    assert "manual intervention" in report.escalation_reason
    assert report.final_state is ActionState.FAILED
    assert "action.rollback_failed" in parts["audit"].events


@pytest.mark.asyncio
async def test_an_execution_that_reports_failure_is_not_verified_at_all() -> None:
    """There is nothing to verify if the change never landed."""
    validated = await make_validated()
    verification = FakeVerification()
    service, _ = build_service(verification=verification, leases=ReleasingLeases())
    report = await service.execute(
        validated, ports(FakeRuntime(restart_ok=False)), settle_seconds=0
    )
    assert report.executed
    assert not report.succeeded
    assert report.verification is None
    assert report.final_state is ActionState.FAILED


# --------------------------------------------------------------------------- #
# the lease is always released                                                 #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_the_lease_is_released_even_when_everything_fails() -> None:
    leases = ReleasingLeases()
    validated = await make_validated()
    service, _ = build_service(
        verification=FakeVerification(verdict=VerificationVerdict.FAILED),
        leases=leases,
    )
    await service.execute(
        validated, ports(FakeRuntime(restart_ok=False)), settle_seconds=0
    )
    assert leases.released == ["lse_01"]


@pytest.mark.asyncio
async def test_authorisation_that_lapsed_since_validation_refuses_to_execute() -> None:
    """Validation and execution are separated by real time."""
    leases = ReleasingLeases()
    validated = await make_validated()
    service, _ = build_service(leases=leases)
    # Move the clock past the lease deadline without touching the object.
    service._clock = FixedClock(NOW + timedelta(hours=1))  # noqa: SLF001
    runtime = FakeRuntime()

    with pytest.raises(AuthorizationError, match="lapsed"):
        await service.execute(validated, ports(runtime), settle_seconds=0)

    assert runtime.restart_calls == []   # nothing touched the environment
    assert leases.released == ["lse_01"]


# --------------------------------------------------------------------------- #
# retry policy                                                                 #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_only_idempotent_actions_are_auto_retryable() -> None:
    """Replaying a non-idempotent write is how one incident becomes two."""
    idempotent = await make_validated()
    assert ExecutionService.is_retryable(idempotent) is True

    granted_gate, _ = build_gate(
        approvals=_approved_for("act_01TEST"),
    )
    promote = await run_gate(
        granted_gate,
        proposal(
            action_type=ActionType.PROMOTE_PATCH,
            arguments={"patch_id": "pat_01"},
        ),
    )
    if isinstance(promote, ValidatedAction):
        assert ExecutionService.is_retryable(promote) is False


def _approved_for(action_id: str) -> Any:
    from tests.unit.test_execution_gate import FakeApprovals

    from aegis.execution.approvals import ApprovalRequest

    return FakeApprovals(
        granted=ApprovalRequest(
            id="apr_live", action_id=action_id, incident_id="inc_01TEST",
            requested_at=NOW, expires_at=NOW + timedelta(minutes=10),
            decision="approved", decided_by="user_alex",
            decided_at=NOW, note="",
        )
    )
