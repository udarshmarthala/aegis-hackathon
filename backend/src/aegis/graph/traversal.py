"""Read-side graph queries, returning typed results.

Two rules shape every function here.

**Depth is clamped, always.** An unbounded variable-length pattern on a real
estate is not a slow query, it is an outage: Neo4j will happily enumerate an
exponential number of paths while holding a connection from a pool that the rest
of the investigation needs. ``MAX_DEPTH`` is the ceiling and callers cannot
exceed it, whatever they ask for.

**An empty list is an answer; an exception is not.** ``Neo4jClient`` already
converts every driver failure to ``SourceUnavailable``, and nothing here catches
it. "No dependents found" and "we could not ask" reach the caller as different
things, and the caller decides which becomes an evidence gap (PRD 13).

The variable-length upper bound is interpolated rather than bound, because Cypher
does not accept a parameter there. It is an ``int`` that has passed through
``_clamp_depth``, so the rendered text is one of seven possible strings.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final
from urllib.parse import urlencode

from aegis.core.logging import get_logger
from aegis.domain.models import BlastRadius
from aegis.graph.client import Neo4jClient
from aegis.graph.ontology import NODES, NodeLabel, RelType, label_token, rel_union_token

log = get_logger(__name__)

# The hard ceiling. Six hops already crosses most estates end to end; beyond it
# the result set stops being explanatory and starts being a denial of service.
MAX_DEPTH: Final = 6

# Per-query row caps. Every read is bounded so one pathological service cannot
# return a subgraph larger than the process can hold.
MAX_NODES: Final = 400
MAX_EDGES: Final = 800
MAX_PATHS: Final = 10
MAX_RESULTS: Final = 100

_CALLS = rel_union_token([RelType.CALLS])
_TOPOLOGY = rel_union_token([RelType.CALLS, RelType.DEPENDS_ON])
_OWNED_BY = rel_union_token([RelType.OWNED_BY])
_DEPENDS_ON = rel_union_token([RelType.DEPENDS_ON])
_DEPLOYED_AS = rel_union_token([RelType.DEPLOYED_AS])
_CREATED_BY = rel_union_token([RelType.CREATED_BY])
_SIMILAR_TO = rel_union_token([RelType.SIMILAR_TO])
_SERVICE = label_token(NodeLabel.SERVICE)
_INCIDENT = label_token(NodeLabel.INCIDENT)
_TEAM = label_token(NodeLabel.TEAM)
_DEPLOYMENT = label_token(NodeLabel.DEPLOYMENT)
_COMMIT = label_token(NodeLabel.COMMIT)

# Every template lives here so provenance can hash exactly what ran. A caller
# handed ``neo4j://<hash>?params=...`` must be able to reproduce the query.
CYPHER: Mapping[str, str] = {
    "blast_radius": f"""
MATCH (root:{_SERVICE} {{{{service_id: $service_id}}}})
MATCH p = (dependent:{_SERVICE})-[:{_CALLS}*1..{{depth}}]->(root)
WITH dependent, min(length(p)) AS hops
RETURN dependent.service_id AS service_id,
       dependent.name AS name,
       coalesce(dependent.customer_facing, false) AS customer_facing,
       hops
ORDER BY hops, service_id
LIMIT $limit
""",
    "request_share": f"""
MATCH (:{_SERVICE})-[r:{_CALLS}]->(b:{_SERVICE} {{environment: $environment}})
RETURN sum(coalesce(r.call_count, 0)) AS total,
       sum(CASE WHEN b.service_id IN $impacted THEN coalesce(r.call_count, 0) ELSE 0 END)
         AS impacted
""",
    "upstream_dependencies": f"""
MATCH p = (s:{_SERVICE} {{{{service_id: $service_id}}}})-[:{_TOPOLOGY}*1..{{depth}}]->(n)
WITH n, min(length(p)) AS hops
RETURN labels(n) AS labels, properties(n) AS props, hops
ORDER BY hops, coalesce(n.service_id, n.resource_id, '')
LIMIT $limit
""",
    "causal_paths": f"""
