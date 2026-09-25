"""Remediation actions: what was proposed, what policy said, what happened.

The detail endpoint is the evidence an operator needs to disagree with Aegis.
It returns the proposal, every policy decision with its full gate list, the
verification claims with their before/after numbers, and the audit trail - not
a summary of those things. An approval surface that asks a human to trust a
verdict they cannot inspect is theatre.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Query
from pydantic import BaseModel

from aegis.api.deps import ContainerDep, RequireViewer
from aegis.core.errors import NotFoundError
from aegis.core.logging import get_logger
from aegis.domain.enums import ActionState
from aegis.execution.registry import executable_action_types
from aegis.policy.tiers import profile_for

log = get_logger(__name__)
router = APIRouter(prefix="/actions", tags=["remediation"])


class ActionSummary(BaseModel):
    id: str
    incident_id: str
    action_type: str
    state: str
    risk_tier: int
    executable: bool
    resource_id: str
    service_id: str | None
    environment: str
    reason: str
    created_at: str
    executed_at: str | None
    completed_at: str | None
    error: str | None


def _summarise(row: Any) -> ActionSummary:
    profile = profile_for(row.action_type)
    return ActionSummary(
        id=row.id,
        incident_id=row.incident_id,
        action_type=row.action_type.value,
        state=row.state.value,
        risk_tier=int(profile.tier),
        # Whether an executor exists at all. Tier-3 types are representable so
        # policy can name them, but nothing here can run them.
        executable=row.action_type in executable_action_types(),
        resource_id=row.resource_id,
        service_id=row.service_id,
        environment=row.environment,
        reason=row.reason,
        created_at=row.created_at.isoformat(),
        executed_at=row.executed_at.isoformat() if row.executed_at else None,
        completed_at=row.completed_at.isoformat() if row.completed_at else None,
        error=row.error,
    )


@router.get("", summary="List remediation actions")
async def list_actions(
    _: RequireViewer,
    container: ContainerDep,
    incident_id: Annotated[str | None, Query()] = None,
    state: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> dict[str, Any]:
    wanted: ActionState | None = None
    if state is not None:
        try:
            wanted = ActionState(state)
        except ValueError as exc:
            raise NotFoundError(
                f"unknown action state {state!r}",
                context={"known": [s.value for s in ActionState]},
            ) from exc

    if incident_id:
        rows = await container.actions.for_incident(incident_id, limit=limit)
        if wanted is not None:
            rows = [r for r in rows if r.state is wanted]
    else:
        rows = await container.actions.recent(state=wanted, limit=limit)

    return {"items": [_summarise(r).model_dump() for r in rows], "count": len(rows)}


@router.get("/{action_id}", summary="One action with its full decision trail")
async def get_action(
    action_id: str, _: RequireViewer, container: ContainerDep
) -> dict[str, Any]:
    action = await container.actions.require(action_id)
    decisions = await container.actions.decisions_for(action_id)
    verifications = await container.verification_store.for_action(action_id)
    approval = await container.approvals.open_for_action(action_id)
    granted = await container.approvals.granted_for_action(action_id)
    audit = await container.audit.for_incident(action.incident_id, limit=200)

    claims: list[dict[str, Any]] = []
    if verifications:
        claims = await container.verification_store.claims_for(verifications[0]["id"])

    return {
        "action": _summarise(action).model_dump(),
        "arguments": action.arguments,
        "supporting_evidence": action.supporting_evidence,
        "expected_effect": action.expected_effect,
        "blast_radius": action.blast_radius,
        "rollback_plan": action.rollback_plan,
        "verification_plan": action.verification_plan,
        "idempotency_key": action.idempotency_key,
        "result": action.result,
        # Every gate, not only the decisive one: an operator clearing one
        # blocker should see the next immediately.
        "policy_decisions": [
            {
                "effect": d["effect"],
                "risk_tier": int(d["risk_tier"]),
                "matched_rule": d["matched_rule"],
                "reasons": list(d["reasons"]),
                "gates": d["gates"],
                "policy_version": d["policy_version"],
                "context_snapshot": d["context_snapshot"],
                "decided_at": d["decided_at"].isoformat(),
            }
            for d in decisions
        ],
        "verifications": [
            {**v, "started_at": v["started_at"].isoformat(),
             "completed_at": v["completed_at"].isoformat()}
            for v in verifications
        ],
        "verification_claims": [
            {**c, "observed_at": c["observed_at"].isoformat()} for c in claims
        ],
        "approval": (
            {
                "id": approval.id,
                "state": "pending",
                "expires_at": approval.expires_at.isoformat(),
            }
            if approval
            else (
                {
                    "id": granted.id,
                    "state": granted.decision,
                    "decided_by": granted.decided_by,
                    "decided_at": granted.decided_at.isoformat()
                    if granted.decided_at
                    else None,
                    "note": granted.note,
                }
                if granted
                else None
            )
        ),
        "audit": [
            {
                "event_type": a.event_type,
                "actor": a.actor,
                "actor_type": a.actor_type,
                "detail": a.detail,
                "created_at": a.created_at.isoformat(),
            }
            for a in audit
            if a.resource_id == action_id or a.event_type.startswith("action.")
        ],
    }


@router.get("/{action_id}/leases", summary="Concurrency state for an action target")
async def action_leases(
    action_id: str, _: RequireViewer, container: ContainerDep
) -> dict[str, Any]:
    action = await container.actions.require(action_id)
    active = await container.leases.active(limit=200)
    mine = [
        {
            "id": lease.id,
            "resource_type": lease.resource_type,
            "resource_id": lease.resource_id,
            "holder": lease.holder,
            "expires_at": lease.expires_at.isoformat(),
        }
        for lease in active
        if lease.resource_id == action.resource_id
    ]
    return {"resource_id": action.resource_id, "active_leases": mine}
