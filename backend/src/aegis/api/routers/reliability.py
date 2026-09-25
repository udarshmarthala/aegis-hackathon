"""Reliability: recurring failures, verification outcomes and operational trend.

Everything here is derived from what actually happened - verified remediations,
recorded verdicts, real incident counts. Nothing on this surface is estimated,
and no metric is shown that Aegis cannot substantiate from its own records.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Query

from aegis.api.deps import ContainerDep, DbDep, RequireViewer
from aegis.core.errors import SourceUnavailable
from aegis.core.logging import get_logger

log = get_logger(__name__)
router = APIRouter(prefix="/reliability", tags=["reliability"])


@router.get("/summary", summary="Operational summary over a window")
async def summary(
    _: RequireViewer,
    db: DbDep,
    days: Annotated[int, Query(ge=1, le=90)] = 7,
) -> dict[str, Any]:
    incidents = await db.fetchrow(
        """
        SELECT count(*)                                              AS total,
               count(*) FILTER (WHERE state = 'RESOLVED')            AS resolved,
               count(*) FILTER (WHERE state NOT IN ('RESOLVED'))     AS open,
               count(*) FILTER (WHERE severity = 'P1')               AS p1,
               avg(EXTRACT(EPOCH FROM (resolved_at - created_at)))
                   FILTER (WHERE resolved_at IS NOT NULL)            AS mttr_seconds
          FROM incidents
         WHERE created_at > now() - make_interval(days => $1)
        """,
        days,
    )
    actions = await db.fetchrow(
        """
        SELECT count(*)                                          AS proposed,
               count(*) FILTER (WHERE state = 'SUCCESS')          AS succeeded,
               count(*) FILTER (WHERE state = 'ROLLED_BACK')      AS rolled_back,
               count(*) FILTER (WHERE state = 'FAILED')           AS failed,
               count(*) FILTER (WHERE state = 'BLOCKED')          AS blocked,
               count(*) FILTER (WHERE state = 'HUMAN_REQUIRED')   AS awaiting_human
          FROM remediation_actions
         WHERE created_at > now() - make_interval(days => $1)
        """,
        days,
    )
    verdicts = await db.fetch(
        """
        SELECT verdict, count(*) AS n
          FROM verification_runs
         WHERE completed_at > now() - make_interval(days => $1)
         GROUP BY verdict
        """,
        days,
    )
    if incidents is None or actions is None:
        # An aggregate over an empty table still returns a row, so a missing
        # row means the query did not come back at all. That is a source
        # outage, and reporting zeros here would render it as a quiet,
        # healthy window - exactly the collapse the contract forbids.
        raise SourceUnavailable(
            "reliability aggregates returned no row",
            context={"window_days": days},
        )

    return {
        "window_days": days,
        "incidents": {
            "total": int(incidents["total"] or 0),
            "resolved": int(incidents["resolved"] or 0),
            "open": int(incidents["open"] or 0),
            "p1": int(incidents["p1"] or 0),
            # None, not zero: no resolved incident in the window means MTTR is
            # undefined, and rendering it as zero would look like perfection.
            "mttr_seconds": (
                float(incidents["mttr_seconds"])
                if incidents["mttr_seconds"] is not None
                else None
            ),
        },
        "actions": {
            key: int(actions[key] or 0)
            for key in (
                "proposed", "succeeded", "rolled_back",
                "failed", "blocked", "awaiting_human",
            )
        },
        "verification": {r["verdict"]: int(r["n"]) for r in verdicts},
    }


def _iso(value: Any) -> str | None:
    """Render a timestamp, or None when the pattern carries none.

    None is preserved rather than coerced to a string: a pattern with no
    recorded window is not the same as one starting at the epoch.
    """
    return value.isoformat() if value is not None else None


@router.get("/recurring", summary="Failures that keep coming back")
async def recurring(
    _: RequireViewer,
    container: ContainerDep,
    days: Annotated[int, Query(ge=7, le=365)] = 90,
    min_occurrences: Annotated[int, Query(ge=2, le=50)] = 2,
) -> dict[str, Any]:
    """Recurrence is computed from verified incident memory only.

    An unverified hypothesis that happened to repeat is not a recurring failure,
    it is a repeated guess, and treating the two alike would send an operator
    chasing a pattern that does not exist.
    """
    if container.memory_recall is None:
        return {
            "available": False,
            "reason": "incident memory is not configured",
            "items": [],
        }
    patterns = await container.memory_recall.recurring_patterns(
        window_days=days, min_occurrences=min_occurrences, limit=50
    )
    return {
        "available": True,
        "window_days": days,
        "items": [
            {
                "fingerprint": getattr(p, "fingerprint", ""),
                "occurrences": getattr(p, "occurrences", 0),
                "services": list(getattr(p, "services", []) or []),
                "cause_category": getattr(p, "cause_category", ""),
                "symptom": getattr(p, "symptom", ""),
                "first_seen": _iso(getattr(p, "first_seen", None)),
                "last_seen": _iso(getattr(p, "last_seen", None)),
            }
            for p in patterns
        ],
        "count": len(patterns),
    }


@router.get("/services", summary="Per-service incident load")
async def by_service(
    _: RequireViewer,
    db: DbDep,
    days: Annotated[int, Query(ge=1, le=365)] = 30,
) -> dict[str, Any]:
    rows = await db.fetch(
        """
        SELECT service                                     AS service_id,
               count(*)                                    AS incidents,
               count(*) FILTER (WHERE severity = 'P1')     AS p1,
               max(created_at)                             AS last_incident
          FROM incidents, unnest(affected_services) AS service
         WHERE created_at > now() - make_interval(days => $1)
         GROUP BY service
         ORDER BY incidents DESC
         LIMIT 100
        """,
        days,
    )
    return {
        "window_days": days,
        "items": [
            {
                "service_id": r["service_id"],
                "incidents": int(r["incidents"]),
                "p1": int(r["p1"]),
                "last_incident": r["last_incident"].isoformat(),
            }
            for r in rows
        ],
    }
