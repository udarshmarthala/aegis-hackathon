"""Structure tools: who depends on whom, and who breaks when this breaks.

All read-only, all served by ``graph.traversal`` and ``graph.graphrag``, which
render parameterised Cypher from a fixed template set. No tool here accepts a
Cypher fragment - an agent picks a service id, a depth and a limit, and nothing
else reaches the driver.

Neo4j is topology, never the system of record. Every tool degrades to
``degraded=True`` with an evidence gap when the graph cannot be read, because a
missing graph must lower confidence rather than halt an investigation
(CLAUDE.md invariant 9).
"""

from __future__ import annotations

from typing import Any

from pydantic import Field

from aegis.core.errors import SourceUnavailable
from aegis.domain.enums import EvidenceType, SourceType
from aegis.graph.traversal import QueryRecorder, provenance_uri, rendered_cypher
from aegis.mcp.deps import ToolDeps
from aegis.mcp.registry import ToolRegistry
from aegis.mcp.tools import support
from aegis.mcp.types import (
    ENVIRONMENTS,
    CostHint,
    ToolContext,
    ToolInput,
    ToolOutcome,
    ToolOutput,
    ToolSpec,
)

MAX_NODES = 100
MAX_EDGES = 200
MAX_PATHS = 10
MAX_SEEDS = 8

SERVICE_ID = Field(min_length=3, max_length=200)


# --------------------------------------------------------------------------- #
# models                                                                       #
# --------------------------------------------------------------------------- #


class ServiceDepthInput(ToolInput):
    service_id: str = SERVICE_ID
    max_depth: int = Field(default=3, ge=1, le=5)
    limit: int = Field(default=50, ge=1, le=MAX_NODES)


class BlastRadiusOutput(ToolOutput):
    service_id: str
    directly_affected: tuple[str, ...] = ()
    downstream: tuple[str, ...] = ()
    customer_facing: bool = False
    estimated_request_share: float = 0.0
    provenance_uri: str = ""

    @property
    def is_empty(self) -> bool:
        return not self.directly_affected and not self.downstream


class GraphNodeOut(ToolOutput):
    node_id: str
    label: str
    name: str
    hops: int


class DependenciesOutput(ToolOutput):
    service_id: str
    dependencies: tuple[GraphNodeOut, ...] = ()
    provenance_uri: str = ""

    @property
    def is_empty(self) -> bool:
        return not self.dependencies


class CausalPathsInput(ToolInput):
    from_service: str = SERVICE_ID
    to_service: str = SERVICE_ID
    max_depth: int = Field(default=5, ge=1, le=5)
    limit: int = Field(default=5, ge=1, le=MAX_PATHS)


class CausalPathOut(ToolOutput):
    nodes: tuple[str, ...]
    hops: int


class CausalPathsOutput(ToolOutput):
    from_service: str
    to_service: str
    paths: tuple[CausalPathOut, ...] = ()
    provenance_uri: str = ""

    @property
    def is_empty(self) -> bool:
        return not self.paths


class NeighbourhoodInput(ToolInput):
    service_id: str = SERVICE_ID
    depth: int = Field(default=2, ge=1, le=3)
    node_limit: int = Field(default=50, ge=1, le=MAX_NODES)
    edge_limit: int = Field(default=100, ge=1, le=MAX_EDGES)


class NeighbourEdgeOut(ToolOutput):
    source: str
    target: str
    type: str
    call_count: int | None = None
    error_count: int | None = None
    latency_p99_ms: float | None = None


class NeighbourNodeOut(ToolOutput):
    node_id: str
    label: str
    name: str
    hops: int
    environment: str | None = None
    health: str | None = None


class NeighbourhoodOutput(ToolOutput):
    service_id: str
    depth: int
    nodes: tuple[NeighbourNodeOut, ...] = ()
    edges: tuple[NeighbourEdgeOut, ...] = ()
    provenance_uri: str = ""

    @property
    def is_empty(self) -> bool:
        return not self.nodes


class ServiceInput(ToolInput):
    service_id: str = SERVICE_ID


class OwningTeamOutput(ToolOutput):
    service_id: str
    team_id: str | None = None
    name: str | None = None
    contact: str | None = None
    provenance_uri: str = ""

    @property
    def is_empty(self) -> bool:
        """No owner recorded in the graph - distinct from an unreachable graph."""
        return self.team_id is None