MATCH (a:{_SERVICE} {{{{service_id: $from_id}}}}), (b:{_SERVICE} {{{{service_id: $to_id}}}})
MATCH p = allShortestPaths((a)-[:{_TOPOLOGY}*1..{{depth}}]->(b))
RETURN [n IN nodes(p) | coalesce(n.service_id, n.resource_id, n.name)] AS path,
       length(p) AS hops
ORDER BY hops
LIMIT $limit
""",
    "neighbourhood_nodes": f"""
MATCH p = (root:{_SERVICE} {{{{service_id: $service_id}}}})-[:{_TOPOLOGY}*0..{{depth}}]-(n)
WITH n, min(length(p)) AS hops
RETURN labels(n) AS labels, properties(n) AS props, hops
ORDER BY hops, coalesce(n.service_id, n.resource_id, n.endpoint_id, '')
LIMIT $limit
""",
    "neighbourhood_edges": f"""
MATCH p = (root:{_SERVICE} {{{{service_id: $service_id}}}})-[:{_TOPOLOGY}*1..{{depth}}]-(n)
UNWIND relationships(p) AS r
WITH DISTINCT r
RETURN type(r) AS rel_type,
       properties(r) AS props,
       labels(startNode(r)) AS source_labels,
       properties(startNode(r)) AS source_props,
       labels(endNode(r)) AS target_labels,
       properties(endNode(r)) AS target_props
LIMIT $limit
""",
    "owning_team": f"""
MATCH (:{_SERVICE} {{service_id: $service_id}})-[:{_OWNED_BY}]->(t:{_TEAM})
RETURN t.team_id AS team_id, t.name AS name, t.contact AS contact
ORDER BY team_id
LIMIT 1
""",
    "recent_deployments": f"""
MATCH (:{_SERVICE} {{service_id: $service_id}})
      -[:{_DEPLOYED_AS}]->(d:{_DEPLOYMENT})
OPTIONAL MATCH (d)-[:{_CREATED_BY}]->(c:{_COMMIT})
RETURN d.deployment_id AS deployment_id,
       d.version AS version,
       d.status AS status,
       d.deployed_at AS deployed_at,
       c.sha AS commit_sha,
       c.repo AS commit_repo,
       c.author AS commit_author,
       c.authored_at AS commit_authored_at
ORDER BY d.deployed_at DESC, deployment_id
LIMIT $limit
""",
    "services_sharing_dependency": f"""
MATCH (:{_SERVICE} {{service_id: $service_id}})
      -[:{_DEPENDS_ON}]->(dep)
MATCH (peer:{_SERVICE})-[:{_DEPENDS_ON}]->(dep)
WHERE peer.service_id <> $service_id
RETURN dep.resource_id AS resource_id,
       dep.name AS name,
       labels(dep) AS labels,
       collect(DISTINCT peer.service_id) AS service_ids
ORDER BY resource_id
LIMIT $limit
""",
    "similar_incidents": f"""
MATCH (:{_INCIDENT} {{incident_id: $incident_id}})
      -[r:{_SIMILAR_TO}]-(other:{_INCIDENT})
RETURN other.incident_id AS incident_id,
       coalesce(r.score, 0.0) AS score,
       r.method AS method,
       other.severity AS severity,
       other.title AS title,
       other.started_at AS started_at
ORDER BY score DESC, incident_id
LIMIT $limit
""",
}


def rendered_cypher(name: str, *, depth: int | None = None) -> str:
    """The exact text an operation sends, with the clamped depth substituted."""
    template = CYPHER[name]
    return template.format(depth=_clamp_depth(depth)) if depth is not None else template


def query_hash(cypher: str) -> str:
    """Short stable digest of a query, used as the provenance identifier."""
    return hashlib.sha256(cypher.encode("utf-8")).hexdigest()[:16]


def provenance_uri(cypher: str, params: Mapping[str, Any]) -> str:
    """``neo4j://<query-hash>?params=...`` - enough for an operator to re-run it."""
    encoded = json.dumps(dict(params), sort_keys=True, separators=(",", ":"), default=str)
    return f"neo4j://{query_hash(cypher)}?{urlencode({'params': encoded})}"


