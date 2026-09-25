"""The debugger node: what it refuses to run, and what it records anyway.

A node that writes code and then executes it is the most dangerous thing in this
repository, so the tests are about the refusals rather than the happy path:

* a diff that does not parse is recorded and rejected, and no container starts;
* a diff touching a file the investigation never localised is refused by path,
  not by intent;
* the same diff twice is one patch and one set of runs;
* a sandbox that was stopped mid-run leaves the patch un-judged, while a suite
  that failed rejects it - two different facts with two different outcomes;
* an abstained diagnosis never reaches the model at all.

The tool boundary, the invoker and the sandbox tools are the real ones. Only the
model, the Docker daemon and the patch repository are faked, so what is asserted
is the trail the production path actually leaves.
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime
from typing import Any

import pytest
from tests.unit.test_workflow_tools import FakeDB as ToolCallDB
from tests.unit.test_workflow_tools import FakeEvidenceStore

from aegis.agents.debugger import (
    make_debug_remediation,
    repo_url,
    suite_for,
    validate_diff,
)
from aegis.agents.schemas import PatchProposalOut
from aegis.agents.state import BudgetGuard, IncidentState
from aegis.agents.workflow import WorkflowDeps
from aegis.core.config import Settings
from aegis.core.errors import DomainError, ValidationError
from aegis.core.resilience import reset_breakers
from aegis.execution.sandbox import SandboxResult, SandboxRunner, SandboxSpec
from aegis.execution.service import RecordingSandboxRunner
from aegis.mcp import ToolDeps, ToolInvoker, default_registry
from aegis.persistence.patches import (
    PatchState,
    SandboxRunRepository,
    StoredPatch,
    diff_sha256,
)
from aegis.retrieval.code import CodeLocalization, FileCandidate

INCIDENT = "inc_01TEST"
REPO = "acme/payments"
REF = "a1b2c3d"

# A minimal diff that parses, changes one line and stays inside the localisation.
GOOD_DIFF = (
    "--- a/app/db.py\n"
    "+++ b/app/db.py\n"
    "@@ -10,3 +10,4 @@\n"
    " pool = Pool()\n"
    "+pool.timeout = 5\n"
    " return pool\n"
    " # end\n"
)

# The columns sandbox_runs is written with, in bind order.
SANDBOX_COLUMNS = (
    "id", "incident_id", "action_id", "purpose", "image", "repo", "base_ref",
    "patch_sha256", "command", "exit_code", "timed_out", "killed", "duration_ms",
    "stdout_excerpt", "stderr_excerpt", "artifacts", "resource_limits", "network",
    "started_at", "finished_at",
)


@pytest.fixture(autouse=True)
def _clean_breakers() -> None:
    reset_breakers()


# --------------------------------------------------------------------------- #
# fakes                                                                        #
# --------------------------------------------------------------------------- #


class FakeDB(ToolCallDB):
    """The tool-call recorder, plus enough of a row to satisfy sandbox_runs."""

    async def fetchrow(self, query: str, *args: Any) -> Any:
        self.statements.append((query, args))
        if "INSERT INTO sandbox_runs" in query:
            return dict(zip(SANDBOX_COLUMNS, args, strict=True))
        return None

    @property
    def sandbox_rows(self) -> list[dict[str, Any]]:
        return [
            dict(zip(SANDBOX_COLUMNS, args, strict=True))
            for query, args in self.statements
            if "INSERT INTO sandbox_runs" in query
        ]


class FakeRouter:
    """Returns scripted structured output, or refuses to be called at all."""

    def __init__(self, *outputs: PatchProposalOut, forbidden: bool = False) -> None:
        self.outputs = list(outputs)
        self.forbidden = forbidden
        self.calls = 0

    async def structured(self, **kwargs: Any) -> tuple[Any, dict[str, Any]]:
        if self.forbidden:
            raise AssertionError(f"the model must not be consulted: {kwargs}")
        self.calls += 1
        out = self.outputs.pop(0) if len(self.outputs) > 1 else self.outputs[0]
        return out, {"model": "fake", "provider": "fake"}


class FakePatches:
    """The patch repository's contract, including the state guard.

    The guard is reproduced rather than ignored because the node's ordering -
    PROPOSED, then REPRODUCED, then TESTED or REJECTED - is a real claim. A fake
    that accepted any transition would let the node walk the states in any order
    and still pass.
    """

    def __init__(self) -> None:
        self.rows: dict[str, StoredPatch] = {}
        self.by_hash: dict[tuple[str, str], str] = {}
        self.transitions: list[tuple[str, PatchState, str]] = []
        self.links: list[tuple[str, str | None, str | None]] = []

    async def propose(
        self,
        *,
        incident_id: str,
        repo: str,
        base_ref: str,
        summary: str,
        diff: str,
        files_changed: list[str],
        rationale: str = "",
        supporting_evidence: list[str] | None = None,
    ) -> tuple[StoredPatch, bool]:
        digest = diff_sha256(diff)
        key = (incident_id, digest)
        if key in self.by_hash:
            return self.rows[self.by_hash[key]], False
        now = datetime.now(UTC)
        patch = StoredPatch(
            id=f"pat_{len(self.rows) + 1}",
            incident_id=incident_id,
            repo=repo,
            base_ref=base_ref,
            summary=summary,
            rationale=rationale,
            diff=diff,
            diff_sha256=digest,
            files_changed=list(files_changed),
            lines_added=0,
            lines_removed=0,
            supporting_evidence=list(supporting_evidence or []),
            reproduction_run_id=None,
            test_run_id=None,
            state=PatchState.PROPOSED,
            pull_request_url=None,
            created_at=now,
            updated_at=now,
        )
        self.rows[patch.id] = patch
        self.by_hash[key] = patch.id
        return patch, True

    async def transition(
        self,
        patch_id: str,
        *,
        to: PatchState,
        expected: PatchState | None = None,
        reason: str = "",
        pull_request_url: str | None = None,
    ) -> StoredPatch:
        current = self.rows[patch_id]
        if expected is not None and current.state is not expected:
            raise DomainError(f"patch is {current.state.value}, expected {expected.value}")
        updated = dataclasses.replace(
            current,
            state=to,
            rationale=f"{current.rationale}\n{reason}".strip(),
        )
        self.rows[patch_id] = updated
        self.transitions.append((patch_id, to, reason))
        return updated

    async def link_runs(
        self,
        patch_id: str,
        *,
        reproduction_run_id: str | None = None,
        test_run_id: str | None = None,
    ) -> StoredPatch:
        current = self.rows[patch_id]
        updated = dataclasses.replace(
            current,
            reproduction_run_id=reproduction_run_id or current.reproduction_run_id,
            test_run_id=test_run_id or current.test_run_id,
        )
        self.rows[patch_id] = updated
        self.links.append((patch_id, reproduction_run_id, test_run_id))
        return updated

    @property
    def only(self) -> StoredPatch:
        assert len(self.rows) == 1, self.rows
        return next(iter(self.rows.values()))


def sandbox_result(**over: Any) -> SandboxResult:
    base: dict[str, Any] = {
        "id": "sbx_1",
        "purpose": "test_patch",
        "image": "python:3.12-slim",
        "command": "python -m pytest -q",
        "exit_code": 0,
        "timed_out": False,
        "killed": False,
        "duration_ms": 900,
        "stdout": "40 passed",
        "stderr": "",
        "artifacts": [],
        "resource_limits": {"cpu": 2.0},
        "network": "none",
        "started_at": datetime.now(UTC),
        "finished_at": datetime.now(UTC),
        "repo": REPO,
        "base_ref": REF,
    }
    base.update(over)
    return SandboxResult(**base)


def localization(paths: tuple[str, ...] = ("app/db.py",)) -> CodeLocalization:
    return CodeLocalization(
        incident_id=INCIDENT,
        service_ids=("payment",),
        symptom_terms=("pool",),
        repos=(REPO,),
        files=tuple(
            FileCandidate(
                repo=REPO,
                ref=REF,
                path=path,
                score=2.0,
                reason="touched by 2 commits in the incident window",
            )
            for path in paths
        ),
    )


# --------------------------------------------------------------------------- #
# assembly                                                                     #
# --------------------------------------------------------------------------- #


def build(
    *,
    router: FakeRouter,
    runs: list[SandboxResult] | None = None,
    paths: tuple[str, ...] = ("app/db.py",),
    with_tools: bool = True,
    resumed: bool = False,
    monkeypatch: pytest.MonkeyPatch | None = None,
) -> tuple[Any, FakeDB, FakePatches, FakeEvidenceStore]:
    db = FakeDB()
    evidence = FakeEvidenceStore()
    patches = FakePatches()
    settings = Settings(postgres_password="x", sandbox_enabled=True)

    scripted = list(runs or [])
    if monkeypatch is not None:
        async def fake_run(_self: Any, spec: SandboxSpec) -> SandboxResult:
            assert spec.incident_id == INCIDENT
            return scripted.pop(0)

        monkeypatch.setattr(SandboxRunner, "run", fake_run)

    sandbox = RecordingSandboxRunner(
        settings, SandboxRunRepository(db)  # type: ignore[arg-type]
    )
    invoker: ToolInvoker | None = None
    if with_tools:
        registry = default_registry(
            ToolDeps(
                evidence=evidence,
                sandbox=sandbox,
                sandbox_image=settings.sandbox_image,
            )
        )
        invoker = ToolInvoker(registry, db=db)

    deps = WorkflowDeps(
        settings=settings,
        db=db,
        evidence=evidence,
        prometheus=None,
        neo4j=None,
        router=router,
        budget=BudgetGuard(
            max_wall_seconds=300, max_llm_calls=10, max_tool_calls=50, max_tokens=100_000
        ),
        tools=invoker,
        patches=patches,
        sandbox=sandbox,
    )
    if not resumed:
        # What localize_code does when it runs in this process.
        deps.localization_attempted = True
        deps.localization = localization(paths)
    return make_debug_remediation(deps), db, patches, evidence


def state(**over: Any) -> IncidentState:
    base: dict[str, Any] = {
        "incident_id": INCIDENT,
        "correlation_id": "corr_01",
        "environment": "local",
        "title": "payment error rate elevated",
        "abstained": False,
        "diagnosis": {
            "statement": "the connection pool is exhausted under load",
            "root_cause_category": "connection_pool_exhaustion",
        },
        "evidence_ids": ["ev_01A"],
        "evidence_summaries": [],
        "evidence_gaps": [],
    }
    base.update(over)
    return base  # type: ignore[return-value]


def proposal(diff: str = GOOD_DIFF, **over: Any) -> PatchProposalOut:
    base: dict[str, Any] = {
        "propose_patch": True,
        "summary": "bound the pool checkout",
        "rationale": "ev_01A shows checkouts blocking indefinitely",
        "diff": diff,
        "files_changed": ["app/db.py"],
        "supporting_evidence": ["ev_01A", "ev_INVENTED"],
    }
    base.update(over)
    return PatchProposalOut(**base)


# --------------------------------------------------------------------------- #
# the diff validator, on its own                                               #
# --------------------------------------------------------------------------- #


def test_a_well_formed_diff_passes_and_is_measured() -> None:
    checked = validate_diff(GOOD_DIFF, allowed_paths=["app/db.py"])
    assert checked.files == ("app/db.py",)
    assert (checked.lines_added, checked.lines_removed) == (1, 0)
    assert checked.diff.endswith("\n")


@pytest.mark.parametrize(
    ("diff", "fragment"),
    [
        ("here is the fix you asked for", "not part of a unified diff"),
        ("--- a/app/db.py\n+++ b/app/db.py\n pool = Pool()\n", "no '@@' hunk header"),
        (
            "--- a/app/db.py\n+++ b/app/db.py\n@@ -10,9 +10,9 @@\n pool = Pool()\n",
            "declares -9/+9 lines but contains",
        ),
        (
            "--- a/app/db.py\n+++ b/app/db.py\n@@ -1,1 +1,1 @@\n pool\n@@ -1,1 +1,1 @@\n"
            "!wat\n",
            "starts with",
        ),
        ("--- /dev/null\n+++ b/app/db.py\n@@ -0,0 +1 @@\n+x\n", "may only\nmodify"),
        # git honours a rename directive over the ---/+++ pair, so a diff that
        # looks in-scope could move the file out of it.
        (
            "diff --git a/app/db.py b/app/db.py\nrename from app/db.py\n"
            "rename to ../outside.py\n" + GOOD_DIFF,
            "rename or copy one",
        ),
        (
            "diff --git a/app/db.py b/app/secrets.py\n" + GOOD_DIFF,
            "app/secrets.py is not one of the localised candidate files",
        ),
        (
            "diff --git something odd\n" + GOOD_DIFF,
            "cannot parse",
        ),
        (
            "diff --git a/app/db.py b/app/db.py\nindex 111..222 100644\n"
            "GIT binary patch\nliteral 24\n",
            "rename or copy one",
        ),
    ],
)
def test_a_diff_that_does_not_parse_is_refused(diff: str, fragment: str) -> None:
    with pytest.raises(ValidationError) as exc:
        validate_diff(diff, allowed_paths=["app/db.py"])
    assert fragment.replace("\n", " ") in str(exc.value).replace("\n", " ")


def test_a_no_newline_marker_does_not_make_a_diff_malformed() -> None:
    """git emits this for any file without a trailing newline.

    Rejecting it would refuse an applicable patch and blame the model for the
    validator's own gap.
    """
    diff = (
        "--- a/app/db.py\n"
        "+++ b/app/db.py\n"
        "@@ -10,1 +10,1 @@\n"
        "-foo\n"
        "\\ No newline at end of file\n"
        "+bar\n"
        "\\ No newline at end of file\n"
    )
    checked = validate_diff(diff, allowed_paths=["app/db.py"])
    assert (checked.lines_added, checked.lines_removed) == (1, 1)


def test_a_git_header_that_agrees_with_the_file_headers_is_accepted() -> None:
    diff = "diff --git a/app/db.py b/app/db.py\nindex 1111111..2222222 100644\n" + GOOD_DIFF
    assert validate_diff(diff, allowed_paths=["app/db.py"]).files == ("app/db.py",)


def test_a_diff_outside_the_localisation_is_refused_by_path() -> None:
    """File scope is a check, not an instruction to the model."""
    elsewhere = GOOD_DIFF.replace("app/db.py", "app/secrets.py")
    with pytest.raises(ValidationError) as exc:
        validate_diff(elsewhere, allowed_paths=["app/db.py"])
    assert "app/secrets.py is not one of the localised candidate files" in str(exc.value)


def test_an_oversized_diff_is_refused() -> None:
    huge = "--- a/app/db.py\n+++ b/app/db.py\n@@ -1,1 +1,2 @@\n x\n" + "+y\n" * 40_000
    with pytest.raises(ValidationError) as exc:
        validate_diff(huge, allowed_paths=["app/db.py"])
    assert "byte limit" in str(exc.value)


def test_the_suite_is_chosen_from_the_files_not_from_the_model() -> None:
    assert suite_for(["app/db.py"]) == "pytest"
    assert suite_for(["cmd/main.go"]) == "go_test"
    assert suite_for(["README.md"]) is None
    assert repo_url("acme/payments") == "https://github.com/acme/payments.git"
    assert repo_url("ssh://git@host/x.git") == "ssh://git@host/x.git"


# --------------------------------------------------------------------------- #
# the node                                                                     #
# --------------------------------------------------------------------------- #


async def test_an_abstained_diagnosis_never_reaches_the_model() -> None:
    """A patch for a cause we did not establish is the failure mode, not a bonus."""
    node, db, patches, _evidence = build(router=FakeRouter(forbidden=True))

    assert await node(state(abstained=True)) == {}
    assert patches.rows == {}
    assert db.sandbox_rows == []


async def test_no_localised_file_means_no_patch() -> None:
    """localize_code ran and found nothing: a finding, not a gap."""
    node, _db, patches, evidence = build(router=FakeRouter(forbidden=True), paths=())

    assert await node(state()) == {}
    assert patches.rows == {}
    assert evidence.gaps == []


async def test_a_lost_localisation_handoff_is_a_gap_not_an_empty_result() -> None:
    """A checkpointed resume rebuilds deps, so the handoff does not survive it.

    Reporting that as "there is nothing to patch" would collapse "we could not
    look" into "we looked and found nothing".
    """
    node, _db, patches, evidence = build(router=FakeRouter(forbidden=True), resumed=True)

    result = await node(state())

    assert patches.rows == {}
    assert result["evidence_gaps"][0]["source"] == "code_localization"
    assert "resumed past it" in result["evidence_gaps"][0]["reason"]
    assert evidence.gaps, "the lost handoff is recorded as an unavailable source"


async def test_localize_code_marks_the_attempt_even_when_it_declines() -> None:
    """The two halves of the handoff must agree on what "not attempted" means.

    localize_code returning early is a decision this process made; only a lost
    checkpoint leaves the flag unset.
    """
    from aegis.agents.workflow import make_nodes

    deps = WorkflowDeps(
        settings=Settings(postgres_password="x"),
        db=FakeDB(),
        evidence=FakeEvidenceStore(),
        prometheus=None,
        neo4j=None,
        router=FakeRouter(forbidden=True),
        budget=BudgetGuard(
            max_wall_seconds=300, max_llm_calls=10, max_tool_calls=50, max_tokens=100_000
        ),
    )
    assert deps.localization_attempted is False

    assert await make_nodes(deps)["localize_code"](state()) == {}

    assert deps.localization_attempted is True


async def test_a_malformed_diff_is_recorded_and_never_executed() -> None:
    """Rejected, with the reason. Not silently dropped, and not run."""
    router = FakeRouter(proposal(diff="I would change the pool timeout to 5 seconds"))
    node, db, patches, _evidence = build(router=router)

    result = await node(state())

    assert result == {}
    assert patches.only.state is PatchState.REJECTED
    patch_id, to, reason = patches.transitions[0]
    assert to is PatchState.REJECTED
    assert "rejected before execution" in reason
    assert "unified diff" in reason
    # Nothing ran: no sandbox row, and no tool call to have produced one.
    assert db.sandbox_rows == []
    assert db.tool_calls == []


async def test_a_patch_touching_an_unlocalised_file_is_refused() -> None:
    router = FakeRouter(proposal(diff=GOOD_DIFF.replace("app/db.py", "app/secrets.py")))
    node, db, patches, _evidence = build(router=router)

    await node(state())

    assert patches.only.state is PatchState.REJECTED
    assert "app/secrets.py" in patches.transitions[0][2]
    assert db.tool_calls == []


async def test_only_resolvable_evidence_is_attached_to_a_patch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cited id this incident does not hold is dropped, not stored."""
    router = FakeRouter(proposal())
    node, _db, patches, _evidence = build(
        router=router,
        runs=[sandbox_result(id="sbx_repro", exit_code=1), sandbox_result(id="sbx_test")],
        monkeypatch=monkeypatch,
    )

    await node(state())

    assert patches.only.supporting_evidence == ["ev_01A"]


