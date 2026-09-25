"""Patch, sandbox-run and deployment persistence.

Three tables existed for months with no writer, so these tests are less about
"does the INSERT run" and more about the properties that decide whether the rows
can be trusted once they exist:

* a run that was killed is not a failing test, and neither is a repository that
  could not be fetched;
* the size and hash of a patch are computed from the patch, so a model cannot
  describe its own change;
* the same diff proposed twice is one patch, not two;
* a state change that lost a race fails rather than clobbering;
* a deployment attempt cannot be closed twice, so a late verification cannot
  rewrite an outcome someone already acted on.

Every collaborator is a fake. Nothing here touches Postgres or Docker.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from tests.unit.test_execution_gate import (
    NOW,
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

from aegis.core.config import Settings
from aegis.core.errors import DomainError, NotFoundError, ValidationError
from aegis.domain.enums import ActionState, ActionType, VerificationVerdict
from aegis.domain.models import ResourceRef
from aegis.execution.sandbox import SandboxResult, SandboxRunner
from aegis.execution.service import ExecutionService, RecordingSandboxRunner
from aegis.persistence.patches import (
    MAX_EXCERPT_CHARS,
    DeploymentRepository,
    DeploymentState,
    PatchRepository,
    PatchState,
    SandboxRunRepository,
    StoredSandboxRun,
    diff_line_counts,
    diff_sha256,
)

INCIDENT = "inc_01TEST"

# A small, well-formed unified diff used wherever the content does not matter.
DIFF = (
    "--- a/app/db.py\n"
    "+++ b/app/db.py\n"
    "@@ -10,3 +10,4 @@\n"
    " pool = Pool()\n"
    "+pool.timeout = 5\n"
    " return pool\n"
    " # end\n"
)


# --------------------------------------------------------------------------- #
# fakes                                                                        #
# --------------------------------------------------------------------------- #


class ScriptedDB:
    """Records every statement and replays scripted rows in order.

    Deliberately dumb: it does not pretend to be Postgres. What the repository
    binds is the interesting thing, and a fake that interpreted SQL would only
    be testing the fake.
    """

    def __init__(self, *rows: Any) -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        self.rows: list[Any] = list(rows)

    def _next(self) -> Any:
        return self.rows.pop(0) if self.rows else None

    async def fetchrow(self, query: str, *args: Any) -> Any:
        self.calls.append((query, args))
        return self._next()

    async def fetch(self, query: str, *args: Any) -> list[Any]:
        self.calls.append((query, args))
        row = self._next()
        if row is None:
            return []
        return list(row) if isinstance(row, list) else [row]

    async def execute(self, query: str, *args: Any) -> str:
        self.calls.append((query, args))
        return "INSERT 0 1"

    def statements(self, fragment: str) -> list[tuple[str, tuple[Any, ...]]]:
        return [c for c in self.calls if fragment in c[0]]


def sandbox_row(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": "sbx_1",
        "incident_id": INCIDENT,
        "action_id": None,
        "purpose": "test_patch",
        "image": "python:3.12-slim",
        "repo": "acme/payments",
        "base_ref": "a1b2c3d",
        "patch_sha256": None,
        "command": "python -m pytest -q",
        "exit_code": 1,
        "timed_out": False,
        "killed": False,
        "duration_ms": 1200,
        "stdout_excerpt": "1 failed",
        "stderr_excerpt": "",
        "artifacts": [],
        "resource_limits": {},
        "network": "none",
        "started_at": NOW,
        "finished_at": NOW,
    }
    base.update(over)
    return base


def patch_row(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": "pat_1",
        "incident_id": INCIDENT,
        "repo": "acme/payments",
        "base_ref": "a1b2c3d",
        "summary": "bound the pool checkout",
        "rationale": "the pool never times out",
        "diff": DIFF,
        "diff_sha256": diff_sha256(DIFF),
        "files_changed": ["app/db.py"],
        "lines_added": 1,
        "lines_removed": 0,
        "supporting_evidence": ["ev_01A"],
        "reproduction_run_id": None,
        "test_run_id": None,
        "state": "PROPOSED",
        "pull_request_url": None,
        "created_at": NOW,
        "updated_at": NOW,
    }
    base.update(over)
    return base


def deployment_row(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": "dep_1",
        "incident_id": INCIDENT,
        "action_id": "act_01TEST",
        "patch_id": None,
        "environment": "local",
        "service_id": "payment",
        "from_version": None,
        "to_version": None,
        "strategy": "scale",
        "state": "IN_PROGRESS",
        "verification_id": None,
        "detail": {},
        "error": None,
        "started_at": NOW,
        "finished_at": None,
    }
    base.update(over)
    return base


def result(**over: Any) -> SandboxResult:
    base: dict[str, Any] = {
        "id": "sbx_1",
        "purpose": "test_patch",
        "image": "python:3.12-slim",
        "command": "python -m pytest -q",
        "exit_code": 1,
        "timed_out": False,
        "killed": False,
        "duration_ms": 1200,
        "stdout": "1 failed",
        "stderr": "",
        "artifacts": [],
        "resource_limits": {"cpu": 2.0},
        "network": "none",
        "started_at": datetime.now(UTC),
        "finished_at": datetime.now(UTC),
        "repo": "acme/payments",
        "base_ref": "a1b2c3d",
    }
    base.update(over)
    return SandboxResult(**base)


# --------------------------------------------------------------------------- #
# sandbox runs: three outcomes, kept apart                                     #
# --------------------------------------------------------------------------- #


async def test_sandbox_output_is_truncated_before_the_insert() -> None:
    """An unbounded log must not be able to fill the incident database."""
    db = ScriptedDB(sandbox_row())
    runs = SandboxRunRepository(db)  # type: ignore[arg-type]

    await runs.record(result(stdout="x" * 200_000), incident_id=INCIDENT)

    _query, args = db.statements("INSERT INTO sandbox_runs")[0]
    stdout_arg = args[13]
    assert len(stdout_arg) < 200_000
    assert stdout_arg.startswith("x" * MAX_EXCERPT_CHARS)
    assert "truncated" in stdout_arg


async def test_an_unknown_purpose_never_reaches_the_database() -> None:
    """The CHECK constraint is restated at the boundary, so nothing is lost.

    A rejected INSERT here would discard the only record that a container ran.
    """
    db = ScriptedDB(sandbox_row())
    runs = SandboxRunRepository(db)  # type: ignore[arg-type]

    with pytest.raises(ValidationError):
        await runs.record(result(), incident_id=INCIDENT, purpose="exfiltrate")

    assert db.calls == []


async def test_recording_a_run_twice_does_not_double_count_it() -> None:
    """A worker that crashed between running and recording retries safely."""
    db = ScriptedDB(None, sandbox_row())
    runs = SandboxRunRepository(db)  # type: ignore[arg-type]

    stored_run = await runs.record(result(), incident_id=INCIDENT)

    assert stored_run.id == "sbx_1"
    assert "ON CONFLICT (id) DO NOTHING" in db.calls[0][0]
    assert "SELECT * FROM sandbox_runs WHERE id" in db.calls[1][0]


def test_a_killed_run_is_not_a_failing_test() -> None:
    """"The sandbox stopped it" and "the suite failed" are different facts."""
    failed = StoredSandboxRun(**sandbox_row(exit_code=1))
    stopped = StoredSandboxRun(**sandbox_row(exit_code=None, timed_out=True, killed=True))
    passed = StoredSandboxRun(**sandbox_row(exit_code=0))

    assert failed.reached_verdict and not failed.succeeded
    assert not stopped.reached_verdict and not stopped.succeeded
    assert passed.reached_verdict and passed.succeeded


async def test_failures_and_timeouts_are_both_persisted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Recording is by wrapping, so no caller can forget to do it.

    The parent runner is stubbed rather than reimplemented: what is under test
    is that ``RecordingSandboxRunner`` writes a row for whatever comes back,
    including the outcomes a happy-path test would never produce.
    """
    from aegis.execution.sandbox import SandboxSpec

    scripted = [
        result(id="sbx_fail", exit_code=1),
        result(id="sbx_timeout", exit_code=None, timed_out=True, killed=True),
    ]

    async def fake_run(_self: Any, _spec: SandboxSpec) -> SandboxResult:
        return scripted.pop(0)

    monkeypatch.setattr(SandboxRunner, "run", fake_run)
    db = ScriptedDB(sandbox_row(id="sbx_fail"), sandbox_row(id="sbx_timeout"))
    runner = RecordingSandboxRunner(
        Settings(postgres_password="x"),
        SandboxRunRepository(db),  # type: ignore[arg-type]
    )
    spec = SandboxSpec(
        purpose="test_patch", command=["python", "-m", "pytest"], incident_id=INCIDENT
    )

    await runner.run(spec)
    await runner.run(spec)

    inserts = db.statements("INSERT INTO sandbox_runs")
    assert len(inserts) == 2
    assert [args[0] for _q, args in inserts] == ["sbx_fail", "sbx_timeout"]
    # exit_code, timed_out, killed - the columns that keep the two apart.
    assert inserts[0][1][9:12] == (1, False, False)
    assert inserts[1][1][9:12] == (None, True, True)


