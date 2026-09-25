"""Every rollback the execution service performs lands in ``deployment_attempts``.

The rollback target of the *next* incident is read from these rows
(``RuntimePortBridge.deployment_history``), so a rollback that is not recorded
is not merely invisible on a page - it removes the version a future rollback
would have returned to. And because recording is bookkeeping, a failed write
here must never change what the execution report says happened.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from tests.unit.test_execution_gate import (
    NOW,
    FakeActions,
    FakeApprovals,
    FakeAudit,
    FakeLeases,
    FixedClock,
    proposal,
    stored,
)
from tests.unit.test_execution_service import (
    FakeRuntime,
    FakeVerification,
    FakeVerificationStore,
    make_validated,
    ports,
)
from tests.unit.test_patches import CapturingDeployments

from aegis.core.errors import DomainError
from aegis.domain.enums import ActionState, ActionType, VerificationVerdict
from aegis.domain.models import ResourceRef
from aegis.execution.approvals import ApprovalRequest
from aegis.execution.ports import DeploymentInfo, OperationResult
from aegis.execution.service import ExecutionService
from aegis.persistence.patches import DeploymentState

SERVICE_ID = "local-docker:aegis-workload:checkout"


class VersionedRuntime(FakeRuntime):
    """Checkout running the bad 1.4.2 build, with 1.4.1 in the recorded history."""

    def __init__(self, *, rollback_ok: bool = True) -> None:
        super().__init__()
        self.rollback_ok = rollback_ok
        self.rollbacks: list[str] = []

    async def deployment_history(self, service_id: str, **_kw: Any) -> list[DeploymentInfo]:
        return [
            DeploymentInfo(deployment_id="dep_a", service_id=service_id, version="1.4.2"),
            DeploymentInfo(deployment_id="dep_b", service_id=service_id, version="1.4.1"),
        ]

    async def current_deployment(self, service_id: str) -> DeploymentInfo:
        return DeploymentInfo(deployment_id="runtime", service_id=service_id, version="1.4.2")

    async def rollback_deployment(
        self, service_id: str, *, to_version: str = "", **_kw: Any
    ) -> OperationResult:
        self.rollbacks.append(to_version)
        return OperationResult(
            ok=self.rollback_ok,
            performed=f"rollback {service_id} -> {to_version}",
            error=None if self.rollback_ok else "docker refused the new container",
        )


class FailingStartDeployments(CapturingDeployments):
    async def start(self, **kw: Any) -> Any:
        raise DomainError("deployment_attempts is unreachable")


async def rollback_action() -> Any:
    granted = ApprovalRequest(
        id="apr_live",
        action_id="act_01TEST",
        incident_id="inc_01TEST",
        requested_at=NOW - timedelta(minutes=2),
        expires_at=NOW + timedelta(minutes=10),
        decision="approved",
        decided_by="dev-user",
        decided_at=NOW - timedelta(minutes=1),
        note="",
    )
    return await make_validated(
        gate_parts={"approvals": FakeApprovals(granted=granted)},
        proposal=proposal(
            action_type=ActionType.ROLLBACK_DEPLOYMENT,
            target=ResourceRef(
                resource_type="deployment",
                resource_id="checkout",
                environment="local",
                service_id=SERVICE_ID,
            ),
            arguments={"to_version": "1.4.1"},
            idempotency_key="idem-rollback-checkout-0001",
        ),
    )


def service(
    deployments: CapturingDeployments,
    verdict: VerificationVerdict = VerificationVerdict.VERIFIED,
) -> ExecutionService:
    return ExecutionService(
        actions=FakeActions(
            action=stored(
                state=ActionState.APPROVED, action_type=ActionType.ROLLBACK_DEPLOYMENT
            )
        ),
        audit=FakeAudit(),
        leases=FakeLeases(),
        verification=FakeVerification(verdict=verdict),
        verification_store=FakeVerificationStore(),
        deployments=deployments,
        clock=FixedClock(NOW),
    )


@pytest.mark.asyncio
async def test_a_verified_rollback_writes_start_and_finish_with_observed_versions() -> None:
    deployments = CapturingDeployments()
    runtime = VersionedRuntime()

    report = await service(deployments).execute(
        await rollback_action(), ports(runtime), settle_seconds=0
    )

    assert report.succeeded
    assert runtime.rollbacks == ["1.4.1"]
    assert len(deployments.started) == 1
    opened = deployments.started[0]
    assert opened["service_id"] == SERVICE_ID
    assert opened["strategy"] == "rollback"
    assert opened["state"] is DeploymentState.IN_PROGRESS
    closed = deployments.finished[0]
    assert closed["state"] is DeploymentState.VERIFIED
    assert closed["from_version"] == "1.4.2"
    assert closed["to_version"] == "1.4.1"
    assert closed["verification_id"] == "ver_01"


@pytest.mark.asyncio
async def test_a_failed_rollback_is_closed_as_failed() -> None:
    deployments = CapturingDeployments()

    report = await service(deployments).execute(
        await rollback_action(), ports(VersionedRuntime(rollback_ok=False)), settle_seconds=0
    )

    assert not report.succeeded
    assert report.final_state is ActionState.FAILED
    assert len(deployments.started) == 1
    assert deployments.finished[0]["state"] is DeploymentState.FAILED
    assert "refused" in deployments.finished[0]["error"]


@pytest.mark.asyncio
async def test_recording_failure_never_turns_a_failed_rollback_into_success() -> None:
    """Both bookkeeping writes failing leaves the report exactly as it was."""
    for deployments in (FailingStartDeployments(), CapturingDeployments(fail_finish=True)):
        report = await service(deployments).execute(
            await rollback_action(),
            ports(VersionedRuntime(rollback_ok=False)),
            settle_seconds=0,
        )
        assert not report.succeeded
        assert report.final_state is ActionState.FAILED
        assert report.escalated


@pytest.mark.asyncio
async def test_recording_failure_does_not_block_an_authorised_rollback() -> None:
    runtime = VersionedRuntime()

    report = await service(FailingStartDeployments()).execute(
        await rollback_action(), ports(runtime), settle_seconds=0
    )

    assert runtime.rollbacks == ["1.4.1"]
    assert report.succeeded


@pytest.mark.asyncio
async def test_the_automatic_undo_of_a_regressed_rollback_is_its_own_attempt() -> None:
    """The undo moves the service back to 1.4.2; history must say so."""
    deployments = CapturingDeployments()
    runtime = VersionedRuntime()

    report = await service(deployments, VerificationVerdict.REGRESSION_DETECTED).execute(
        await rollback_action(), ports(runtime), settle_seconds=0
    )

    assert report.rolled_back
    assert runtime.rollbacks == ["1.4.1", "1.4.2"]
    assert [d["strategy"] for d in deployments.started] == [
        "rollback",
        "compensating_rollback",
    ]
    assert deployments.started[1]["detail"]["compensates"] == "dep_1"
    undo, original = deployments.finished
    assert undo["state"] is DeploymentState.DEPLOYED
    assert (undo["from_version"], undo["to_version"]) == ("1.4.1", "1.4.2")
    assert original["state"] is DeploymentState.ROLLED_BACK
