"""Investigation transparency: what the agents did, and with what.

This is the surface that makes an AI system auditable rather than magical. It
exposes agent runs, tool calls, state transitions, budget consumption and the
evidence each step produced.

It deliberately does NOT expose model chain-of-thought. What an operator needs
is the operational record - which agent ran, what it queried, what came back,
what it concluded and what that conclusion rests on. Reasoning traces are
neither reliable as explanation nor safe to surface.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Query

from aegis.api.deps import ContainerDep, DbDep, RequireViewer
from aegis.core.logging import get_logger

log = get_logger(__name__)
router = APIRouter(prefix="/investigations", tags=["investigations"])


@router.get("", summary="Recent investigation runs")
async def list_investigations(
    _: RequireViewer,
    db: DbDep,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> dict[str, Any]:
    rows = await db.fetch(
        """
        SELECT i.id                AS incident_id,
               i.title,
               i.severity,
               i.state,
               i.environment,
               i.confidence,
               i.created_at,
               i.updated_at,
               count(r.id)                                   AS agent_runs,
               count(*) FILTER (WHERE r.status = 'failed')    AS failed_runs,
               max(r.finished_at)                            AS last_activity,
               coalesce(sum(r.duration_ms), 0)               AS total_duration_ms
          FROM incidents i
          LEFT JOIN agent_runs r ON r.incident_id = i.id
         GROUP BY i.id
         ORDER BY i.created_at DESC
         LIMIT $1
        """,
        min(limit, 200),
    )
    return {
        "items": [
            {
                "incident_id": r["incident_id"],
                "title": r["title"],
                "severity": r["severity"],
                "state": r["state"],
                "environment": r["environment"],
                "confidence": r["confidence"],
                "agent_runs": int(r["agent_runs"]),
                "failed_runs": int(r["failed_runs"]),
                "total_duration_ms": int(r["total_duration_ms"]),
                "created_at": r["created_at"].isoformat(),
                "last_activity": (
                    r["last_activity"].isoformat() if r["last_activity"] else None
                ),
            }
            for r in rows
        ],
        "count": len(rows),
    }


@router.get("/{incident_id}/runs", summary="Agent runs for one incident")
async def agent_runs(
    incident_id: str,
    _: RequireViewer,
    db: DbDep,
) -> dict[str, Any]:
    rows = await db.fetch(
        """
        SELECT id, agent_role, status, model, provider, prompt_version, task,
               result_summary, evidence_ids, duration_ms, error,
               started_at, finished_at
          FROM agent_runs
         WHERE incident_id = $1
         ORDER BY started_at ASC
         LIMIT 500
        """,
        incident_id,
    )
    return {
        "incident_id": incident_id,
        "items": [
            {
                "id": r["id"],
                "agent_role": r["agent_role"],
                "status": r["status"],
                "model": r["model"],
                "provider": r["provider"],
                "prompt_version": r["prompt_version"],
                "task": r["task"],
                "summary": r["result_summary"],
                "evidence_ids": list(r["evidence_ids"] or []),
                "duration_ms": r["duration_ms"],
                "error": r["error"],
                "started_at": r["started_at"].isoformat() if r["started_at"] else None,
                "finished_at": r["finished_at"].isoformat() if r["finished_at"] else None,
            }
            for r in rows
        ],
        "count": len(rows),
    }


@router.get("/{incident_id}/tools", summary="Tool calls for one incident")
async def tool_calls(
    incident_id: str,
    _: RequireViewer,
    db: DbDep,
    limit: Annotated[int, Query(ge=1, le=500)] = 200,
) -> dict[str, Any]:
    rows = await db.fetch(
        """
        SELECT id, agent_run_id, tool_name, status, duration_ms,
               arguments, error, created_at
          FROM tool_calls
         WHERE incident_id = $1
         ORDER BY created_at ASC
         LIMIT $2
        """,
        incident_id, min(limit, 500),
    )
    return {
        "incident_id": incident_id,
        "items": [
            {
                "id": r["id"],
                "agent_run_id": r["agent_run_id"],
                "tool_name": r["tool_name"],
                "status": r["status"],
                "duration_ms": r["duration_ms"],
                "arguments": r["arguments"],
                "error": r["error"],
                "created_at": r["created_at"].isoformat(),
            }
            for r in rows
        ],
        "count": len(rows),
    }


@router.get("/{incident_id}/gaps", summary="Sources that could not be consulted")
async def evidence_gaps(
    incident_id: str,
    _: RequireViewer,
    container: ContainerDep,
) -> dict[str, Any]:
    """Gaps are a first-class result, not an absence.

    "Prometheus was unreachable" and "no latency anomaly exists" are opposite
    conclusions, and this endpoint exists so the UI can never conflate them.
    """
    items = await container.evidence.list_for_incident(incident_id, limit=500)
    gaps = [e for e in items if e.is_gap]
    return {
        "incident_id": incident_id,
        "items": [
            {
                "id": g.id,
                "source": g.source,
                "source_type": g.source_type.value,
                "summary": g.summary,
                "reason": g.structured_value.get("reason", ""),
                "retrieved_at": g.retrieved_at.isoformat(),
            }
            for g in gaps
        ],
        "count": len(gaps),
        "usable_evidence_count": len(items) - len(gaps),
    }