# --------------------------------------------------------------------------- #
# patches                                                                      #
# --------------------------------------------------------------------------- #


async def test_patch_size_is_computed_not_claimed() -> None:
    """The model describes the change; the repository measures it."""
    db = ScriptedDB(patch_row())
    patches = PatchRepository(db)  # type: ignore[arg-type]

    await patches.propose(
        incident_id=INCIDENT,
        repo="acme/payments",
        base_ref="a1b2c3d",
        summary="bound the pool checkout",
        diff=DIFF,
        files_changed=["app/db.py"],
    )

    _query, args = db.statements("INSERT INTO remediation_patches")[0]
    assert args[7] == diff_sha256(DIFF)
    assert (args[9], args[10]) == diff_line_counts(DIFF) == (1, 0)


async def test_an_oversized_diff_is_bounded_before_the_insert() -> None:
    """The bound holds regardless of caller.

    A malformed diff is recorded before it is rejected, and that text never
    passed the debugger's own size check - so the database-side bound is the
    one that has to hold.
    """
    db = ScriptedDB(patch_row())
    patches = PatchRepository(db)  # type: ignore[arg-type]
    huge = "+x\n" * 200_000

    await patches.propose(
        incident_id=INCIDENT, repo="r", base_ref="ref", summary="s",
        diff=huge, files_changed=[],
    )

    _query, args = db.statements("INSERT INTO remediation_patches")[0]
    assert len(args[6]) < len(huge)
    assert "truncated" in args[6]
    # The hash describes exactly what was stored, not what was offered.
    assert args[7] == diff_sha256(args[6])


