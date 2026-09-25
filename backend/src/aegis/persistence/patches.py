"""Patches, sandbox runs and deployment attempts.

Three tables in one module because they describe one story: a candidate change
was written, it was executed somewhere disposable, and something was - or was
not - deployed as a result. ``007_execution.sql`` created all three and nothing
ever wrote to them, so ``/debug`` and ``/deployments`` could only render an
empty state and the evaluation harness could only report ``patch_applies`` as
unmeasured.

The design points carry the same weight as in ``persistence.actions``:

* **Deduplication is a UNIQUE index, not an application check.** The same diff
  proposed twice for one incident collapses onto the existing row through
  ``patches_incident_diff_idx``. ``propose`` reports which happened, so a caller
  cannot mistake a replay for a second, independent fix.
* **Every number is computed here, never accepted.** ``diff_sha256``,
  ``lines_added`` and ``lines_removed`` are derived from the diff text itself. A
  model's claim about the size of its own patch has no route into the database.
* **Output is truncated at write time.** A runaway test suite can emit
  gigabytes; the excerpt columns are capped before the INSERT rather than
  trimmed afterwards by a cleanup job that may never run.
* **State changes are conditional on the expected current state**, so two
  workers racing a patch from TESTED to PROMOTED produce one winner and one
  ``DomainError`` instead of two promotions.

A sandbox run row distinguishes three outcomes that are routinely conflated:
the command ran and passed, the command ran and failed, and the sandbox never
reached a verdict at all. ``exit_code IS NULL`` - or ``timed_out`` /``killed`` -
is that third state, and it is never read as either of the first two.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Final

from aegis.core.errors import DomainError, NotFoundError, ValidationError
from aegis.core.ids import ACTION, new_id
from aegis.core.logging import get_logger
from aegis.persistence.db import Database

if TYPE_CHECKING:  # pragma: no cover - typing only
    # Imported for typing alone. A runtime import would run
    # ``aegis.execution.__init__``, which imports ``execution.service``, which
    # imports this module - a cycle that would break either import order.
    from aegis.execution.sandbox import SandboxResult

log = get_logger(__name__)

# Excerpt bound applied on the way into Postgres. The sandbox already truncates
# what it returns; this is the second, independent bound, because the column is
# what an unbounded log would actually fill.
MAX_EXCERPT_CHARS: Final = 32_000
# Rationale accumulates a line per state change (why a patch was rejected, why
# it was promoted). Bounded for the same reason.
MAX_RATIONALE_CHARS: Final = 8_000
# The bound on the stored diff, applied here rather than trusted to the caller.
# A patch that was *validated* is far smaller than this - the debugger refuses
# anything over 64 kB - so a real remediation is never touched. What this bounds
# is the other path: a malformed diff is recorded before it is rejected, and
# that text never went through the validator's size check.
MAX_DIFF_CHARS: Final = 200_000

# The CHECK constraint on sandbox_runs.purpose, restated so a bad value is a
# typed ValidationError at the boundary rather than a constraint violation
# surfacing from the driver as an opaque database error.
SANDBOX_PURPOSES: Final = frozenset(
    {"reproduce", "test_patch", "regression", "build", "static_check"}
)


class PatchState(StrEnum):
    """The CHECK-constrained lifecycle of ``remediation_patches.state``.

    PROMOTED is reachable only through ``PROMOTE_PATCH``, which deliberately has
    no registered executor. Nothing in this module can put a patch into an
    environment; it can only record that one was written, run and judged.
    """

    PROPOSED = "PROPOSED"
    REPRODUCED = "REPRODUCED"
    TESTED = "TESTED"
    REJECTED = "REJECTED"
    PROMOTED = "PROMOTED"


class DeploymentState(StrEnum):
    """The CHECK-constrained lifecycle of ``deployment_attempts.state``."""

    PENDING = "PENDING"
    IN_PROGRESS = "IN_PROGRESS"
    DEPLOYED = "DEPLOYED"
    VERIFIED = "VERIFIED"
    FAILED = "FAILED"
    ROLLED_BACK = "ROLLED_BACK"


def _scoped_id(prefix: str) -> str:
    """A prefixed ULID for a table ``core.ids`` declares no constant for.

    Mirrors how ``persistence.actions`` mints policy-decision ids: the body is
    still a time-sortable ULID, and ``core.ids`` remains the single place the
    domain-level prefixes are declared.
    """
    return f"{prefix}_{new_id(ACTION).split('_', 1)[1]}"


def _truncate(text: str, limit: int = MAX_EXCERPT_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n...[truncated {len(text) - limit} characters]"


def diff_sha256(diff: str) -> str:
    """The deduplication key. Computed from the diff, never supplied."""
    return hashlib.sha256(diff.encode()).hexdigest()


def diff_line_counts(diff: str) -> tuple[int, int]:
    """Added and removed line counts, read off the diff body.

    The ``+++``/``---`` file headers are excluded; counting them would inflate
    every patch by two lines per file and make the size figure in the UI wrong
    in a way nobody would notice.
    """
    added = 0
    removed = 0
    for line in diff.splitlines():
        if line.startswith(("+++", "---")):
            continue
        if line.startswith("+"):
            added += 1
        elif line.startswith("-"):
            removed += 1
    return added, removed


# --------------------------------------------------------------------------- #
# sandbox runs                                                                 #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class StoredSandboxRun:
    """One sandboxed execution as persisted."""

    id: str
    incident_id: str | None
    action_id: str | None
    purpose: str
    image: str
    repo: str | None
    base_ref: str | None
    patch_sha256: str | None
    command: str
    exit_code: int | None
    timed_out: bool
    killed: bool
    duration_ms: int | None
    stdout_excerpt: str
    stderr_excerpt: str
    artifacts: list[dict[str, Any]]
    resource_limits: dict[str, Any]
    network: str
    started_at: datetime
    finished_at: datetime | None

    @property
    def reached_verdict(self) -> bool:
        """Whether the command itself decided the outcome.

        False means the sandbox stopped the run - timeout or kill - so there is
        no exit code to interpret. "The sandbox could not run it" and "the tests
        failed" are different facts and this is where they part company.
        """
        return self.exit_code is not None and not self.timed_out and not self.killed

    @property
    def succeeded(self) -> bool:
        return self.reached_verdict and self.exit_code == 0


class SandboxRunRepository:
    """Every sandboxed execution, recorded whether or not it succeeded.

    A remediation that was tested and failed matters to the audit trail exactly
    as much as one that passed, so there is no filtered write path here: the
    repository takes whatever the runner returned.
    """

    __slots__ = ("_db",)

    def __init__(self, db: Database) -> None:
        self._db = db

    async def record(
        self,
        result: SandboxResult,
        *,
        incident_id: str | None = None,
        action_id: str | None = None,
        purpose: str | None = None,
    ) -> StoredSandboxRun:
        """Persist one ``SandboxResult``.

        ``purpose`` defaults to the purpose the run actually declared; passing a
        different one is allowed but validated, because the column is
        CHECK-constrained and a rejected INSERT here would lose the only record
        that the container ever ran.

        Idempotent on the run id, so a worker that crashes between running and
        recording does not double-count the execution on retry.
        """
        chosen = purpose or result.purpose
        if chosen not in SANDBOX_PURPOSES:
            raise ValidationError(
                f"unknown sandbox purpose {chosen!r}",
                context={"purpose": chosen, "sandbox_id": result.id},
            )

        row = await self._db.fetchrow(
            """
            INSERT INTO sandbox_runs
                (id, incident_id, action_id, purpose, image, repo, base_ref,
                 patch_sha256, command, exit_code, timed_out, killed, duration_ms,
                 stdout_excerpt, stderr_excerpt, artifacts, resource_limits,
                 network, started_at, finished_at)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,
                    $18,$19,$20)
            ON CONFLICT (id) DO NOTHING
            RETURNING *
            """,
            result.id, incident_id, action_id, chosen, result.image, result.repo,
            result.base_ref, result.patch_sha256, result.command, result.exit_code,
            result.timed_out, result.killed, result.duration_ms,
            _truncate(result.stdout), _truncate(result.stderr),
            list(result.artifacts), dict(result.resource_limits), result.network,
            result.started_at, result.finished_at,
        )
        if row is None:
            existing = await self.get(result.id)
            if existing is None:  # pragma: no cover - only if the row vanished
                raise DomainError(
                    "sandbox run insert conflicted but no existing row was found",
                    context={"sandbox_id": result.id},
                )
            return existing

        log.info(
            "sandbox run recorded",
            sandbox_id=result.id,
            incident_id=incident_id,
            purpose=chosen,
            exit_code=result.exit_code,
            timed_out=result.timed_out,
            killed=result.killed,
        )
        return self._row(row)

    async def get(self, run_id: str) -> StoredSandboxRun | None:
        row = await self._db.fetchrow("SELECT * FROM sandbox_runs WHERE id = $1", run_id)
        return self._row(row) if row else None

    async def for_incident(
        self, incident_id: str, *, limit: int = 50
    ) -> list[StoredSandboxRun]:
        rows = await self._db.fetch(
            """
            SELECT * FROM sandbox_runs
             WHERE incident_id = $1 ORDER BY started_at DESC LIMIT $2
            """,
            incident_id, min(limit, 200),
        )
        return [self._row(r) for r in rows]

    @staticmethod
    def _row(row: Any) -> StoredSandboxRun:
        return StoredSandboxRun(
            id=row["id"],
            incident_id=row["incident_id"],
            action_id=row["action_id"],
            purpose=row["purpose"],
            image=row["image"],
            repo=row["repo"],
            base_ref=row["base_ref"],
            patch_sha256=row["patch_sha256"],
            command=row["command"],
            exit_code=row["exit_code"],
            timed_out=bool(row["timed_out"]),
            killed=bool(row["killed"]),
            duration_ms=row["duration_ms"],
            stdout_excerpt=row["stdout_excerpt"] or "",
            stderr_excerpt=row["stderr_excerpt"] or "",
            artifacts=list(row["artifacts"] or []),
            resource_limits=dict(row["resource_limits"] or {}),
            network=row["network"],
            started_at=row["started_at"],
            finished_at=row["finished_at"],
        )


# --------------------------------------------------------------------------- #
# patches                                                                      #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class StoredPatch:
    """A candidate remediation as a concrete, reviewable change."""

    id: str
    incident_id: str
    repo: str
    base_ref: str
    summary: str
    rationale: str
    diff: str
    diff_sha256: str
    files_changed: list[str]
    lines_added: int
    lines_removed: int
    supporting_evidence: list[str]
    reproduction_run_id: str | None
    test_run_id: str | None
    state: PatchState
    pull_request_url: str | None
    created_at: datetime
    updated_at: datetime

    @property
    def is_terminal(self) -> bool:
        return self.state in (PatchState.REJECTED, PatchState.PROMOTED)


class PatchRepository:
    """Candidate code changes, tested or not, accepted or not."""

    __slots__ = ("_db",)

    def __init__(self, db: Database) -> None:
        self._db = db

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
        """Persist a candidate patch. Returns ``(patch, created)``.

        ``created`` is False when this incident already holds a patch with the
        same ``diff_sha256``. Callers must treat that as "this change was
        already proposed" and must not run it through the sandbox a second time;
        re-testing an identical diff burns a container to learn nothing.
        """
        if not diff.strip():
            raise ValidationError(
                "a patch needs a diff", context={"incident_id": incident_id}
            )
        # Truncated before the hash is taken, so diff_sha256 always describes
        # exactly the text that was stored. Hashing the original and storing a
        # shorter one would make the deduplication key a claim about something
        # the database does not hold.
        diff = _truncate(diff, MAX_DIFF_CHARS)
        digest = diff_sha256(diff)
        added, removed = diff_line_counts(diff)

        row = await self._db.fetchrow(
            """
            INSERT INTO remediation_patches
                (id, incident_id, repo, base_ref, summary, rationale, diff,
                 diff_sha256, files_changed, lines_added, lines_removed,
                 supporting_evidence, state)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13)
            ON CONFLICT (incident_id, diff_sha256) DO NOTHING
            RETURNING *
            """,
            _scoped_id("pat"), incident_id, repo, base_ref, summary[:1000],
            rationale[:MAX_RATIONALE_CHARS], diff, digest, list(files_changed),
            added, removed, list(supporting_evidence or []),
            PatchState.PROPOSED.value,
        )
        if row is not None:
            log.info(
                "patch proposed",
                patch_id=row["id"],
                incident_id=incident_id,
                files=len(files_changed),
                lines_added=added,
                lines_removed=removed,
            )
            return self._row(row), True

        existing = await self.by_diff(incident_id, digest)
        if existing is None:  # pragma: no cover - only if the row vanished
            raise DomainError(
                "patch insert conflicted but no existing row was found",
                context={"incident_id": incident_id, "diff_sha256": digest},
            )
        log.info(
            "identical patch already proposed for this incident",
            patch_id=existing.id,
            incident_id=incident_id,
            state=existing.state.value,
        )
        return existing, False

    async def get(self, patch_id: str) -> StoredPatch | None:
        row = await self._db.fetchrow(
            "SELECT * FROM remediation_patches WHERE id = $1", patch_id
        )
        return self._row(row) if row else None

    async def require(self, patch_id: str) -> StoredPatch:
        patch = await self.get(patch_id)
        if patch is None:
            raise NotFoundError("patch not found", context={"patch_id": patch_id})
        return patch

    async def by_diff(self, incident_id: str, digest: str) -> StoredPatch | None:
        row = await self._db.fetchrow(
            "SELECT * FROM remediation_patches WHERE incident_id = $1 AND diff_sha256 = $2",
            incident_id, digest,
        )
        return self._row(row) if row else None

    async def for_incident(self, incident_id: str, *, limit: int = 50) -> list[StoredPatch]:
        rows = await self._db.fetch(
            """
            SELECT * FROM remediation_patches
             WHERE incident_id = $1 ORDER BY created_at DESC LIMIT $2
            """,
            incident_id, min(limit, 100),
        )
        return [self._row(r) for r in rows]

    async def recent(self, *, limit: int = 50) -> list[StoredPatch]:
        rows = await self._db.fetch(
            "SELECT * FROM remediation_patches ORDER BY created_at DESC LIMIT $1",
            min(limit, 100),
        )
        return [self._row(r) for r in rows]

    async def link_runs(
        self,
        patch_id: str,
        *,
        reproduction_run_id: str | None = None,
        test_run_id: str | None = None,
    ) -> StoredPatch:
        """Attach the sandbox runs that judged this patch.

        COALESCE rather than assignment: a later test run must not erase the
        reproduction run that justified attempting a fix in the first place.
        """
        row = await self._db.fetchrow(
            """
            UPDATE remediation_patches
               SET reproduction_run_id = COALESCE($2, reproduction_run_id),
                   test_run_id = COALESCE($3, test_run_id),
                   updated_at = now()
             WHERE id = $1
            RETURNING *
            """,
            patch_id, reproduction_run_id, test_run_id,
        )
        if row is None:
            raise NotFoundError("patch not found", context={"patch_id": patch_id})
        return self._row(row)

    async def transition(
        self,
        patch_id: str,
        *,
        to: PatchState,
        expected: PatchState | None = None,
        reason: str = "",
        pull_request_url: str | None = None,
    ) -> StoredPatch:
        """Move a patch to a new state, optionally guarded on the current one.

        ``reason`` is appended to the rationale rather than replacing it: why a
        patch was rejected is part of the record, and the record of why it was
        written in the first place is not overwritten to make room for it.
        """
        row = await self._db.fetchrow(
            """
            UPDATE remediation_patches
               SET state = $2,
                   rationale = CASE
                       WHEN $4::text IS NULL OR $4 = '' THEN rationale
                       ELSE left(btrim(rationale || E'\\n' || $4), $6)
                   END,
                   pull_request_url = COALESCE($5, pull_request_url),
                   updated_at = now()
             WHERE id = $1 AND ($3::text IS NULL OR state = $3)
            RETURNING *
            """,
            patch_id, to.value, expected.value if expected else None,
            reason[:MAX_RATIONALE_CHARS] or None,
            pull_request_url[:500] if pull_request_url else None,
            MAX_RATIONALE_CHARS,
        )
        if row is None:
            current = await self.get(patch_id)
            if current is None:
                raise NotFoundError("patch not found", context={"patch_id": patch_id})
            want = expected.value if expected else "?"
            raise DomainError(
                f"patch is {current.state.value}, expected {want}",
                context={
                    "patch_id": patch_id,
                    "current_state": current.state.value,
                    "expected_state": expected.value if expected else None,
                },
            )
        log.info("patch state changed", patch_id=patch_id, state=to.value, reason=reason[:200])
        return self._row(row)

    @staticmethod
    def _row(row: Any) -> StoredPatch:
        return StoredPatch(
            id=row["id"],
            incident_id=row["incident_id"],
            repo=row["repo"],
            base_ref=row["base_ref"],
            summary=row["summary"],
            rationale=row["rationale"] or "",
            diff=row["diff"],
            diff_sha256=row["diff_sha256"],
            files_changed=list(row["files_changed"] or []),
            lines_added=int(row["lines_added"] or 0),
            lines_removed=int(row["lines_removed"] or 0),
            supporting_evidence=list(row["supporting_evidence"] or []),
            reproduction_run_id=row["reproduction_run_id"],
            test_run_id=row["test_run_id"],
            state=PatchState(row["state"]),
            pull_request_url=row["pull_request_url"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )


# --------------------------------------------------------------------------- #
# deployments                                                                  #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class StoredDeployment:
    """One deployment Aegis performed or observed."""

    id: str
    incident_id: str | None
    action_id: str | None
    patch_id: str | None
    environment: str
    service_id: str
    from_version: str | None
    to_version: str | None
    strategy: str
    state: DeploymentState
    verification_id: str | None
    detail: dict[str, Any]
    error: str | None
    started_at: datetime
    finished_at: datetime | None


class DeploymentRepository:
    """Deployment attempts, so "we deployed and it was healthy" is a record.

    ``execution.adapters.RuntimePortBridge`` reads these rows back when it needs
    a previous version to roll back to, so an unrecorded deployment is not only
    invisible in the UI - it removes the rollback target a future incident would
    have used.
    """

    __slots__ = ("_db",)

    def __init__(self, db: Database) -> None:
        self._db = db

    async def start(
        self,
        *,
        environment: str,
        service_id: str,
        incident_id: str | None = None,
        action_id: str | None = None,
        patch_id: str | None = None,
        from_version: str | None = None,
        to_version: str | None = None,
        strategy: str = "rolling",
        state: DeploymentState = DeploymentState.IN_PROGRESS,
        detail: dict[str, Any] | None = None,
    ) -> StoredDeployment:
        """Open an attempt before the change is made, not after.

        Written first so that a worker killed mid-deployment leaves an
        IN_PROGRESS row an operator can see, rather than no trace of the change
        that is now live.
        """
        if not service_id:
            raise ValidationError(
                "a deployment attempt needs a service",
                context={"environment": environment},
            )
        row = await self._db.fetchrow(
            """
            INSERT INTO deployment_attempts
                (id, incident_id, action_id, patch_id, environment, service_id,
                 from_version, to_version, strategy, state, detail)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)
            RETURNING *
            """,
            _scoped_id("dep"), incident_id, action_id, patch_id, environment,
            service_id, from_version, to_version, strategy, state.value,
            detail or {},
        )
        if row is None:  # pragma: no cover - a plain INSERT always returns
            raise DomainError(
                "deployment attempt was not recorded",
                context={"service_id": service_id, "environment": environment},
            )
        log.info(
            "deployment attempt started",
            deployment_id=row["id"],
            service_id=service_id,
            environment=environment,
            state=state.value,
        )
        return self._row(row)

    async def finish(
        self,
        deployment_id: str,
        *,
        state: DeploymentState,
        verification_id: str | None = None,
        error: str | None = None,
        from_version: str | None = None,
        to_version: str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> StoredDeployment:
        """Close an attempt with its real outcome.

        The versions are supplied here rather than at ``start`` because they are
        only known once the runtime has been observed: a proposal naming a
        target version is a request, and recording it as what happened would be
        a claim nobody checked.

        Guarded on ``finished_at IS NULL`` so a late-arriving verification
        cannot rewrite an attempt that already reached a terminal state.
        """
        row = await self._db.fetchrow(
            """
            UPDATE deployment_attempts
               SET state = $2,
                   verification_id = COALESCE($3, verification_id),
                   error = COALESCE($4, error),
                   from_version = COALESCE($6, from_version),
                   to_version = COALESCE($7, to_version),
                   detail = detail || $5::jsonb,
                   finished_at = now()
             WHERE id = $1 AND finished_at IS NULL
            RETURNING *
            """,
            deployment_id, state.value, verification_id,
            error[:2000] if error else None, detail or {},
            from_version, to_version,
        )
        if row is None:
            current = await self.get(deployment_id)
            if current is None:
                raise NotFoundError(
                    "deployment attempt not found",
                    context={"deployment_id": deployment_id},
                )
            raise DomainError(
                f"deployment attempt already finished as {current.state.value}",
                context={
                    "deployment_id": deployment_id,
                    "current_state": current.state.value,
                },
            )
        log.info(
            "deployment attempt finished",
            deployment_id=deployment_id,
            state=state.value,
            verification_id=verification_id,
        )
        return self._row(row)

    async def get(self, deployment_id: str) -> StoredDeployment | None:
        row = await self._db.fetchrow(
            "SELECT * FROM deployment_attempts WHERE id = $1", deployment_id
        )
        return self._row(row) if row else None

    async def for_incident(
        self, incident_id: str, *, limit: int = 50
    ) -> list[StoredDeployment]:
        rows = await self._db.fetch(
            """
            SELECT * FROM deployment_attempts
             WHERE incident_id = $1 ORDER BY started_at DESC LIMIT $2
            """,
            incident_id, min(limit, 200),
        )
        return [self._row(r) for r in rows]

    @staticmethod
    def _row(row: Any) -> StoredDeployment:
        return StoredDeployment(
            id=row["id"],
            incident_id=row["incident_id"],
            action_id=row["action_id"],
            patch_id=row["patch_id"],
            environment=row["environment"],
            service_id=row["service_id"],
            from_version=row["from_version"],
            to_version=row["to_version"],
            strategy=row["strategy"],
            state=DeploymentState(row["state"]),
            verification_id=row["verification_id"],
            detail=dict(row["detail"] or {}),
            error=row["error"],
            started_at=row["started_at"],
            finished_at=row["finished_at"],
        )


__all__ = [
    "MAX_EXCERPT_CHARS",
    "SANDBOX_PURPOSES",
    "DeploymentRepository",
    "DeploymentState",
    "PatchRepository",
    "PatchState",
    "SandboxRunRepository",
    "StoredDeployment",
    "StoredPatch",
    "StoredSandboxRun",
    "diff_line_counts",
    "diff_sha256",
]