async def test_a_reproduced_failure_then_a_passing_suite_marks_the_patch_tested(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    router = FakeRouter(proposal())
    node, db, patches, _evidence = build(
        router=router,
        runs=[
            sandbox_result(id="sbx_repro", purpose="reproduce", exit_code=1),
            sandbox_result(id="sbx_test", exit_code=0),
        ],
        monkeypatch=monkeypatch,
    )

    result = await node(state())

    assert [t[1] for t in patches.transitions] == [
        PatchState.REPRODUCED, PatchState.TESTED
    ]
    assert patches.links == [
        ("pat_1", "sbx_repro", None),
        ("pat_1", None, "sbx_test"),
    ]
    # Both runs are in the database, and both are cited as evidence.
    assert [r["id"] for r in db.sandbox_rows] == ["sbx_repro", "sbx_test"]
    assert len(result["evidence_ids"]) == 2
    assert result["evidence_gaps"] == []
    assert [c["tool"] for c in db.tool_calls] == ["run_reproduction", "test_patch"]


async def test_a_failing_suite_rejects_the_patch_and_still_records_the_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A tested-and-failed remediation is part of the record, not an absence."""
    router = FakeRouter(proposal())
    node, db, patches, _evidence = build(
        router=router,
        runs=[
            sandbox_result(id="sbx_repro", purpose="reproduce", exit_code=1),
            sandbox_result(id="sbx_test", exit_code=1, stdout="2 failed"),
        ],
        monkeypatch=monkeypatch,
    )

    await node(state())

    assert patches.only.state is PatchState.REJECTED
    assert "exit 1" in patches.transitions[-1][2]
    row = db.sandbox_rows[-1]
    assert (row["id"], row["exit_code"], row["timed_out"]) == ("sbx_test", 1, False)
    assert patches.links[-1] == ("pat_1", None, "sbx_test")


async def test_a_stopped_sandbox_is_not_a_failing_test(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A killed run produced no verdict, so it never becomes one."""
    router = FakeRouter(proposal())
    node, db, patches, _evidence = build(
        router=router,
        runs=[
            sandbox_result(id="sbx_repro", purpose="reproduce", exit_code=1),
            sandbox_result(id="sbx_test", exit_code=None, timed_out=True, killed=True),
        ],
        monkeypatch=monkeypatch,
    )

    result = await node(state())

    assert patches.only.state is PatchState.REPRODUCED
    assert PatchState.REJECTED not in [t[1] for t in patches.transitions]
    assert "no test verdict" in patches.transitions[-1][2]
    # The run is still recorded, and the run row is what keeps the two apart.
    row = db.sandbox_rows[-1]
    assert (row["exit_code"], row["timed_out"], row["killed"]) == (None, True, True)
    assert result["evidence_gaps"], "a stopped run must leave an evidence gap"


async def test_the_same_diff_is_never_tested_twice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Dedup is on the content hash, so a replay costs no container."""
    router = FakeRouter(proposal())
    node, db, patches, _evidence = build(
        router=router,
        runs=[
            sandbox_result(id="sbx_repro", purpose="reproduce", exit_code=1),
            sandbox_result(id="sbx_test", exit_code=0),
        ],
        monkeypatch=monkeypatch,
    )

    await node(state())
    second = await node(state())

    assert second == {}
    assert len(patches.rows) == 1
    assert len(db.sandbox_rows) == 2          # from the first run only
    assert router.calls == 2                  # the model was asked twice
    assert len(db.tool_calls) == 2            # but nothing was executed twice


async def test_no_tool_boundary_means_recorded_but_untested() -> None:
    """Fail closed: an unauthorised execution is not an execution."""
    router = FakeRouter(proposal())
    node, db, patches, evidence = build(router=router, with_tools=False)

    result = await node(state())

    assert patches.only.state is PatchState.PROPOSED
    assert db.sandbox_rows == []
    assert result["evidence_gaps"][0]["source"] == "sandbox"
    assert evidence.gaps, "the unconsulted sandbox is recorded as a gap"


async def test_declining_to_patch_records_the_reason_and_proposes_nothing() -> None:
    router = FakeRouter(
        proposal(propose_patch=False, diff="", reason="the cause is in a config value")
    )
    node, db, patches, _evidence = build(router=router)

    assert await node(state()) == {}
    assert patches.rows == {}
    agent_runs = [
        args for query, args in db.statements if "INSERT INTO agent_runs" in query
    ]
    assert "the cause is in a config value" in agent_runs[0][8]