def test_file_headers_are_not_counted_as_changed_lines() -> None:
    """--- and +++ start with - and +; counting them inflates every patch."""
    assert diff_line_counts(DIFF) == (1, 0)


async def test_the_same_diff_proposed_twice_is_one_patch() -> None:
    """Deduplication is the UNIQUE index, and the caller is told which happened.

    A replay reported as a fresh proposal would be tested again - a container
    burned to learn what is already recorded.
    """
    db = ScriptedDB(patch_row())
    patches = PatchRepository(db)  # type: ignore[arg-type]
    first, created_first = await patches.propose(
        incident_id=INCIDENT, repo="acme/payments", base_ref="a1b2c3d",
        summary="s", diff=DIFF, files_changed=["app/db.py"],
    )

    # Second attempt: the INSERT conflicts and returns nothing, so the
    # repository reads back the row the unique index protected.
    db.rows = [None, patch_row()]
    second, created_second = await patches.propose(
        incident_id=INCIDENT, repo="acme/payments", base_ref="a1b2c3d",
        summary="different wording, identical change",
        diff=DIFF, files_changed=["app/db.py"],
    )

    assert created_first is True
    assert created_second is False
    assert first.id == second.id == "pat_1"
    lookup = db.statements("WHERE incident_id = $1 AND diff_sha256 = $2")[0]
    assert lookup[1] == (INCIDENT, diff_sha256(DIFF))


