"""The human decision surface.

An approval request must carry everything needed to decide without
reconstructing the investigation: the incident, the diagnosis, the exact
proposed change, the blast radius, the rollback plan, the verification criteria
and the evidence behind all of it. That is the whole point of the queue - a
human who has to go and find the context will approve on vibes instead.

Deciding is deliberately NOT the same as executing. A granted approval enqueues
work; the worker then re-runs the entire gate chain, because the world may have
moved since the operator looked at it. An approval is permission, not a
command.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field

from aegis.api.deps import ContainerDep, RequireApprover, RequireViewer
from aegis.core.errors import NotFoundError
from aegis.core.logging import get_logger
from aegis.persistence.jobs import JobQueue
from aegis.policy.tiers import profile_for

log = get_logger(__name__)
router = APIRouter(prefix="/approvals", tags=["governance"])


class DecisionRequest(BaseModel):
    decision: Annotated[str, Field(pattern="^(approved|rejected|more_evidence)$")]
    note: Annotated[str, Field(default="", max_length=2000)]


@router.get("", summary="Open approval requests with full decision context")
async def pending(
    _: RequireViewer,
    container: ContainerDep,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> dict[str, Any]:
    rows = await container.approvals.pending(limit=limit)
    items: list[dict[str, Any]] = []
    for row in rows:
        profile = profile_for_safe(row["action_type"])
        items.append(
            {
                "approval_id": row["approval_id"],
                "action_id": row["action_id"],
                "incident_id": row["incident_id"],
                "requested_at": row["requested_at"].isoformat(),
                "expires_at": row["expires_at"].isoformat(),
                "action": {
                    "type": row["action_type"],
                    "risk_tier": profile,
                    "resource_type": row["resource_type"],
                    "resource_id": row["resource_id"],
                    "service_id": row["service_id"],
                    "environment": row["environment"],
                    "reason": row["reason"],
                },
                "incident": {
                    "title": row["incident_title"],
                    "severity": row["incident_severity"],
                    "confidence": row["incident_confidence"],
                },
                "diagnosis": {
                    "statement": row["diagnosis_statement"],
                    "confidence": row["diagnosis_confidence"],
                },
                "blast_radius": row["blast_radius"],
                "rollback_plan": row["rollback_plan"],
                "verification_plan": row["verification_plan"],
                "expected_effect": row["expected_effect"],
                "supporting_evidence": list(row["supporting_evidence"] or []),
            }
        )
    return {"items": items, "count": len(items)}


def profile_for_safe(action_type: str) -> int:
    """Risk tier for a stored action-type string.

    An unrecognised value reports tier 3 rather than raising: an approval queue
    that 500s because one row holds a retired action type is worse than one
    that shows the safest possible classification.
    """
    from aegis.domain.enums import ActionType

    try:
        return int(profile_for(ActionType(action_type)).tier)
    except ValueError:
        log.warning("unknown action type in approvals queue", action_type=action_type)
        return 3


@router.get("/{approval_id}", summary="One approval request")
async def get_approval(
    approval_id: str, _: RequireViewer, container: ContainerDep
) -> dict[str, Any]:
    approval = await container.approvals.get(approval_id)
    if approval is None:
        raise NotFoundError("approval not found", context={"approval_id": approval_id})
    return {
        "id": approval.id,
        "action_id": approval.action_id,
        "incident_id": approval.incident_id,
        "requested_at": approval.requested_at.isoformat(),
        "expires_at": approval.expires_at.isoformat(),
        "decision": approval.decision,
        "decided_by": approval.decided_by,
        "decided_at": approval.decided_at.isoformat() if approval.decided_at else None,
        "note": approval.note,
    }


@router.post("/{approval_id}/decide", summary="Record a human decision")
async def decide(
    approval_id: str,
    body: DecisionRequest,
    principal: RequireApprover,
    container: ContainerDep,
) -> dict[str, Any]:
    """Record the decision, then enqueue work - never execute inline.

    Two reasons this does not call the executor directly. A remediation can take
    minutes, and holding an HTTP request open for it would time out and leave
    the operator unsure whether it ran. More importantly, the worker re-runs the
    full gate chain: the approval authorises the action, it does not bypass the
    checks that were never about authorisation in the first place.
    """
    approval = await container.approvals.decide(
        approval_id,
        decision=body.decision,  # type: ignore[arg-type]
        decided_by=principal.uid,
        note=body.note,
    )

    enqueued = False
    if approval.decision == "approved":
        queue = JobQueue(container.db)
        await queue.enqueue(
            kind="execute_action",
            incident_id=approval.incident_id,
            payload={"action_id": approval.action_id, "approval_id": approval.id},
        )
        enqueued = True
    elif approval.decision == "more_evidence":
        queue = JobQueue(container.db)
        await queue.enqueue(
            kind="investigate",
            incident_id=approval.incident_id,
            payload={"reason": "approver requested more evidence"},
        )
        enqueued = True

    log.info(
        "approval decided",
        approval_id=approval_id,
        decision=approval.decision,
        decided_by=principal.uid,
        work_enqueued=enqueued,
    )
    return {
        "id": approval.id,
        "action_id": approval.action_id,
        "decision": approval.decision,
        "decided_by": approval.decided_by,
        "work_enqueued": enqueued,
    }