@dataclass(frozen=True, slots=True)
class QueryRecord:
    operation: str
    cypher_sha256: str
    params: Mapping[str, Any]
    uri: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "operation": self.operation,
            "cypher_sha256": self.cypher_sha256,
            "params": dict(self.params),
            "uri": self.uri,
        }


class QueryRecorder:
    """Bounded provenance sink passed in by a caller that needs citations.

    Explicitly passed rather than accumulated on the traversal object, because a
    shared traversal serves concurrent incidents and provenance belonging to one
    incident must never leak into another's evidence.
    """

    __slots__ = ("_records", "_limit")

    def __init__(self, limit: int = 64) -> None:
        self._records: list[QueryRecord] = []
        self._limit = limit

    def add(self, operation: str, cypher: str, params: Mapping[str, Any]) -> None:
        if len(self._records) >= self._limit:
            return
        self._records.append(
            QueryRecord(
                operation=operation,
                cypher_sha256=query_hash(cypher),
                params=dict(params),
                uri=provenance_uri(cypher, params),
            )
        )

    @property
    def records(self) -> tuple[QueryRecord, ...]:
        return tuple(self._records)

    def first(self, operation: str) -> QueryRecord | None:
        return next((r for r in self._records if r.operation == operation), None)


# --------------------------------------------------------------------------- #
# typed results                                                                #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class GraphNode:
    node_id: str
    label: NodeLabel
    name: str
    hops: int
    properties: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class GraphEdge:
    rel_type: RelType
    source_id: str
    target_id: str
    properties: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class CausalPath:
    nodes: tuple[str, ...]
    hops: int


@dataclass(frozen=True, slots=True)
class TeamRef:
    team_id: str
    name: str
    contact: str | None = None


@dataclass(frozen=True, slots=True)
class DeploymentSummary:
    deployment_id: str
    version: str
    status: str
    deployed_at: datetime | None
    commit_sha: str | None = None
    commit_repo: str | None = None
    commit_author: str | None = None
    commit_authored_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class SharedDependency:
    resource_id: str
    name: str
    label: NodeLabel
    service_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SimilarIncident:
    incident_id: str
    score: float
    method: str | None = None
    severity: str | None = None
    title: str | None = None
    started_at: datetime | None = None


# --------------------------------------------------------------------------- #
# helpers                                                                      #
# --------------------------------------------------------------------------- #


def _clamp_depth(depth: int | None) -> int:
    """At least one hop, never more than ``MAX_DEPTH``.

    Clamping rather than raising is deliberate: a caller asking for depth 50 gets
    a bounded, useful answer plus a log line, not a failed investigation step.
    """
    if depth is None:
        return 1
    clamped = max(1, min(int(depth), MAX_DEPTH))
    if clamped != depth:
        log.info("traversal depth clamped", requested=depth, applied=clamped, ceiling=MAX_DEPTH)
    return clamped


def _clamp_limit(limit: int | None, ceiling: int) -> int:
    if limit is None:
        return ceiling
    return max(1, min(int(limit), ceiling))


def _as_datetime(value: Any) -> datetime | None:
    """Neo4j temporals, native datetimes and ISO strings all arrive here."""
    if value is None or isinstance(value, datetime):
        return value
    to_native = getattr(value, "to_native", None)
    if callable(to_native):
        native = to_native()
        return native if isinstance(native, datetime) else None
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None
    return None


def _resolve_node(labels: Sequence[str], props: Mapping[str, Any]) -> tuple[NodeLabel, str] | None:
    """Pick the ontology label and natural-key value for a returned node.

    A node whose label is outside the ontology is dropped rather than guessed at:
    the graph is only useful to reason over while every node in it means
    something the schema defines.
    """
    for raw in labels or ():
        try:
            label = NodeLabel(raw)
        except ValueError:
            continue
        key = NODES[label].key
        value = props.get(key)
        if isinstance(value, str) and value:
            return label, value
    return None


