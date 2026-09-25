"""Integration and capability status.

Every optional dependency reports three things: whether it is configured,
whether it is reachable right now, and why not when it is not. The third is the
one that matters - "GraphRAG unavailable" is a support ticket, "GraphRAG
unavailable: NEO4J_PASSWORD is not set" is a fix.

Health probes here are best-effort and bounded. This endpoint must stay fast
even when three integrations are timing out, so probes run concurrently with a
hard deadline and a slow dependency reports as unknown rather than blocking the
page.
"""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter

from aegis.api.deps import ContainerDep, RequireViewer
from aegis.core.logging import get_logger
from aegis.core.resilience import breaker_states

log = get_logger(__name__)
router = APIRouter(prefix="/integrations", tags=["configuration"])

PROBE_TIMEOUT_S = 3.0


async def _probe(name: str, coro: Any) -> tuple[str, bool | None, str]:
    """Run one health probe under a deadline. Never raises."""
    try:
        result = await asyncio.wait_for(coro, timeout=PROBE_TIMEOUT_S)
        return name, bool(result), ""
    except TimeoutError:
        return name, None, f"probe exceeded {PROBE_TIMEOUT_S:.0f}s"
    except Exception as exc:  # noqa: BLE001 - a probe failure is a datum
        return name, False, f"{type(exc).__name__}: {exc}"


@router.get("", summary="Configured capabilities and their live health")
async def list_integrations(
    _: RequireViewer, container: ContainerDep
) -> dict[str, Any]:
    capabilities = container.capability_report()

    probes: list[Any] = [_probe("postgres", container.db.healthy())]
    if capabilities.get("graph", {}).get("configured"):
        probes.append(_probe("graph", container.neo4j.healthy()))
    if capabilities.get("sandbox", {}).get("configured"):
        probes.append(_probe("sandbox", container.sandbox.healthy()))
    if container.runtime is not None and container.runtime.available:
        probes.append(_probe("runtime", _runtime_probe(container)))

    results = await asyncio.gather(*probes, return_exceptions=False)
    health = {name: {"healthy": ok, "reason": reason} for name, ok, reason in results}

    items = []
    for name, cap in capabilities.items():
        probe = health.get(name, {})
        items.append(
            {
                "name": name,
                "configured": cap["configured"],
                "reason": cap["reason"],
                "healthy": probe.get("healthy"),
                "health_reason": probe.get("reason", ""),
            }
        )
    # Postgres is not an optional capability, so it is added explicitly rather
    # than being absent from a page that claims to show what Aegis depends on.
    items.insert(
        0,
        {
            "name": "postgres",
            "configured": True,
            "reason": "",
            "healthy": health.get("postgres", {}).get("healthy"),
            "health_reason": health.get("postgres", {}).get("reason", ""),
            "required": True,
        },
    )

    return {
        "items": items,
        "count": len(items),
        # An open breaker means Aegis is deliberately not calling a dependency.
        # Surfacing it stops an operator debugging a "missing" integration that
        # is actually being protected from a failing backend.
        "circuit_breakers": breaker_states(),
        "audit_write_failures": container.audit.write_failures,
    }


async def _runtime_probe(container: ContainerDep) -> bool:
    services = await container.runtime.list_services()
    return isinstance(services, list)


@router.get("/autonomy", summary="Autonomy posture as actually configured")
async def autonomy(_: RequireViewer, container: ContainerDep) -> dict[str, Any]:
    """What Aegis is currently permitted to do without a human.

    Read from live settings and the kill-switch table rather than from
    documentation, because the two drift and only one of them stops an action.
    """
    s = container.settings
    kill_switch = await container.policy.load_kill_switches()
    return {
        "autonomy_enabled": s.autonomy_enabled,
        "autonomy_mode": s.autonomy_mode.value,
        "allowed_tiers": sorted(s.allowed_tiers),
        "max_actions_per_hour": s.autonomy_max_actions_per_hour,
        "approval_ttl_seconds": s.approval_ttl_seconds,
        "lease_ttl_seconds": s.resource_lease_ttl_seconds,
        "environment": s.aegis_env.value,
        "kill_switch": {
            "any_engaged": kill_switch.any_engaged,
            "global": kill_switch.global_engaged,
            "degraded": kill_switch.degraded,
            "reason": kill_switch.reason,
            "environments": sorted(kill_switch.environments),
            "action_types": sorted(a.value for a in kill_switch.action_types),
            "services": sorted(kill_switch.services),
        },
    }
