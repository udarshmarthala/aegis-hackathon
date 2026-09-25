"""What this operator personally needs to do next.

The home page answers "is anything on fire"; this answers "is anything waiting
on me". Approvals about to expire come first, because a lapsed approval silently
un-does a decision someone already made and the work has to start again.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Query

from aegis.api.deps import ContainerDep, PrincipalDep
from aegis.core.logging import get_logger

log = get_logger(__name__)
router = APIRouter(prefix="/tasks", tags=["operations"])


@router.get("", summary="Work waiting on this operator")
async def my_tasks(
    principal: PrincipalDep,
    container: ContainerDep,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> dict[str, Any]:
    approvals = await container.approvals.pending(limit=limit)

    escalated = await container.db.fetch(
        """
        SELECT a.id, a.incident_id, a.action_type, a.resource_id, a.error,
               a.completed_at, i.title, i.severity
          FROM remediation_actions a
          JOIN incidents i ON i.id = a.incident_id
         WHERE a.state IN ('FAILED', 'ROLLED_BACK')
           AND a.completed_at > now() - interval '24 hours'
         ORDER BY a.completed_at DESC
         LIMIT $1
        """,
        limit,
    )

    blocked = await container.db.fetch(
        """
        SELECT id, title, severity, state, environment, updated_at
          FROM incidents
         WHERE state IN ('BLOCKED', 'ESCALATED')
         ORDER BY updated_at DESC
         LIMIT $1
        """,
        limit,
    )

    tasks: list[dict[str, Any]] = []
    for row in approvals:
        tasks.append(
            {
                "kind": "approval",
                "priority": 1,
                "title": f"Approve {row['action_type']} on {row['resource_id']}",
                "incident_id": row["incident_id"],
                "action_id": row["action_id"],
                "approval_id": row["approval_id"],
                "severity": row["incident_severity"],
                "due_at": row["expires_at"].isoformat(),
                "context": row["incident_title"],
            }
        )
    for row in escalated:
        tasks.append(
            {
                "kind": "escalation",
                "priority": 2,
                "title": f"{row['action_type']} did not succeed on {row['resource_id']}",
                "incident_id": row["incident_id"],
                "action_id": row["id"],
                "severity": row["severity"],
                "due_at": None,
                "context": row["error"] or row["title"],
            }
        )
    for row in blocked:
        tasks.append(
            {
                "kind": "blocked_incident",
                "priority": 3,
                "title": row["title"],
                "incident_id": row["id"],
                "severity": row["severity"],
                "due_at": None,
                "context": f"incident is {row['state']}",
            }
        )

    tasks.sort(key=lambda t: (t["priority"], t["due_at"] or "9999"))
    return {"operator": principal.uid, "items": tasks, "count": len(tasks)}