class SharedDependencyOut(ToolOutput):
    resource_id: str
    name: str
    label: str
    service_ids: tuple[str, ...]


class SharedDependenciesInput(ToolInput):
    service_id: str = SERVICE_ID
    limit: int = Field(default=25, ge=1, le=MAX_NODES)


class SharedDependenciesOutput(ToolOutput):
    service_id: str
    shared: tuple[SharedDependencyOut, ...] = ()
    provenance_uri: str = ""

    @property
    def is_empty(self) -> bool:
        return not self.shared


class ExpandContextInput(ToolInput):
    seed_services: list[str] = Field(min_length=1, max_length=MAX_SEEDS)
    depth: int = Field(default=2, ge=1, le=3)
    budget: int = Field(default=40, ge=1, le=100)


class ScoredServiceOut(ToolOutput):
    service_id: str
    name: str
    label: str
    hops: int
    score: float
    reasons: tuple[str, ...]


class ExpandContextOutput(ToolOutput):
    seeds: tuple[str, ...]
    services: tuple[ScoredServiceOut, ...] = ()
    causal_paths: tuple[tuple[str, ...], ...] = ()
    directly_affected: tuple[str, ...] = ()
    downstream: tuple[str, ...] = ()
    customer_facing: bool = False
    deployments: tuple[str, ...] = ()
    truncated: bool = False

    @property
    def is_empty(self) -> bool:
        return not self.services


# --------------------------------------------------------------------------- #
# registration                                                                 #
# --------------------------------------------------------------------------- #


def _uri(operation: str, params: dict[str, Any], *, depth: int | None = None) -> str:
    """Reproducible provenance: the exact rendered query plus its parameters."""
    return provenance_uri(rendered_cypher(operation, depth=depth), params)


