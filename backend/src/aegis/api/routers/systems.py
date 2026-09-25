"""Live systems: what is running, and how healthy it is right now.

Reads come from the runtime adapter, which normalises Compose, Kubernetes and
ECS into one shape. When no adapter is configured these endpoints return an
explicit unavailable state rather than an empty list - an operator looking at an
empty services page needs to know whether that means "nothing is deployed" or
"Aegis cannot see your environment".
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Query

from aegis.api.deps import ContainerDep, RequireViewer
from aegis.core.errors import ExternalServiceError, SourceUnavailable
from aegis.core.logging import get_logger

log = get_logger(__name__)
router = APIRouter(prefix="/systems", tags=["systems"])


def _unavailable(reason: str) -> dict[str, Any]:
    """The shape the UI renders as a configuration state, not as emptiness."""
    return {
        "available": False,
        "reason": reason,
        "items": [],
        "count": 0,
    }


@router.get("/services", summary="Services in the observed environment")
async def list_services(
    _: RequireViewer, container: ContainerDep
) -> dict[str, Any]:
    if container.runtime is None or not container.runtime.available:
        reason = (
            container.runtime.unavailable_reason
            if container.runtime is not None
            else "no runtime adapter is configured"
        )
        return _unavailable(reason)

    try:
        states = await container.runtime.list_services()
    except ExternalServiceError as exc:
        return _unavailable(str(exc))

    items = [
        {
            "service_id": s.ref.service_id,
            "name": s.ref.name,
            "environment": s.ref.environment,
            "workload": s.ref.workload,
            "health": s.health.value,
            "version": s.version,
            "desired_instances": s.desired_instances,
            "ready_instances": s.ready_instances,
            "degraded": s.is_degraded,
            "error_rate": s.error_rate,
            "latency_p99_ms": s.latency_p99_ms,
            "owner_team": s.owner_team,
        }
        for s in states
    ]
    return {"available": True, "reason": "", "items": items, "count": len(items)}


@router.get("/services/{service_id}/instances", summary="Instances of one service")
async def list_instances(
    service_id: str, _: RequireViewer, container: ContainerDep
) -> dict[str, Any]:
    if container.runtime is None or not container.runtime.available:
        return _unavailable("no runtime adapter is configured")
    try:
        instances = await container.runtime.list_instances(service_id)
    except ExternalServiceError as exc:
        return _unavailable(str(exc))

    items = [
        {
            "instance_id": i.instance_id,
            "name": i.name,
            "state": i.state,
            "health": i.health.value,
            "image": i.image,
            "version": i.version,
            "restart_count": i.restart_count,
            "started_at": i.started_at.isoformat() if i.started_at else None,
        }
        for i in instances
    ]
    return {"available": True, "reason": "", "items": items, "count": len(items)}


@router.get("/services/{service_id}/metrics", summary="Golden signals for one service")
async def service_metrics(
    _: RequireViewer,
    container: ContainerDep,
    service_id: str,
    window_s: Annotated[int, Query(ge=60, le=86400)] = 3600,
) -> dict[str, Any]:
    """Error rate, latency and throughput.

    Each signal is reported independently. One unavailable metric does not blank
    the other two, and an unavailable metric is never rendered as zero.
    """
    name = service_id.split(":")[-1]
    out: dict[str, Any] = {"service_id": service_id, "window_s": window_s, "signals": {}}

    for label, call in (
        ("error_rate", container.prometheus.error_rate),
        ("latency_p99", container.prometheus.latency_p99),
        ("request_rate", container.prometheus.request_rate),
    ):
        try:
            series = await call(name, window_s=window_s)
        except SourceUnavailable as exc:
            out["signals"][label] = {"available": False, "reason": str(exc)}
            continue
        points = [
            {"t": p.timestamp, "v": p.value} for s in series for p in s.points
        ][-500:]
        out["signals"][label] = {
            "available": True,
            "points": points,
            "latest": points[-1]["v"] if points else None,
            "empty": not points,
        }
    return out


@router.get("/health", summary="Environment health roll-up")
async def environment_health(
    _: RequireViewer, container: ContainerDep
) -> dict[str, Any]:
    services = await list_services(_, container)
    if not services["available"]:
        return {"available": False, "reason": services["reason"]}
    items = services["items"]
    by_health: dict[str, int] = {}
    for item in items:
        by_health[item["health"]] = by_health.get(item["health"], 0) + 1
    return {
        "available": True,
        "total": len(items),
        "by_health": by_health,
        "degraded": [i["service_id"] for i in items if i["degraded"]],
    }