async def test_an_empty_diff_is_refused() -> None:
    db = ScriptedDB()
    patches = PatchRepository(db)  # type: ignore[arg-type]

    with pytest.raises(ValidationError):
        await patches.propose(
            incident_id=INCIDENT, repo="r", base_ref="ref", summary="s",
            diff="   \n\n", files_changed=[],
        )
    assert db.calls == []


async def test_a_lost_state_race_fails_rather_than_clobbering() -> None:
    """Two workers moving one patch produce a winner and an error, not two wins."""
    db = ScriptedDB(None, patch_row(state="TESTED"))
    patches = PatchRepository(db)  # type: ignore[arg-type]

    with pytest.raises(DomainError) as exc:
        await patches.transition(
            "pat_1", to=PatchState.PROMOTED, expected=PatchState.PROPOSED
        )

    assert "TESTED" in str(exc.value)


async def test_transitioning_a_missing_patch_is_not_found() -> None:
    db = ScriptedDB(None, None)
    patches = PatchRepository(db)  # type: ignore[arg-type]

    with pytest.raises(NotFoundError):
        await patches.transition("pat_nope", to=PatchState.REJECTED)


async def test_a_rejection_reason_is_appended_not_substituted() -> None:
    """Why a patch was written survives the record of why it was refused."""
    db = ScriptedDB(patch_row(state="REJECTED", rationale="original\nrejected: bad hunk"))
    patches = PatchRepository(db)  # type: ignore[arg-type]

    updated = await patches.transition(
        "pat_1", to=PatchState.REJECTED, expected=PatchState.PROPOSED,
        reason="rejected: bad hunk",
    )

    query, args = db.calls[0]
    assert "rationale ||" in query
    assert args[3] == "rejected: bad hunk"
    assert "original" in updated.rationale


async def test_linking_a_test_run_never_erases_the_reproduction_run() -> None:
    db = ScriptedDB(patch_row(reproduction_run_id="sbx_repro", test_run_id="sbx_test"))
    patches = PatchRepository(db)  # type: ignore[arg-type]

    linked = await patches.link_runs("pat_1", test_run_id="sbx_test")

    query, args = db.calls[0]
    assert "COALESCE($2, reproduction_run_id)" in query
    assert args == ("pat_1", None, "sbx_test")
    assert linked.reproduction_run_id == "sbx_repro"


# --------------------------------------------------------------------------- #
# deployments                                                                  #
# --------------------------------------------------------------------------- #


async def test_a_deployment_needs_a_service() -> None:
    db = ScriptedDB()
    deployments = DeploymentRepository(db)  # type: ignore[arg-type]

    with pytest.raises(ValidationError):
        await deployments.start(environment="local", service_id="")
    assert db.calls == []


async def test_a_finished_deployment_cannot_be_rewritten() -> None:
    """A late verification must not turn a failure into a success."""
    db = ScriptedDB(None, deployment_row(state="FAILED", finished_at=NOW))
    deployments = DeploymentRepository(db)  # type: ignore[arg-type]

    with pytest.raises(DomainError) as exc:
        await deployments.finish("dep_1", state=DeploymentState.VERIFIED)

    assert "already finished" in str(exc.value)
    assert "finished_at IS NULL" in db.calls[0][0]


async def test_finishing_an_unknown_deployment_is_not_found() -> None:
    db = ScriptedDB(None, None)
    deployments = DeploymentRepository(db)  # type: ignore[arg-type]

    with pytest.raises(NotFoundError):
        await deployments.finish("dep_nope", state=DeploymentState.FAILED)


# --------------------------------------------------------------------------- #
# the execution service writes the rows                                        #
# --------------------------------------------------------------------------- #


