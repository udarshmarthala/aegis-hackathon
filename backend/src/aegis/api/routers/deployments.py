"""Deployments Aegis performed or observed.

A record rather than a claim: "we deployed to staging and it was healthy" is
only meaningful if the deployment and its verification are both stored and both
inspectable. Each attempt links to the action that caused it, the patch it
carried and the verification that judged it.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Query

from aegis.api.deps import ContainerDep, DbDep, RequireViewer
from aegis.core.logging import get_logger

log = get_logger(__name__)
router = APIRouter(prefix="/deployments", tags=["deployments"])



def _outcome(exit_code: int | None) -> bool | None:
    """Map a sandbox exit code onto pass / fail / not-run.

    ``None`` means no run is linked to this patch yet, which a caller must be
    able to tell apart from a run that happened and failed.
    """
    if exit_code is None:
        return None
    return exit_code == 0

@router.get("", summary="Deployment attempts")
async def list_deployments(
    _: RequireViewer,
    db: DbDep,
    environment: Annotated[str | None, Query(max_length=64)] = None,
    service_id: Annotated[str | None, Query(max_length=200)] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> dict[str, Any]:
    rows = await db.fetch(
        """
        SELECT d.id, d.incident_id, d.action_id, d.patch_id, d.environment,
               d.service_id, d.from_version, d.to_version, d.strategy, d.state,
               d.verification_id, d.error, d.started_at, d.finished_at,
               v.verdict AS verification_verdict
          FROM deployment_attempts d
          LEFT JOIN verification_runs v ON v.id = d.verification_id
         WHERE ($2::text IS NULL OR d.environment = $2)
           AND ($3::text IS NULL OR d.service_id = $3)
         ORDER BY d.started_at DESC
         LIMIT $1
        """,
        min(limit, 200), environment, service_id,
    )
    return {
        "items": [
            {
                "id": r["id"],
                "incident_id": r["incident_id"],
                "action_id": r["action_id"],
                "patch_id": r["patch_id"],
                "environment": r["environment"],
                "service_id": r["service_id"],
                "from_version": r["from_version"],
                "to_version": r["to_version"],
                "strategy": r["strategy"],
                "state": r["state"],
                "verification_verdict": r["verification_verdict"],
                "error": r["error"],
                "started_at": r["started_at"].isoformat(),
                "finished_at": (
                    r["finished_at"].isoformat() if r["finished_at"] else None
                ),
            }
            for r in rows
        ],
        "count": len(rows),
    }


@router.get("/patches", summary="Candidate remediation patches")
async def list_patches(
    _: RequireViewer,
    db: DbDep,
    incident_id: Annotated[str | None, Query(max_length=64)] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> dict[str, Any]:
    """Proposed code changes, tested or not.

    A patch that was generated, tested and rejected is as much a part of the
    record as one that shipped - hiding rejected attempts would make the system
    look more decisive than it was.
    """
    rows = await db.fetch(
        """
        SELECT p.id, p.incident_id, p.repo, p.base_ref, p.summary, p.rationale,
               p.diff_sha256, p.files_changed, p.lines_added, p.lines_removed,
               p.state, p.pull_request_url, p.created_at,
               rr.exit_code AS reproduction_exit_code,
               tr.exit_code AS test_exit_code
          FROM remediation_patches p
          LEFT JOIN sandbox_runs rr ON rr.id = p.reproduction_run_id
          LEFT JOIN sandbox_runs tr ON tr.id = p.test_run_id
         WHERE ($2::text IS NULL OR p.incident_id = $2)
         ORDER BY p.created_at DESC
         LIMIT $1
        """,
        min(limit, 100), incident_id,
    )
    return {
        "items": [
            {
                "id": r["id"],
                "incident_id": r["incident_id"],
                "repo": r["repo"],
                "base_ref": r["base_ref"],
                "summary": r["summary"],
                "rationale": r["rationale"],
                "files_changed": list(r["files_changed"] or []),
                "lines_added": r["lines_added"],
                "lines_removed": r["lines_removed"],
                "state": r["state"],
                "pull_request_url": r["pull_request_url"],
                # Tri-state, not boolean. A NULL exit code means the run never
                # happened; comparing it to 0 yields False and renders "never
                # run" identically to "ran and failed". Those are opposite
                # facts about a patch, and collapsing them is the read-side
                # form of the "no evidence found" versus "source unavailable"
                # conflation (CLAUDE.md invariant 6).
                "reproduced": _outcome(r["reproduction_exit_code"]),
                "tests_passed": _outcome(r["test_exit_code"]),
                "created_at": r["created_at"].isoformat(),
            }
            for r in rows
        ],
        "count": len(rows),
    }


@router.get("/patches/{patch_id}/diff", summary="The exact diff of one patch")
async def patch_diff(
    patch_id: str, _: RequireViewer, db: DbDep
) -> dict[str, Any]:
    row = await db.fetchrow(
        "SELECT id, repo, base_ref, diff, diff_sha256, state FROM "
        "remediation_patches WHERE id = $1",
        patch_id,
    )
    if row is None:
        return {"found": False, "id": patch_id}
    return {
        "found": True,
        "id": row["id"],
        "repo": row["repo"],
        "base_ref": row["base_ref"],
        "diff": row["diff"],
        "diff_sha256": row["diff_sha256"],
        "state": row["state"],
    }


@router.get("/sandbox-runs", summary="Sandboxed executions")
async def sandbox_runs(
    _: RequireViewer,
    db: DbDep,
    incident_id: Annotated[str | None, Query(max_length=64)] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> dict[str, Any]:
    rows = await db.fetch(
        """
        SELECT id, incident_id, action_id, purpose, image, repo, base_ref,
               command, exit_code, timed_out, killed, duration_ms,
               stdout_excerpt, stderr_excerpt, network, resource_limits,
               started_at, finished_at
          FROM sandbox_runs
         WHERE ($2::text IS NULL OR incident_id = $2)
         ORDER BY started_at DESC
         LIMIT $1
        """,
        min(limit, 100), incident_id,
    )
    return {
        "items": [
            {
                "id": r["id"],
                "incident_id": r["incident_id"],
                "action_id": r["action_id"],
                "purpose": r["purpose"],
                "image": r["image"],
                "repo": r["repo"],
                "base_ref": r["base_ref"],
                "command": r["command"],
                "exit_code": r["exit_code"],
                "timed_out": r["timed_out"],
                "killed": r["killed"],
                "duration_ms": r["duration_ms"],
                "stdout_excerpt": r["stdout_excerpt"],
                "stderr_excerpt": r["stderr_excerpt"],
                "network": r["network"],
                "resource_limits": r["resource_limits"],
                "started_at": r["started_at"].isoformat(),
                "finished_at": (
                    r["finished_at"].isoformat() if r["finished_at"] else None
                ),
            }
            for r in rows
        ],
        "count": len(rows),
    }


@router.get("/environments", summary="Environments Aegis can see")
async def environments(_: RequireViewer, container: ContainerDep) -> dict[str, Any]:
    return {
        "current": container.settings.aegis_environment_name,
        "adapter": container.settings.workload_adapter,
        "runtime_available": bool(
            container.runtime is not None and container.runtime.available
        ),
        "is_production": container.settings.is_production,
    }