def _to_graph_node(row: Mapping[str, Any]) -> GraphNode | None:
    props = row.get("props") or {}
    resolved = _resolve_node(row.get("labels") or (), props)
    if resolved is None:
        return None
    label, node_id = resolved
    return GraphNode(
        node_id=node_id,
        label=label,
        name=str(props.get("name") or node_id),
        hops=int(row.get("hops") or 0),
        properties=dict(props),
    )


class GraphTraversal:
    """Read-side topology queries.

    Holds no state between calls beyond the client, so one instance is safe to
    share across concurrent incidents.
    """

    __slots__ = ("_client",)

    def __init__(self, client: Neo4jClient) -> None:
        self._client = client

    async def _run(
        self,
        operation: str,
        cypher: str,
        params: Mapping[str, Any],
        recorder: QueryRecorder | None,
    ) -> list[dict[str, Any]]:
        if recorder is not None:
            recorder.add(operation, cypher, params)
        return await self._client.run(cypher, dict(params))

    # -------------------------------------------------------------- downstream

    async def blast_radius(
        self,
        service_id: str,
        max_depth: int = 3,
        *,
        limit: int = MAX_RESULTS,
        recorder: QueryRecorder | None = None,
    ) -> BlastRadius:
        """Who breaks when this service breaks - the transitive callers.

        Direction matters: impact flows against the call edge. A service one hop
        upstream of the failure is ``directly_affected``; anything further is
        ``downstream`` of that first ring.
        """
        depth = _clamp_depth(max_depth)
        cypher = rendered_cypher("blast_radius", depth=depth)
        params = {"service_id": service_id, "limit": _clamp_limit(limit, MAX_RESULTS)}
        rows = await self._run("blast_radius", cypher, params, recorder)

        direct = [r["service_id"] for r in rows if int(r["hops"]) == 1]
        indirect = [r["service_id"] for r in rows if int(r["hops"]) > 1]
        customer_facing = any(bool(r.get("customer_facing")) for r in rows)
        impacted = direct + indirect
        share = await self._request_share(service_id, impacted, recorder)
        return BlastRadius(
            directly_affected=direct,
            downstream=indirect,
            customer_facing=customer_facing,
            estimated_request_share=share,
        )

    async def _request_share(
        self, service_id: str, impacted: Sequence[str], recorder: QueryRecorder | None
    ) -> float:
        """Share of observed inbound calls in this environment that hit the set.

        Derived from the ``call_count`` the ingestor observed, not modelled. When
        no counts have been ingested the honest answer is 0.0 - the caller reads
        that as "unmeasured", never as "no impact".
        """
        environment = service_id.split(":", 1)[0]
        if not impacted or not environment:
            return 0.0
        cypher = rendered_cypher("request_share")
        params = {"environment": environment, "impacted": list(impacted[:MAX_RESULTS])}
        rows = await self._run("request_share", cypher, params, recorder)
        if not rows:
            return 0.0
        total = float(rows[0].get("total") or 0.0)
        hit = float(rows[0].get("impacted") or 0.0)
        if total <= 0.0:
            return 0.0
        return max(0.0, min(hit / total, 1.0))

    # ---------------------------------------------------------------- upstream

    async def upstream_dependencies(
        self,
        service_id: str,
        max_depth: int = 3,
        *,
        limit: int = MAX_RESULTS,
        recorder: QueryRecorder | None = None,
    ) -> list[GraphNode]:
        """What this service needs - transitive callees and infrastructure."""
        depth = _clamp_depth(max_depth)
        cypher = rendered_cypher("upstream_dependencies", depth=depth)
        params = {"service_id": service_id, "limit": _clamp_limit(limit, MAX_RESULTS)}
        rows = await self._run("upstream_dependencies", cypher, params, recorder)
        return [node for row in rows if (node := _to_graph_node(row)) is not None]

    # ------------------------------------------------------------ causal paths

    async def causal_paths(
        self,
        from_service: str,
        to_service: str,
        max_depth: int = 5,
        *,
        limit: int = MAX_PATHS,
        recorder: QueryRecorder | None = None,
    ) -> list[CausalPath]:
        """Shortest structural paths between two services, as ordered node lists.

        ``allShortestPaths`` rather than every path: the set of all paths grows
        combinatorially, and an operator reading a propagation story needs the
        few shortest ones, not thousands of variations on them.
        """
        depth = _clamp_depth(max_depth)
        cypher = rendered_cypher("causal_paths", depth=depth)
        params = {
            "from_id": from_service,
            "to_id": to_service,
            "limit": _clamp_limit(limit, MAX_PATHS),
        }
        rows = await self._run("causal_paths", cypher, params, recorder)
        return [
            CausalPath(
                nodes=tuple(str(n) for n in row["path"] if n is not None),
                hops=int(row.get("hops") or 0),
            )
            for row in rows
            if row.get("path")
        ]

    # --------------------------------------------------------- frontend view

    async def neighbourhood(
        self,
        service_id: str,
        depth: int = 2,
        *,
        node_limit: int = MAX_NODES,
        edge_limit: int = MAX_EDGES,
        recorder: QueryRecorder | None = None,
    ) -> dict[str, list[dict[str, Any]]]:
        """Node + edge subgraph shaped for the service-graph view.

        Two queries rather than one: returning nodes and edges in a single result
        set means one of them is repeated per row, and the payload the UI has to
        parse grows with the product of the two instead of the sum.
        """
        applied = _clamp_depth(depth)
        node_cypher = rendered_cypher("neighbourhood_nodes", depth=applied)
        node_params = {
            "service_id": service_id,
            "limit": _clamp_limit(node_limit, MAX_NODES),
        }
        node_rows = await self._run("neighbourhood_nodes", node_cypher, node_params, recorder)
        nodes = [n for row in node_rows if (n := _to_graph_node(row)) is not None]
        known = {n.node_id for n in nodes}

        edge_cypher = rendered_cypher("neighbourhood_edges", depth=applied)
        edge_params = {
            "service_id": service_id,
            "limit": _clamp_limit(edge_limit, MAX_EDGES),
        }
        edge_rows = await self._run("neighbourhood_edges", edge_cypher, edge_params, recorder)
        edges = [e for e in (_to_graph_edge(row) for row in edge_rows) if e is not None]

        return {
            "nodes": [
                {
                    "id": n.node_id,
                    "label": n.label.value,
                    "name": n.name,
                    "hops": n.hops,
                    "environment": n.properties.get("environment"),
                    "health": n.properties.get("health"),
                    "last_seen": _iso(n.properties.get("last_seen")),
                }
                for n in nodes
            ],
            # An edge to a node the node query truncated away would render as a
            # dangling line in the UI, so it is dropped with the node.
            "edges": [
                {
                    "source": e.source_id,
                    "target": e.target_id,
                    "type": e.rel_type.value,
                    "call_count": e.properties.get("call_count"),
                    "error_count": e.properties.get("error_count"),
                    "latency_p99_ms": e.properties.get("latency_p99_ms"),
                }
                for e in edges
                if e.source_id in known and e.target_id in known
            ],
        }

    # ------------------------------------------------------------- ownership

    async def owning_team(
        self, service_id: str, *, recorder: QueryRecorder | None = None
    ) -> TeamRef | None:
        """``None`` means the graph holds no owner - not that Neo4j was down."""
        cypher = rendered_cypher("owning_team")
        params = {"service_id": service_id}
        rows = await self._run("owning_team", cypher, params, recorder)
        if not rows:
            return None
        row = rows[0]
        team_id = row.get("team_id")
        if not team_id:
            return None
        return TeamRef(
            team_id=str(team_id),
            name=str(row.get("name") or team_id),
            contact=row.get("contact"),
        )

    async def recent_deployments_for(
        self,
        service_id: str,
        limit: int = 10,
        *,
        recorder: QueryRecorder | None = None,
    ) -> list[DeploymentSummary]:
        """Most recent deployments first, with the commit each one carried."""
        cypher = rendered_cypher("recent_deployments")
        params = {"service_id": service_id, "limit": _clamp_limit(limit, MAX_RESULTS)}
        rows = await self._run("recent_deployments", cypher, params, recorder)
        return [
            DeploymentSummary(
                deployment_id=str(row["deployment_id"]),
                version=str(row.get("version") or ""),
                status=str(row.get("status") or "unknown"),
                deployed_at=_as_datetime(row.get("deployed_at")),
                commit_sha=row.get("commit_sha"),
                commit_repo=row.get("commit_repo"),
                commit_author=row.get("commit_author"),
                commit_authored_at=_as_datetime(row.get("commit_authored_at")),
            )
            for row in rows
            if row.get("deployment_id")
        ]

    async def services_sharing_dependency(
        self,
        service_id: str,
        *,
        limit: int = MAX_RESULTS,
        recorder: QueryRecorder | None = None,
    ) -> list[SharedDependency]:
        """Peers behind the same datastore, cache or queue.

        This is the correlated-failure question: several services degrading at
        once is a coincidence until they turn out to share a dependency.
        """
        cypher = rendered_cypher("services_sharing_dependency")
        params = {"service_id": service_id, "limit": _clamp_limit(limit, MAX_RESULTS)}
        rows = await self._run("services_sharing_dependency", cypher, params, recorder)
        out: list[SharedDependency] = []
        for row in rows:
            resolved = _resolve_node(
                row.get("labels") or (), {"resource_id": row.get("resource_id")}
            )
            if resolved is None:
                continue
            label, resource_id = resolved
            out.append(
                SharedDependency(
                    resource_id=resource_id,
                    name=str(row.get("name") or resource_id),
                    label=label,
                    service_ids=tuple(sorted(str(s) for s in row.get("service_ids") or ())),
                )
            )
        return out

    async def similar_incidents(
        self,
        incident_id: str,
        limit: int = 5,
        *,
        recorder: QueryRecorder | None = None,
    ) -> list[SimilarIncident]:
        """Prior incidents linked by SIMILAR_TO, highest score first."""
        cypher = rendered_cypher("similar_incidents")
        params = {"incident_id": incident_id, "limit": _clamp_limit(limit, MAX_RESULTS)}
        rows = await self._run("similar_incidents", cypher, params, recorder)
        return [
            SimilarIncident(
                incident_id=str(row["incident_id"]),
                score=float(row.get("score") or 0.0),
                method=row.get("method"),
                severity=row.get("severity"),
                title=row.get("title"),
                started_at=_as_datetime(row.get("started_at")),
            )
            for row in rows
            if row.get("incident_id")
        ]


def _to_graph_edge(row: Mapping[str, Any]) -> GraphEdge | None:
    source = _resolve_node(row.get("source_labels") or (), row.get("source_props") or {})
    target = _resolve_node(row.get("target_labels") or (), row.get("target_props") or {})
    if source is None or target is None:
        return None
    try:
        rel_type = RelType(str(row.get("rel_type")))
    except ValueError:
        return None
    return GraphEdge(
        rel_type=rel_type,
        source_id=source[1],
        target_id=target[1],
        properties=dict(row.get("props") or {}),
    )


def _iso(value: Any) -> str | None:
    parsed = _as_datetime(value)
    return parsed.isoformat() if parsed is not None else None


__all__ = [
    "CYPHER",
    "MAX_DEPTH",
    "MAX_EDGES",
    "MAX_NODES",
    "MAX_PATHS",
    "MAX_RESULTS",
    "CausalPath",
    "DeploymentSummary",
    "GraphEdge",
    "GraphNode",
    "GraphTraversal",
    "QueryRecord",
    "QueryRecorder",
    "SharedDependency",
    "SimilarIncident",
    "TeamRef",
    "provenance_uri",
    "query_hash",
    "rendered_cypher",
]
