"""The service graph.

Neo4j holds topology; Postgres remains the system of record. These endpoints
return nodes and edges shaped for direct rendering, with the query provenance
attached so an operator can reproduce what they are looking at.

An unreachable graph returns an explicit unavailable state. Rendering an empty
canvas would tell an operator their architecture has no dependencies, which is
the opposite of what happened.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Query

from aegis.api.deps import ContainerDep, RequireViewer
from aegis.core.errors import SourceUnavailable
from aegis.core.logging import get_logger

log = get_logger(__name__)
router = APIRouter(prefix="/graph", tags=["topology"])


def _unavailable(reason: str) -> dict[str, Any]:
    return {"available": False, "reason": reason, "nodes": [], "edges": []}


@router.get("/neighbourhood", summary="Service graph around one service")
async def neighbourhood(
    _: RequireViewer,
    container: ContainerDep,
    service_id: Annotated[str, Query(min_length=1, max_length=200)],
    depth: Annotated[int, Query(ge=1, le=4)] = 2,
) -> dict[str, Any]:
    if container.topology is None:
        return _unavailable("no graph client is configured")
    try:
        result = await container.topology.neighbourhood(service_id, depth=depth)
    except SourceUnavailable as exc:
        return _unavailable(str(exc))
    return {
        "available": True,
        "reason": "",
        "nodes": result.get("nodes", []),
        "edges": result.get("edges", []),
        "root": service_id,
        "depth": depth,
    }


@router.get("/blast-radius", summary="What breaks if this service does")
async def blast_radius(
    _: RequireViewer,
    container: ContainerDep,
    service_id: Annotated[str, Query(min_length=1, max_length=200)],
    max_depth: Annotated[int, Query(ge=1, le=6)] = 3,
) -> dict[str, Any]:
    if container.topology is None:
        return {"available": False, "reason": "no graph client is configured"}
    try:
        radius = await container.topology.blast_radius(service_id, max_depth=max_depth)
    except SourceUnavailable as exc:
        return {"available": False, "reason": str(exc)}
    return {
        "available": True,
        "service_id": service_id,
        "directly_affected": radius.directly_affected,
        "downstream": radius.downstream,
        "customer_facing": radius.customer_facing,
        "size": radius.size,
        "estimated_request_share": radius.estimated_request_share,
    }


@router.get("/causal-paths", summary="Paths between two services")
async def causal_paths(
    _: RequireViewer,
    container: ContainerDep,
    from_service: Annotated[str, Query(min_length=1, max_length=200)],
    to_service: Annotated[str, Query(min_length=1, max_length=200)],
    max_depth: Annotated[int, Query(ge=1, le=6)] = 5,
) -> dict[str, Any]:
    if container.topology is None:
        return {"available": False, "reason": "no graph client is configured", "paths": []}
    try:
        paths = await container.topology.causal_paths(
            from_service, to_service, max_depth=max_depth
        )
    except SourceUnavailable as exc:
        return {"available": False, "reason": str(exc), "paths": []}
    return {"available": True, "paths": paths, "count": len(paths)}


@router.get("/dependencies", summary="Upstream dependencies of a service")
async def dependencies(
    _: RequireViewer,
    container: ContainerDep,
    service_id: Annotated[str, Query(min_length=1, max_length=200)],
    max_depth: Annotated[int, Query(ge=1, le=6)] = 3,
) -> dict[str, Any]:
    if container.topology is None:
        return {"available": False, "reason": "no graph client is configured", "items": []}
    try:
        upstream = await container.topology.upstream_dependencies(
            service_id, max_depth=max_depth
        )
        shared = await container.topology.services_sharing_dependency(service_id)
    except SourceUnavailable as exc:
        return {"available": False, "reason": str(exc), "items": []}
    return {
        "available": True,
        "service_id": service_id,
        "upstream": upstream,
        "sharing_a_dependency": shared,
    }


@router.post("/refresh", summary="Re-ingest topology from telemetry")
async def refresh(
    _: RequireViewer, container: ContainerDep
) -> dict[str, Any]:
    """Rebuild Service nodes and CALLS edges from what telemetry actually shows.

    Ingestion is MERGE-only and idempotent, so this is safe to call repeatedly;
    a service that churned does not become a second node.
    """
    if container.graph_ingest is None:
        return {"available": False, "reason": "no graph client is configured"}

    result: dict[str, Any] = {"available": True, "services": None, "edges": None}
    try:
        # Re-evaluates the capability as well, so a topology that was marked
        # unavailable at boot recovers here instead of staying stale.
        await container.ensure_graph_schema()
        refs = await container.graph_ingest.ingest_from_telemetry(
            container.prometheus,
            environment=container.settings.aegis_environment_name,
            workload=container.settings.workload_namespace,
        )
        result["services"] = len(refs)
    except SourceUnavailable as exc:
        result["services_error"] = str(exc)

    if container.tempo is not None:
        try:
            stats = await container.graph_ingest.ingest_from_traces(
                container.tempo,
                environment=container.settings.aegis_environment_name,
                workload=container.settings.workload_namespace,
            )
            result["edges"] = stats.edges
        except SourceUnavailable as exc:
            result["edges_error"] = str(exc)
    else:
        result["edges_error"] = "no trace source is configured"

    log.info("topology refresh requested", **{k: v for k, v in result.items() if v is not None})
    return result