def register(registry: ToolRegistry, deps: ToolDeps) -> None:
    """Declare the topology tools against an injected dependency set."""

    async def blast_radius(context: ToolContext, args: ServiceDepthInput) -> ToolOutcome:
        empty = BlastRadiusOutput(service_id=args.service_id)
        if deps.traversal is None:
            return await support.degraded(
                deps, context, source="neo4j", source_type=SourceType.GRAPH,
                reason="graph traversal is not configured", value=empty,
            )
        recorder = QueryRecorder()
        try:
            radius = await deps.traversal.blast_radius(
                args.service_id, args.max_depth, limit=args.limit, recorder=recorder
            )
        except SourceUnavailable as exc:
            return await support.degraded(
                deps, context, source="neo4j", source_type=SourceType.GRAPH,
                reason=exc.message, value=empty,
            )
        record = recorder.first("blast_radius")
        uri = record.uri if record else _uri("blast_radius", {"service_id": args.service_id},
                                             depth=args.max_depth)
        value = BlastRadiusOutput(
            service_id=args.service_id,
            directly_affected=tuple(radius.directly_affected),
            downstream=tuple(radius.downstream),
            customer_facing=radius.customer_facing,
            estimated_request_share=radius.estimated_request_share,
            provenance_uri=uri,
        )
        ids = await support.record_evidence(
            deps, context, source="neo4j", source_type=SourceType.GRAPH,
            evidence_type=EvidenceType.BLAST_RADIUS,
            summary=(
                f"{args.service_id}: {len(radius.directly_affected)} directly affected, "
                f"{len(radius.downstream)} downstream, customer_facing="
                f"{radius.customer_facing}"
            ),
            structured_value=value.model_dump(mode="json"),
            provenance_uri=uri, resource_id=args.service_id,
        )
        return ToolOutcome(value=value, evidence_ids=ids, provenance=(uri,))

    async def upstream_dependencies(
        context: ToolContext, args: ServiceDepthInput
    ) -> ToolOutcome:
        empty = DependenciesOutput(service_id=args.service_id)
        if deps.traversal is None:
            return await support.degraded(
                deps, context, source="neo4j", source_type=SourceType.GRAPH,
                reason="graph traversal is not configured", value=empty,
            )
        recorder = QueryRecorder()
        try:
            nodes = await deps.traversal.upstream_dependencies(
                args.service_id, args.max_depth, limit=args.limit, recorder=recorder
            )
        except SourceUnavailable as exc:
            return await support.degraded(
                deps, context, source="neo4j", source_type=SourceType.GRAPH,
                reason=exc.message, value=empty,
            )
        record = recorder.first("upstream_dependencies")
        uri = record.uri if record else _uri(
            "upstream_dependencies", {"service_id": args.service_id}, depth=args.max_depth
        )
        value = DependenciesOutput(
            service_id=args.service_id,
            dependencies=tuple(
                GraphNodeOut(
                    node_id=n.node_id, label=n.label.value, name=n.name, hops=n.hops
                )
                for n in nodes
            ),
            provenance_uri=uri,
        )
        ids = await support.record_evidence(
            deps, context, source="neo4j", source_type=SourceType.GRAPH,
            evidence_type=EvidenceType.TOPOLOGY_PATH,
            summary=f"{args.service_id} depends on {len(nodes)} upstream resources",
            structured_value=value.model_dump(mode="json"),
            provenance_uri=uri, resource_id=args.service_id,
        )
        return ToolOutcome(value=value, evidence_ids=ids, provenance=(uri,))

    async def causal_paths(context: ToolContext, args: CausalPathsInput) -> ToolOutcome:
        empty = CausalPathsOutput(
            from_service=args.from_service, to_service=args.to_service
        )
        if deps.traversal is None:
            return await support.degraded(
                deps, context, source="neo4j", source_type=SourceType.GRAPH,
                reason="graph traversal is not configured", value=empty,
            )
        recorder = QueryRecorder()
        try:
            paths = await deps.traversal.causal_paths(
                args.from_service, args.to_service, args.max_depth,
                limit=args.limit, recorder=recorder,
            )
        except SourceUnavailable as exc:
            return await support.degraded(
                deps, context, source="neo4j", source_type=SourceType.GRAPH,
                reason=exc.message, value=empty,
            )
        record = recorder.first("causal_paths")
        uri = record.uri if record else _uri(
            "causal_paths",
            {"from_id": args.from_service, "to_id": args.to_service},
            depth=args.max_depth,
        )
        if not paths:
            # No structural path means the two services are not connected in the
            # graph. That refutes a propagation story rather than failing to test it.
            return ToolOutcome(
                value=CausalPathsOutput(
                    from_service=args.from_service, to_service=args.to_service,
                    provenance_uri=uri,
                ),
                provenance=(uri,),
            )
        value = CausalPathsOutput(
            from_service=args.from_service, to_service=args.to_service,
            paths=tuple(CausalPathOut(nodes=p.nodes, hops=p.hops) for p in paths),
            provenance_uri=uri,
        )
        ids = await support.record_evidence(
            deps, context, source="neo4j", source_type=SourceType.GRAPH,
            evidence_type=EvidenceType.TOPOLOGY_PATH,
            summary=(
                f"{len(paths)} structural paths from {args.from_service} "
                f"to {args.to_service}"
            ),
            structured_value=value.model_dump(mode="json"),
            provenance_uri=uri, resource_id=args.from_service,
        )
        return ToolOutcome(value=value, evidence_ids=ids, provenance=(uri,))

    async def service_neighbourhood(
        context: ToolContext, args: NeighbourhoodInput
    ) -> ToolOutcome:
        empty = NeighbourhoodOutput(service_id=args.service_id, depth=args.depth)
        if deps.traversal is None:
            return await support.degraded(
                deps, context, source="neo4j", source_type=SourceType.GRAPH,
                reason="graph traversal is not configured", value=empty,
            )
        recorder = QueryRecorder()
        try:
            view = await deps.traversal.neighbourhood(
                args.service_id, args.depth,
                node_limit=args.node_limit, edge_limit=args.edge_limit,
                recorder=recorder,
            )
        except SourceUnavailable as exc:
            return await support.degraded(
                deps, context, source="neo4j", source_type=SourceType.GRAPH,
                reason=exc.message, value=empty,
            )
        record = recorder.first("neighbourhood_nodes")
        uri = record.uri if record else _uri(
            "neighbourhood_nodes", {"service_id": args.service_id}, depth=args.depth
        )
        value = NeighbourhoodOutput(
            service_id=args.service_id, depth=args.depth,
            nodes=tuple(
                NeighbourNodeOut(
                    node_id=str(n["id"]), label=str(n["label"]), name=str(n["name"]),
                    hops=int(n["hops"]),
                    environment=n.get("environment"), health=n.get("health"),
                )
                for n in view["nodes"][:MAX_NODES]
            ),
            edges=tuple(
                NeighbourEdgeOut(
                    source=str(e["source"]), target=str(e["target"]), type=str(e["type"]),
                    call_count=e.get("call_count"), error_count=e.get("error_count"),
                    latency_p99_ms=e.get("latency_p99_ms"),
                )
                for e in view["edges"][:MAX_EDGES]
            ),
            provenance_uri=uri,
        )
        return ToolOutcome(value=value, provenance=(uri,))

    async def owning_team(context: ToolContext, args: ServiceInput) -> ToolOutcome:
        empty = OwningTeamOutput(service_id=args.service_id)
        if deps.traversal is None:
            return await support.degraded(
                deps, context, source="neo4j", source_type=SourceType.GRAPH,
                reason="graph traversal is not configured", value=empty,
            )
        recorder = QueryRecorder()
        try:
            team = await deps.traversal.owning_team(args.service_id, recorder=recorder)
        except SourceUnavailable as exc:
            return await support.degraded(
                deps, context, source="neo4j", source_type=SourceType.GRAPH,
                reason=exc.message, value=empty,
            )
        record = recorder.first("owning_team")
        uri = record.uri if record else _uri("owning_team", {"service_id": args.service_id})
        if team is None:
            # The graph holds no owner. Said plainly rather than guessed at.
            return ToolOutcome(
                value=OwningTeamOutput(service_id=args.service_id, provenance_uri=uri),
                provenance=(uri,),
            )
        value = OwningTeamOutput(
            service_id=args.service_id, team_id=team.team_id, name=team.name,
            contact=team.contact, provenance_uri=uri,
        )
        return ToolOutcome(value=value, provenance=(uri,))

    async def services_sharing_dependency(
        context: ToolContext, args: SharedDependenciesInput
    ) -> ToolOutcome:
        empty = SharedDependenciesOutput(service_id=args.service_id)
        if deps.traversal is None:
            return await support.degraded(
                deps, context, source="neo4j", source_type=SourceType.GRAPH,
                reason="graph traversal is not configured", value=empty,
            )
        recorder = QueryRecorder()
        try:
            shared = await deps.traversal.services_sharing_dependency(
                args.service_id, limit=args.limit, recorder=recorder
            )
        except SourceUnavailable as exc:
            return await support.degraded(
                deps, context, source="neo4j", source_type=SourceType.GRAPH,
                reason=exc.message, value=empty,
            )
        record = recorder.first("services_sharing_dependency")
        uri = record.uri if record else _uri(
            "services_sharing_dependency", {"service_id": args.service_id}
        )
        value = SharedDependenciesOutput(
            service_id=args.service_id,
            shared=tuple(
                SharedDependencyOut(
                    resource_id=s.resource_id, name=s.name, label=s.label.value,
                    service_ids=s.service_ids,
                )
                for s in shared
            ),
            provenance_uri=uri,
        )
        if shared:
            ids = await support.record_evidence(
                deps, context, source="neo4j", source_type=SourceType.GRAPH,
                evidence_type=EvidenceType.TOPOLOGY_PATH,
                summary=(
                    f"{args.service_id} shares {len(shared)} dependencies with "
                    "other services"
                ),
                structured_value=value.model_dump(mode="json"),
                provenance_uri=uri, resource_id=args.service_id,
            )
            return ToolOutcome(value=value, evidence_ids=ids, provenance=(uri,))
        return ToolOutcome(value=value, provenance=(uri,))

    async def expand_graph_context(
        context: ToolContext, args: ExpandContextInput
    ) -> ToolOutcome:
        empty = ExpandContextOutput(seeds=tuple(args.seed_services))
        if deps.graphrag is None:
            return await support.degraded(
                deps, context, source="neo4j", source_type=SourceType.GRAPH,
                reason="graph expansion is not configured", value=empty,
            )
        if not context.incident_id:
            # Expansion scores nodes against an incident's start time; without an
            # incident there is nothing to score against, and inventing one would
            # silently change the ranking.
            return await support.degraded(
                deps, context, source="neo4j", source_type=SourceType.GRAPH,
                reason="expand_graph_context requires an incident context",
                value=empty,
            )
        try:
            ctx = await deps.graphrag.expand(
                list(args.seed_services),
                incident_id=context.incident_id,
                depth=args.depth,
                budget=args.budget,
            )
        except SourceUnavailable as exc:
            return await support.degraded(
                deps, context, source="neo4j", source_type=SourceType.GRAPH,
                reason=exc.message, value=empty,
            )
        value = ExpandContextOutput(
            seeds=ctx.seeds,
            services=tuple(
                ScoredServiceOut(
                    service_id=s.service_id, name=s.name, label=s.label,
                    hops=s.hops, score=s.score, reasons=s.reasons,
                )
                for s in ctx.services
            ),
            causal_paths=ctx.causal_paths,
            directly_affected=tuple(ctx.blast_radius.directly_affected),
            downstream=tuple(ctx.blast_radius.downstream),
            customer_facing=ctx.blast_radius.customer_facing,
            deployments=tuple(d.deployment_id for d in ctx.deployments),
            truncated=ctx.truncated,
        )
        uri = str(ctx.provenance.get("uri") or f"graph://expand/{context.incident_id}")
        ids = await support.record_evidence(
            deps, context, source="neo4j", source_type=SourceType.GRAPH,
            evidence_type=EvidenceType.TOPOLOGY_PATH,
            summary=(
                f"graph expansion from {len(ctx.seeds)} seeds returned "
                f"{len(ctx.services)} ranked services"
            ),
            structured_value={
                "seeds": list(ctx.seeds),
                "services": [s.as_dict() for s in ctx.services[:40]],
                "truncated": ctx.truncated,
            },
            provenance_uri=uri,
        )
        return ToolOutcome(value=value, evidence_ids=ids, provenance=(uri,))

    # ---- specs ----------------------------------------------------------- #

    def _spec(
        name: str,
        description: str,
        input_model: type[ToolInput],
        output_model: type[ToolOutput],
        *,
        timeout_s: float,
        cost_hint: CostHint,
    ) -> ToolSpec:
        """Every topology tool is a bounded, idempotent, retryable graph read."""
        return ToolSpec(
            name=name,
            description=description,
            server="topology",
            input_model=input_model,
            output_model=output_model,
            access="read",
            mutates="nothing",
            scope="topology:read",
            environments=ENVIRONMENTS,
            timeout_s=timeout_s,
            retryable=True,
            idempotent=True,
            cost_hint=cost_hint,
        )

    registry.register(
        _spec(
            "blast_radius",
            "Services that break when this one breaks, with request share.",
            ServiceDepthInput,
            BlastRadiusOutput,
            timeout_s=20.0,
            cost_hint="moderate",
        ),
        blast_radius,
    )
    registry.register(
        _spec(
            "upstream_dependencies",
            "Transitive callees and infrastructure this service needs.",
            ServiceDepthInput,
            DependenciesOutput,
            timeout_s=20.0,
            cost_hint="moderate",
        ),
        upstream_dependencies,
    )
    registry.register(
        _spec(
            "causal_paths",
            "Shortest structural paths between two services.",
            CausalPathsInput,
            CausalPathsOutput,
            timeout_s=25.0,
            cost_hint="moderate",
        ),
        causal_paths,
    )
    registry.register(
        _spec(
            "service_neighbourhood",
            "Bounded node and edge subgraph around one service.",
            NeighbourhoodInput,
            NeighbourhoodOutput,
            timeout_s=25.0,
            cost_hint="moderate",
        ),
        service_neighbourhood,
    )
    registry.register(
        _spec(
            "owning_team",
            "The team that owns a service, or an explicit absence.",
            ServiceInput,
            OwningTeamOutput,
            timeout_s=10.0,
            cost_hint="cheap",
        ),
        owning_team,
    )
    registry.register(
        _spec(
            "services_sharing_dependency",
            "Peers behind the same datastore, cache or queue - the "
            "correlated-failure question.",
            SharedDependenciesInput,
            SharedDependenciesOutput,
            timeout_s=20.0,
            cost_hint="moderate",
        ),
        services_sharing_dependency,
    )
    registry.register(
        _spec(
            "expand_graph_context",
            "Expand from seed services into a ranked, budgeted structural context "
            "for the current incident.",
            ExpandContextInput,
            ExpandContextOutput,
            timeout_s=45.0,
            cost_hint="expensive",
        ),
        expand_graph_context,
    )


__all__ = ["register"]
