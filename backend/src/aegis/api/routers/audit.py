"""The audit trail.

Append-only and filterable. The question this surface exists to answer is "did
a person authorise this, and when?", which is why ``actor_type`` is a first-class
filter rather than a detail buried in a payload.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Query

from aegis.api.deps import ContainerDep, RequireViewer
from aegis.core.logging import get_logger

log = get_logger(__name__)
router = APIRouter(prefix="/audit", tags=["governance"])


def _render(record: Any) -> dict[str, Any]:
    return {
        "id": record.id,
        "incident_id": record.incident_id,
        "actor": record.actor,
        "actor_type": record.actor_type,
        "event_type": record.event_type,
        "resource_type": record.resource_type,
        "resource_id": record.resource_id,
        "detail": record.detail,
        "correlation_id": record.correlation_id,
        "created_at": record.created_at.isoformat(),
    }


@router.get("", summary="Audit events, newest first")
async def recent(
    _: RequireViewer,
    container: ContainerDep,
    event_type: Annotated[str | None, Query(max_length=64)] = None,
    actor_type: Annotated[str | None, Query(pattern="^(human|agent|system)$")] = None,
    correlation_id: Annotated[str | None, Query(max_length=64)] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> dict[str, Any]:
    records = await container.audit.recent(
        limit=limit,
        event_type=event_type,
        actor_type=actor_type,
        correlation_id=correlation_id,
    )
    return {
        "items": [_render(r) for r in records],
        "count": len(records),
        # Non-zero means the trail has holes and any completeness claim about it
        # is now qualified. Surfaced rather than hidden.
        "write_failures": container.audit.write_failures,
    }


@router.get("/incident/{incident_id}", summary="Full audit trail for one incident")
async def for_incident(
    incident_id: str,
    _: RequireViewer,
    container: ContainerDep,
    limit: Annotated[int, Query(ge=1, le=1000)] = 500,
) -> dict[str, Any]:
    records = await container.audit.for_incident(incident_id, limit=limit)
    human_decisions = [r for r in records if r.actor_type == "human"]
    return {
        "incident_id": incident_id,
        "items": [_render(r) for r in records],
        "count": len(records),
        "human_decisions": len(human_decisions),
        "autonomous_actions": len(
            [r for r in records if r.event_type == "action.execution_started"]
        )
        - len(human_decisions),
    }