class CapturingDeployments:
    """The repository interface, recording what the service asks of it."""

    def __init__(self, *, fail_finish: bool = False) -> None:
        self.started: list[dict[str, Any]] = []
        self.finished: list[dict[str, Any]] = []
        self.fail_finish = fail_finish

    async def start(self, **kw: Any) -> Any:
        self.started.append(kw)
        from aegis.persistence.patches import StoredDeployment

        carried = {
            k: v for k, v in kw.items()
            if k in {"environment", "service_id", "incident_id", "action_id", "strategy"}
        }
        return StoredDeployment(
            **deployment_row(**carried, state=DeploymentState.IN_PROGRESS)
        )

    async def finish(self, deployment_id: str, **kw: Any) -> Any:
        if self.fail_finish:
            raise DomainError("the row vanished")
        self.finished.append({"id": deployment_id, **kw})
        from aegis.persistence.patches import StoredDeployment

        return StoredDeployment(**deployment_row(state=kw["state"], finished_at=NOW))


def scale_service(
    deployments: CapturingDeployments,
    verdict: VerificationVerdict = VerificationVerdict.VERIFIED,
) -> ExecutionService:
    from tests.unit.test_execution_gate import FakeActions

    return ExecutionService(
        actions=FakeActions(
            action=stored(
                state=ActionState.APPROVED, action_type=ActionType.SCALE_UP_BOUNDED
            )
        ),
        audit=FakeAudit(),
        leases=FakeLeases(),
        verification=FakeVerification(verdict=verdict),
        verification_store=FakeVerificationStore(),
        deployments=deployments,
        clock=FixedClock(NOW),
    )


async def scale_action() -> Any:
    return await make_validated(
        proposal=proposal(
            action_type=ActionType.SCALE_UP_BOUNDED,
            target=ResourceRef(
                resource_type="service",
                resource_id="payment",
                environment="local",
                service_id="local:demo:payment",
            ),
            arguments={"replica_delta": 1},
            idempotency_key="idem-scale-payment-0001",
        )
    )


async def test_a_verified_scale_is_recorded_as_a_deployment() -> None:
    """The page says "deployed and healthy" only because these rows exist."""
    deployments = CapturingDeployments()
    service = scale_service(deployments)

    report = await service.execute(await scale_action(), ports(), settle_seconds=0)

    assert report.succeeded
    assert deployments.started[0]["service_id"] == "local:demo:payment"
    assert deployments.started[0]["strategy"] == "scale"
    closed = deployments.finished[0]
    assert closed["state"] is DeploymentState.VERIFIED
    assert closed["verification_id"] == "ver_01"
    # Replica counts observed by the executor, not versions invented here.
    assert closed["to_version"] is None
    assert closed["detail"]["to_replicas"] == 3


async def test_an_unverified_change_is_deployed_not_verified() -> None:
    """PARTIALLY_VERIFIED left the change in place; it did not prove it worked."""
    deployments = CapturingDeployments()
    service = scale_service(deployments, VerificationVerdict.PARTIALLY_VERIFIED)

    report = await service.execute(await scale_action(), ports(), settle_seconds=0)

    assert not report.succeeded
    assert deployments.finished[0]["state"] is DeploymentState.DEPLOYED


async def test_a_failed_bookkeeping_write_never_loses_the_execution() -> None:
    """An unclosed attempt row is visible; a lost execution report is not."""
    deployments = CapturingDeployments(fail_finish=True)
    service = scale_service(deployments)

    report = await service.execute(await scale_action(), ports(FakeRuntime()), settle_seconds=0)

    assert report.succeeded
    assert report.final_state is ActionState.SUCCESS
    assert deployments.finished == []


def test_a_restart_is_not_a_deployment() -> None:
    """Padding the history with rows nothing can roll back to helps nobody."""
    from aegis.execution.service import DEPLOYMENT_ACTIONS

    assert ActionType.RESTART_INSTANCE not in DEPLOYMENT_ACTIONS
    assert ActionType.CLEAR_CACHE_KEY not in DEPLOYMENT_ACTIONS
    assert ActionType.RERUN_HEALTH_CHECK not in DEPLOYMENT_ACTIONS
    assert ActionType.ROLLBACK_DEPLOYMENT in DEPLOYMENT_ACTIONS
    assert ActionType.SCALE_UP_BOUNDED in DEPLOYMENT_ACTIONS
