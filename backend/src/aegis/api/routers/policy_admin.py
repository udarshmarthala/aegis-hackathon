"""Governance surface: autonomy posture, kill switches, action registry.

Everything that changes safety posture requires ADMIN and is audited. The
registry endpoint exists so operators can see the real tool and action boundary
rather than trusting documentation (UX spec section 56).
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from aegis.api.deps import RequireAdmin, RequireViewer, SettingsDep
from aegis.core.logging import get_logger
from aegis.domain.enums import ActionType
from aegis.policy.engine import POLICY_VERSION
from aegis.policy.store import PolicyStore
from aegis.policy.tiers import profile_for

log = get_logger(__name__)
router = APIRouter(prefix="/policy", tags=["governance"])

_VALID_SCOPES = {"global", "environment", "action_type", "service"}


@router.get("", summary="Current autonomy posture and policy configuration")
async def get_policy(_: RequireViewer, settings: SettingsDep, request: Request) -> dict[str, Any]:
    store = PolicyStore(request.app.state.db)
    state = await store.load_kill_switches()
    return {
        "policy_version": POLICY_VERSION,
        "autonomy": {
            "enabled": settings.autonomy_enabled,
            "mode": settings.autonomy_mode.value,
            "allowed_tiers": sorted(settings.allowed_tiers),
            "max_actions_per_hour": settings.autonomy_max_actions_per_hour,
        },
        "approval_ttl_seconds": settings.approval_ttl_seconds,
        "kill_switches": {
            "any_engaged": state.any_engaged,
            "global": state.global_engaged,
            "degraded": state.degraded,
            "environments": sorted(state.environments),
            "services": sorted(state.services),
            "action_types": sorted(a.value for a in state.action_types),
        },
    }


@router.get("/actions", summary="Action registry with risk classification")
async def action_registry(_: RequireViewer) -> dict[str, Any]:
    """The complete action vocabulary and how policy classifies each entry.

    Tier 3 entries are listed as ``executable: false`` because no executor is
    registered for them - policy cannot allow what does not exist.
    """
    items = []
    for action in ActionType:
        p = profile_for(action)
        items.append({
            "action_type": action.value,
            "risk_tier": int(p.tier),
            "executable": int(p.tier) < 3,
            "idempotent": p.idempotent,
            "reversible": p.reversible,
            "max_blast_radius": p.max_blast_radius,
            "min_confidence": p.min_confidence,
            "requires_verification": p.requires_verification,
            "description": p.description,
        })
    items.sort(key=lambda i: (i["risk_tier"], i["action_type"]))
    return {"items": items}


class KillSwitchIn(BaseModel):
    scope: str = Field(description="global | environment | action_type | service")
    target: str = Field(default="", max_length=256)
    reason: str = Field(min_length=1, max_length=500)


@router.post("/kill-switch", summary="Engage a kill switch")
async def engage_kill_switch(
    body: KillSwitchIn, principal: RequireAdmin, request: Request
) -> dict[str, Any]:
    if body.scope not in _VALID_SCOPES:
        from aegis.core.errors import ValidationError

        raise ValidationError(
            f"scope must be one of {sorted(_VALID_SCOPES)}", context={"scope": body.scope}
        )

    db = request.app.state.db
    store = PolicyStore(db)
    await store.engage(body.scope, body.target, reason=body.reason, actor=principal.uid)
    await db.execute(
        """
        INSERT INTO audit_log (actor, actor_type, event_type, resource_type,
                               resource_id, detail)
        VALUES ($1,'human','kill_switch_engaged','kill_switch',$2,$3)
        """,
        principal.uid, f"{body.scope}:{body.target}", {"reason": body.reason},
    )
    return {"engaged": True, "scope": body.scope, "target": body.target}


@router.delete("/kill-switch", summary="Release a kill switch")
async def release_kill_switch(
    scope: str, principal: RequireAdmin, request: Request, target: str = ""
) -> dict[str, Any]:
    db = request.app.state.db
    await PolicyStore(db).release(scope, target, actor=principal.uid)
    await db.execute(
        """
        INSERT INTO audit_log (actor, actor_type, event_type, resource_type, resource_id)
        VALUES ($1,'human','kill_switch_released','kill_switch',$2)
        """,
        principal.uid, f"{scope}:{target}",
    )
    return {"engaged": False, "scope": scope, "target": target}


@router.get("/kill-switch", summary="List kill switches")
async def list_kill_switches(_: RequireViewer, request: Request) -> dict[str, Any]:
    return {"items": await PolicyStore(request.app.state.db).list_all()}


@router.get("/audit", summary="Audit log")
async def audit(
    _: RequireAdmin, request: Request, limit: int = 100, incident_id: str | None = None
) -> dict[str, Any]:
    db = request.app.state.db
    capped = max(1, min(limit, 500))
    if incident_id:
        rows = await db.fetch(
            """
            SELECT id, incident_id, actor, actor_type, event_type, resource_type,
                   resource_id, detail, correlation_id, created_at
            FROM audit_log WHERE incident_id = $1 ORDER BY created_at DESC LIMIT $2
            """,
            incident_id, capped,
        )
    else:
        rows = await db.fetch(
            """
            SELECT id, incident_id, actor, actor_type, event_type, resource_type,
                   resource_id, detail, correlation_id, created_at
            FROM audit_log ORDER BY created_at DESC LIMIT $1
            """,
            capped,
        )
    return {"items": [dict(r) for r in rows]}
