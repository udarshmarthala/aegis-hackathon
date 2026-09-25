"""Topology ingestion - every write is a MERGE, on purpose.

Identity in this graph is the canonical ``service_id``, not the runtime object.
A pod restarting, a container being rescheduled or a scale event re-running
discovery must update the existing node, never mint a second one; a duplicated
service silently halves every blast-radius answer that follows (ESD 7).

``last_seen`` is set on every node and edge each time it is observed. That is
what turns "this topology is three weeks old" from an invisible assumption into
a queryable property, so a traversal can be trusted or discounted on evidence
instead of on faith.

Batches are bounded twice: rows per statement, and rows per call. A discovery
source that suddenly reports a million edges degrades into a truncated ingest
plus a warning, not into an unbounded transaction that pins the driver pool.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

from aegis.core.clock import SYSTEM_CLOCK, Clock
from aegis.core.errors import SourceUnavailable, ValidationError
from aegis.core.logging import get_logger
from aegis.domain.models import ServiceRef, ServiceState
from aegis.graph.client import Neo4jClient
from aegis.graph.ontology import (
    INFRASTRUCTURE_LABELS,
    NodeLabel,
    RelType,
    constraints_cypher,
    label_token,
    rel_token,
    validate_edge,
    validate_label,
)

log = get_logger(__name__)

# Rows per Cypher statement. Large enough that ingesting a normal estate is one
# round trip, small enough that one statement can never become a long-running
# write transaction that blocks schema operations.
MAX_BATCH = 500

# Rows accepted per public call. Beyond this the input is not topology, it is a
# runaway discovery source, and truncating loudly beats ingesting forever.
MAX_ROWS = 5_000

# Commit subjects are attacker-influenceable free text (CLAUDE.md 3.7). The node
# stores a truncated copy flagged untrusted so a reader must wrap it before it
# reaches a prompt; the graph never treats it as instruction.
MAX_MESSAGE_CHARS = 500


# --------------------------------------------------------------------------- #
# input records                                                                #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class CallEdge:
    """One observed caller -> callee relationship, with what was measured on it."""

    caller: ServiceRef
    callee: ServiceRef
    call_count: int = 0
    error_count: int = 0
    latency_p99_ms: float | None = None


@dataclass(frozen=True, slots=True)
class DependencyEdge:
    """A service's dependency on a datastore, cache or queue."""

    service: ServiceRef
    resource_id: str
    name: str
    kind: NodeLabel = NodeLabel.DATABASE
    technology: str = ""


@dataclass(frozen=True, slots=True)
class InstanceRecord:
    instance_id: str
    name: str
    state: str = "unknown"


@dataclass(frozen=True, slots=True)
class EndpointRecord:
    route: str
    method: str = "GET"

    def endpoint_id(self, service_id: str) -> str:
        return f"{service_id}:{self.method.upper()}:{self.route}"


@dataclass(frozen=True, slots=True)
class FileChange:
    repo: str
    path: str
    symbols: tuple[str, ...] = ()
    additions: int = 0
    deletions: int = 0

    @property
    def file_id(self) -> str:
        return f"{self.repo}:{self.path}"

    def symbol_id(self, symbol: str) -> str:
        return f"{self.repo}:{self.path}:{symbol}"


@dataclass(frozen=True, slots=True)
class CommitRecord:
    sha: str
    repo: str
    author: str = ""
    authored_at: datetime | None = None
    message: str = ""
    files: tuple[FileChange, ...] = ()


@dataclass(frozen=True, slots=True)
class DeploymentRecord:
    deployment_id: str
    service: ServiceRef
    version: str
    deployed_at: datetime
    status: str = "unknown"
    commit: CommitRecord | None = None


@dataclass(frozen=True, slots=True)
class ObservedCall:
    """A caller/callee pair derived from span parent-child links.

    The field names match ``telemetry.tempo.CallEdge`` so a trace client
    satisfies ``TraceTopologySource`` structurally, with no adapter to drift out
    of step with it.
    """

    caller_service: str
    callee_service: str
    count: int = 0
    p99_ms: float | None = None
    error_count: int = 0


@dataclass(frozen=True, slots=True)
class IngestStats:
    """What an ingest actually wrote, so a caller can log or assert on it."""

    nodes: int = 0
    edges: int = 0
    dropped: int = 0


class ServiceDiscoverySource(Protocol):
    """Anything that can enumerate live service names.

    ``telemetry.prometheus.PrometheusClient`` satisfies this. Depending on the
    protocol rather than the class keeps ingestion testable without a metrics
    backend, and leaves room for a runtime adapter to supply the same list.
    """

    async def known_services(self) -> list[str]: ...


class TraceCallEdge(Protocol):
    """The shape a trace backend reports an observed call in."""

    caller_service: str
    callee_service: str
    count: int


class TraceTopologySource(Protocol):
    """Anything that can derive caller/callee pairs from distributed traces.

    ``telemetry.tempo.TempoClient`` satisfies this as written, so trace-derived
    topology needs no translation layer between the two packages.
    """

    async def service_call_edges(
        self, start: float, end: float, limit: int = 50
    ) -> Sequence[TraceCallEdge]: ...


# --------------------------------------------------------------------------- #
# Cypher - labels and types come from the ontology, values are always bound     #
# --------------------------------------------------------------------------- #

_SERVICE = label_token(NodeLabel.SERVICE)
_INSTANCE = label_token(NodeLabel.INSTANCE)
_DEPLOYMENT = label_token(NodeLabel.DEPLOYMENT)
_COMMIT = label_token(NodeLabel.COMMIT)
_FILE = label_token(NodeLabel.FILE)
_SYMBOL = label_token(NodeLabel.CODE_SYMBOL)
_TEAM = label_token(NodeLabel.TEAM)
_INCIDENT = label_token(NodeLabel.INCIDENT)
_ALERT = label_token(NodeLabel.ALERT)
_REMEDIATION = label_token(NodeLabel.REMEDIATION)
_VERIFICATION = label_token(NodeLabel.VERIFICATION)
_ENDPOINT = label_token(NodeLabel.ENDPOINT)

# Merging the Service node inside every edge statement makes ingestion
# order-independent: a call edge discovered before its endpoints were registered
# still lands correctly instead of being silently dropped by a MATCH.
_MERGE_SERVICE_FRAGMENT = f"""
MERGE (s:{_SERVICE} {{service_id: row.service_id}})
ON CREATE SET s.first_seen = $now
SET s.name = row.name,
    s.environment = row.environment,
    s.workload = row.workload,
    s.last_seen = $now
"""

MERGE_SERVICES = f"""
UNWIND $rows AS row
{_MERGE_SERVICE_FRAGMENT}
SET s.health = coalesce(row.health, s.health),
    s.version = coalesce(row.version, s.version),
    s.desired_instances = coalesce(row.desired_instances, s.desired_instances),
    s.ready_instances = coalesce(row.ready_instances, s.ready_instances),
    s.customer_facing = coalesce(row.customer_facing, s.customer_facing, false)
WITH s, row WHERE row.owner_team IS NOT NULL
MERGE (t:{_TEAM} {{team_id: row.owner_team_id}})
ON CREATE SET t.first_seen = $now
SET t.name = row.owner_team, t.last_seen = $now
MERGE (s)-[o:{rel_token(RelType.OWNED_BY)}]->(t)
ON CREATE SET o.first_seen = $now
SET o.last_seen = $now
"""

MERGE_CALLS = f"""
UNWIND $rows AS row
MERGE (a:{_SERVICE} {{service_id: row.caller_id}})
ON CREATE SET a.first_seen = $now
SET a.name = row.caller_name,
    a.environment = row.environment,
    a.workload = row.workload,
    a.last_seen = $now
MERGE (b:{_SERVICE} {{service_id: row.callee_id}})
ON CREATE SET b.first_seen = $now
SET b.name = row.callee_name,
    b.environment = row.environment,
    b.workload = row.workload,
    b.last_seen = $now
MERGE (a)-[r:{rel_token(RelType.CALLS)}]->(b)
ON CREATE SET r.first_seen = $now
SET r.call_count = row.call_count,
    r.error_count = row.error_count,
    r.latency_p99_ms = row.latency_p99_ms,
    r.last_seen = $now
"""


def _dependency_cypher(kind: NodeLabel) -> str:
    """One template per infrastructure label.

    A label cannot be a bind parameter, so the alternative would be string
    concatenation at call time. Building the templates once from validated enum
    members means the call site only ever selects, never constructs.
    """
    validate_edge(RelType.DEPENDS_ON, NodeLabel.SERVICE, kind)
    return f"""
UNWIND $rows AS row
MERGE (s:{_SERVICE} {{service_id: row.service_id}})
ON CREATE SET s.first_seen = $now
SET s.name = row.name,
    s.environment = row.environment,
    s.workload = row.workload,
    s.last_seen = $now
MERGE (d:{label_token(kind)} {{resource_id: row.resource_id}})
ON CREATE SET d.first_seen = $now
SET d.name = row.resource_name,
    d.environment = row.environment,
    d.technology = row.technology,
    d.last_seen = $now
MERGE (s)-[r:{rel_token(RelType.DEPENDS_ON)}]->(d)
ON CREATE SET r.first_seen = $now
SET r.technology = row.technology, r.last_seen = $now
"""


MERGE_DEPENDENCIES: Mapping[NodeLabel, str] = {
    kind: _dependency_cypher(kind) for kind in sorted(INFRASTRUCTURE_LABELS)
}

MERGE_DEPLOYMENT = f"""
MERGE (s:{_SERVICE} {{service_id: $service_id}})
ON CREATE SET s.first_seen = $now
SET s.name = $service_name,
    s.environment = $environment,
    s.workload = $workload,
    s.last_seen = $now
MERGE (d:{_DEPLOYMENT} {{deployment_id: $deployment_id}})
ON CREATE SET d.first_seen = $now
SET d.service_id = $service_id,
    d.version = $version,
    d.status = $status,
    d.deployed_at = $deployed_at,
    d.last_seen = $now
MERGE (s)-[r:{rel_token(RelType.DEPLOYED_AS)}]->(d)
ON CREATE SET r.first_seen = $now
SET r.last_seen = $now
"""

MERGE_DEPLOYMENT_COMMIT = f"""
MATCH (d:{_DEPLOYMENT} {{deployment_id: $deployment_id}})
MERGE (c:{_COMMIT} {{sha: $sha}})
ON CREATE SET c.first_seen = $now
SET c.repo = $repo,
    c.author = $author,
    c.authored_at = $authored_at,
    c.message = $message,
    c.message_untrusted = true,
    c.last_seen = $now
MERGE (d)-[r:{rel_token(RelType.CREATED_BY)}]->(c)
ON CREATE SET r.first_seen = $now
SET r.last_seen = $now
"""

MERGE_COMMIT_FILES = f"""
UNWIND $rows AS row
MATCH (c:{_COMMIT} {{sha: $sha}})
MERGE (f:{_FILE} {{file_id: row.file_id}})
ON CREATE SET f.first_seen = $now
SET f.path = row.path, f.repo = row.repo, f.last_seen = $now
MERGE (c)-[m:{rel_token(RelType.MODIFIES)}]->(f)
ON CREATE SET m.first_seen = $now
SET m.additions = row.additions, m.deletions = row.deletions, m.last_seen = $now
WITH f, row
UNWIND row.symbols AS symbol
MERGE (y:{_SYMBOL} {{symbol_id: symbol.symbol_id}})
ON CREATE SET y.first_seen = $now
SET y.name = symbol.name, y.file_id = row.file_id, y.last_seen = $now
MERGE (f)-[e:{rel_token(RelType.DEFINES)}]->(y)
ON CREATE SET e.first_seen = $now
SET e.last_seen = $now
"""

MERGE_INCIDENT = f"""
MERGE (i:{_INCIDENT} {{incident_id: $incident_id}})
ON CREATE SET i.first_seen = $now
SET i.severity = $severity,
    i.title = $title,
    i.started_at = $started_at,
    i.state = $state,
    i.last_seen = $now
"""

MERGE_INCIDENT_SERVICES = f"""
MATCH (i:{_INCIDENT} {{incident_id: $incident_id}})
UNWIND $rows AS row
MERGE (s:{_SERVICE} {{service_id: row.service_id}})
ON CREATE SET s.first_seen = $now
SET s.name = row.name,
    s.environment = row.environment,
    s.workload = row.workload,
    s.last_seen = $now
MERGE (i)-[r:{rel_token(RelType.AFFECTS)}]->(s)
ON CREATE SET r.first_seen = $now
SET r.last_seen = $now
"""

MERGE_INCIDENT_ALERTS = f"""
MATCH (i:{_INCIDENT} {{incident_id: $incident_id}})
UNWIND $rows AS row
MERGE (a:{_ALERT} {{alert_id: row.alert_id}})
ON CREATE SET a.first_seen = $now
SET a.source = row.source,
    a.severity = row.severity,
    a.received_at = row.received_at,
    a.last_seen = $now
MERGE (i)-[r:{rel_token(RelType.RELATES_TO)}]->(a)
ON CREATE SET r.first_seen = $now
SET r.last_seen = $now
"""

# Incident, Alert, Remediation and Verification are projections keyed by an id
# Postgres already owns, so merging the stub is harmless and keeps ingestion
# order-independent. Service, Deployment and Commit endpoints are MATCHed
# instead: inventing a topology node that was never observed is the one thing
# this package must never do.
MERGE_CAUSAL_PATH = f"""
MERGE (i:{_INCIDENT} {{incident_id: $incident_id}})
ON CREATE SET i.first_seen = $now
SET i.last_seen = $now
WITH i
UNWIND $rows AS row
MATCH (s:{_SERVICE} {{service_id: row.service_id}})
MERGE (i)-[r:{rel_token(RelType.PASSES_THROUGH)}]->(s)
ON CREATE SET r.first_seen = $now
SET r.position = row.position, r.last_seen = $now
"""


def _caused_by_cypher(kind: NodeLabel) -> str:
    validate_edge(RelType.CAUSED_BY, NodeLabel.INCIDENT, kind)
    key = {
        NodeLabel.SERVICE: "service_id",
        NodeLabel.DEPLOYMENT: "deployment_id",
        NodeLabel.COMMIT: "sha",
    }[kind]
    return f"""
MERGE (i:{_INCIDENT} {{incident_id: $incident_id}})
ON CREATE SET i.first_seen = $now
SET i.last_seen = $now
WITH i
MATCH (t:{label_token(kind)} {{{key}: $target_key}})
MERGE (i)-[r:{rel_token(RelType.CAUSED_BY)}]->(t)
ON CREATE SET r.first_seen = $now
SET r.confidence = $confidence,
    r.evidence_ids = $evidence_ids,
    r.last_seen = $now
"""


MERGE_CAUSED_BY: Mapping[NodeLabel, str] = {
    kind: _caused_by_cypher(kind)
    for kind in (NodeLabel.SERVICE, NodeLabel.DEPLOYMENT, NodeLabel.COMMIT)
}

MERGE_REMEDIATION = f"""
MERGE (m:{_REMEDIATION} {{action_id: $action_id}})
ON CREATE SET m.first_seen = $now
SET m.action_type = $action_type,
    m.state = $state,
    m.executed_at = $executed_at,
    m.last_seen = $now
WITH m
MERGE (i:{_INCIDENT} {{incident_id: $incident_id}})
ON CREATE SET i.first_seen = $now
SET i.last_seen = $now
MERGE (m)-[a:{rel_token(RelType.ASSOCIATED_WITH)}]->(i)
ON CREATE SET a.first_seen = $now
SET a.last_seen = $now
WITH m
MATCH (s:{_SERVICE} {{service_id: $service_id}})
MERGE (m)-[t:{rel_token(RelType.TARGETS)}]->(s)
ON CREATE SET t.first_seen = $now
SET t.last_seen = $now
"""

MERGE_VERIFICATION = f"""
MERGE (m:{_REMEDIATION} {{action_id: $action_id}})
ON CREATE SET m.first_seen = $now
SET m.last_seen = $now
WITH m
MERGE (v:{_VERIFICATION} {{verification_id: $verification_id}})
ON CREATE SET v.first_seen = $now
SET v.passed = $passed,
    v.summary = $summary,
    v.verified_at = $verified_at,
    v.last_seen = $now
MERGE (m)-[r:{rel_token(RelType.VERIFIED_BY)}]->(v)
ON CREATE SET r.first_seen = $now
SET r.last_seen = $now
"""

MERGE_SIMILAR_INCIDENTS = f"""
UNWIND $rows AS row
MERGE (a:{_INCIDENT} {{incident_id: row.low_id}})
ON CREATE SET a.first_seen = $now
SET a.last_seen = $now
MERGE (b:{_INCIDENT} {{incident_id: row.high_id}})
ON CREATE SET b.first_seen = $now
SET b.last_seen = $now
MERGE (a)-[r:{rel_token(RelType.SIMILAR_TO)}]->(b)
ON CREATE SET r.first_seen = $now
SET r.score = row.score, r.method = $method, r.last_seen = $now
"""

MERGE_INSTANCES = f"""
MATCH (s:{_SERVICE} {{service_id: $service_id}})
UNWIND $rows AS row
MERGE (n:{_INSTANCE} {{instance_id: row.instance_id}})
ON CREATE SET n.first_seen = $now
SET n.name = row.name,
    n.service_id = $service_id,
    n.state = row.state,
    n.last_seen = $now
MERGE (s)-[r:{rel_token(RelType.RUNS_ON)}]->(n)
ON CREATE SET r.first_seen = $now
SET r.last_seen = $now
"""

MERGE_ENDPOINTS = f"""
MATCH (s:{_SERVICE} {{service_id: $service_id}})
UNWIND $rows AS row
MERGE (e:{_ENDPOINT} {{endpoint_id: row.endpoint_id}})
ON CREATE SET e.first_seen = $now
SET e.route = row.route,
    e.method = row.method,
    e.service_id = $service_id,
    e.last_seen = $now
MERGE (s)-[r:{rel_token(RelType.EXPOSES)}]->(e)
ON CREATE SET r.first_seen = $now
SET r.last_seen = $now
"""


# --------------------------------------------------------------------------- #
# helpers                                                                      #
# --------------------------------------------------------------------------- #


def _chunks(rows: Sequence[Any], size: int = MAX_BATCH) -> Iterator[Sequence[Any]]:
    for start in range(0, len(rows), size):
        yield rows[start : start + size]


def _service_row(ref: ServiceRef) -> dict[str, Any]:
    return {
        "service_id": ref.service_id,
        "name": ref.name,
        "environment": ref.environment,
        "workload": ref.workload,
    }


def _team_id(name: str) -> str:
    """Slugged so a team renamed in one source does not fork into two nodes."""
    return name.strip().lower().replace(" ", "-")


def _truncate(text: str) -> str:
    return text[:MAX_MESSAGE_CHARS]


@dataclass(slots=True)
class _Counter:
    nodes: int = 0
    edges: int = 0
    dropped: int = 0
    seen: set[str] = field(default_factory=set)


class TopologyIngestor:
    """Writes topology into Neo4j. Idempotent by construction, bounded by policy.

    Neo4j is a soft dependency: every method here surfaces ``SourceUnavailable``
    from the client untouched, and the caller records an evidence gap. Ingestion
    failing degrades later blast-radius answers; it never fails an incident.
    """

    __slots__ = ("_client", "_clock", "_max_batch")

    def __init__(
        self,
        client: Neo4jClient,
        *,
        clock: Clock = SYSTEM_CLOCK,
        max_batch: int = MAX_BATCH,
    ) -> None:
        if not 1 <= max_batch <= MAX_BATCH:
            raise ValueError(f"max_batch must be between 1 and {MAX_BATCH}")
        self._client = client
        self._clock = clock
        self._max_batch = max_batch

    # ---------------------------------------------------------------- schema

    async def ensure_schema(self) -> int:
        """Apply every constraint and index. Safe to call on every boot."""
        statements = constraints_cypher()
        for statement in statements:
            await self._client.write(statement, {})
        log.info("graph schema ensured", statements=len(statements))
        return len(statements)

    # -------------------------------------------------------------- services

    async def ingest_services(
        self, services: Sequence[ServiceState] | Sequence[ServiceRef]
    ) -> IngestStats:
        """MERGE Service nodes by ``service_id``, plus OWNED_BY when known."""
        rows, dropped = self._bound(
            [self._service_state_row(s) for s in services], what="services"
        )
        if not rows:
            return IngestStats(dropped=dropped)
        for batch in _chunks(rows, self._max_batch):
            await self._client.write(MERGE_SERVICES, {"rows": list(batch), "now": self._now()})
        owned = sum(1 for r in rows if r["owner_team"] is not None)
        log.info("services ingested", count=len(rows), owned=owned, dropped=dropped)
        return IngestStats(nodes=len(rows), edges=owned, dropped=dropped)

    @staticmethod
    def _service_state_row(service: ServiceState | ServiceRef) -> dict[str, Any]:
        ref = service.ref if isinstance(service, ServiceState) else service
        row = _service_row(ref)
        row.update(
            {
                "health": None,
                "version": None,
                "desired_instances": None,
                "ready_instances": None,
                "customer_facing": None,
                "owner_team": None,
                "owner_team_id": None,
            }
        )
        if isinstance(service, ServiceState):
            row["health"] = service.health.value
            row["version"] = service.version
            row["desired_instances"] = service.desired_instances
            row["ready_instances"] = service.ready_instances
            if service.owner_team:
                row["owner_team"] = service.owner_team
                row["owner_team_id"] = _team_id(service.owner_team)
        return row

    async def ingest_call_edges(self, edges: Sequence[CallEdge]) -> IngestStats:
        """MERGE ``(:Service)-[:CALLS]->(:Service)`` with observed traffic."""
        rows, dropped = self._bound(
            [
                {
                    "caller_id": e.caller.service_id,
                    "caller_name": e.caller.name,
                    "callee_id": e.callee.service_id,
                    "callee_name": e.callee.name,
                    "environment": e.callee.environment,
                    "workload": e.callee.workload,
                    "call_count": max(0, e.call_count),
                    "error_count": max(0, e.error_count),
                    "latency_p99_ms": e.latency_p99_ms,
                }
                for e in edges
                if e.caller.service_id != e.callee.service_id
            ],
            what="call_edges",
        )
        if not rows:
            return IngestStats(dropped=dropped)
        for batch in _chunks(rows, self._max_batch):
            await self._client.write(MERGE_CALLS, {"rows": list(batch), "now": self._now()})
        log.info("call edges ingested", count=len(rows), dropped=dropped)
        return IngestStats(edges=len(rows), dropped=dropped)

    async def ingest_dependencies(self, dependencies: Sequence[DependencyEdge]) -> IngestStats:
        """MERGE ``(:Service)-[:DEPENDS_ON]->(:Database|:Cache|:Queue)``."""
        by_kind: dict[NodeLabel, list[dict[str, Any]]] = {}
        for dep in dependencies:
            kind = validate_label(dep.kind)
            if kind not in INFRASTRUCTURE_LABELS:
                raise ValidationError(
                    "DEPENDS_ON target must be infrastructure",
                    context={
                        "kind": kind.value,
                        "allowed": sorted(x.value for x in INFRASTRUCTURE_LABELS),
                    },
                )
            row = _service_row(dep.service)
            row.update(
                {
                    "resource_id": dep.resource_id,
                    "resource_name": dep.name,
                    "technology": dep.technology,
                }
            )
            by_kind.setdefault(kind, []).append(row)

        written = 0
        dropped = 0
        for kind in sorted(by_kind):
            rows, kind_dropped = self._bound(by_kind[kind], what=f"dependencies:{kind.value}")
            dropped += kind_dropped
            for batch in _chunks(rows, self._max_batch):
                await self._client.write(
                    MERGE_DEPENDENCIES[kind], {"rows": list(batch), "now": self._now()}
                )
            written += len(rows)
        log.info("dependencies ingested", count=written, dropped=dropped)
        return IngestStats(nodes=written, edges=written, dropped=dropped)

    async def ingest_instances(
        self, service: ServiceRef, instances: Sequence[InstanceRecord]
    ) -> IngestStats:
        """MERGE ``(:Service)-[:RUNS_ON]->(:Instance)``.

        Instances churn far faster than services, which is exactly why they are
        separate nodes: an instance disappearing must not touch service identity.
        """
        rows, dropped = self._bound(
            [{"instance_id": i.instance_id, "name": i.name, "state": i.state} for i in instances],
            what="instances",
        )
        if not rows:
            return IngestStats(dropped=dropped)
        for batch in _chunks(rows, self._max_batch):
            await self._client.write(
                MERGE_INSTANCES,
                {"service_id": service.service_id, "rows": list(batch), "now": self._now()},
            )
        return IngestStats(nodes=len(rows), edges=len(rows), dropped=dropped)

    async def ingest_endpoints(
        self, service: ServiceRef, endpoints: Sequence[EndpointRecord]
    ) -> IngestStats:
        """MERGE ``(:Service)-[:EXPOSES]->(:Endpoint)``."""
        rows, dropped = self._bound(
            [
                {
                    "endpoint_id": e.endpoint_id(service.service_id),
                    "route": e.route,
                    "method": e.method.upper(),
                }
                for e in endpoints
            ],
            what="endpoints",
        )
        if not rows:
            return IngestStats(dropped=dropped)
        for batch in _chunks(rows, self._max_batch):
            await self._client.write(
                MERGE_ENDPOINTS,
                {"service_id": service.service_id, "rows": list(batch), "now": self._now()},
            )
        return IngestStats(nodes=len(rows), edges=len(rows), dropped=dropped)

    # ----------------------------------------------------------- change lineage

    async def ingest_deployment(self, deployment: DeploymentRecord) -> IngestStats:
        """Deployment, its service, its commit, and that commit's code lineage.

        Change lineage is the highest-value edge set in the graph: most incidents
        correlate with a change, and "which symbol did the deploy that preceded
        this touch" is a traversal, not a search.
        """
        now = self._now()
        ref = deployment.service
        await self._client.write(
            MERGE_DEPLOYMENT,
            {
                "deployment_id": deployment.deployment_id,
                "service_id": ref.service_id,
                "service_name": ref.name,
                "environment": ref.environment,
                "workload": ref.workload,
                "version": deployment.version,
                "status": deployment.status,
                "deployed_at": deployment.deployed_at,
                "now": now,
            },
        )
        stats = IngestStats(nodes=2, edges=1)
        commit = deployment.commit
        if commit is None:
            return stats

        await self._client.write(
            MERGE_DEPLOYMENT_COMMIT,
            {
                "deployment_id": deployment.deployment_id,
                "sha": commit.sha,
                "repo": commit.repo,
                "author": commit.author,
                "authored_at": commit.authored_at,
                "message": _truncate(commit.message),
                "now": now,
            },
        )

        file_rows, dropped = self._bound(
            [
                {
                    "file_id": f.file_id,
                    "path": f.path,
                    "repo": f.repo,
                    "additions": f.additions,
                    "deletions": f.deletions,
                    "symbols": [
                        {"symbol_id": f.symbol_id(name), "name": name} for name in f.symbols
                    ],
                }
                for f in commit.files
            ],
            what="commit_files",
        )
        for batch in _chunks(file_rows, self._max_batch):
            await self._client.write(
                MERGE_COMMIT_FILES, {"sha": commit.sha, "rows": list(batch), "now": now}
            )
        symbols = sum(len(r["symbols"]) for r in file_rows)
        log.info(
            "deployment ingested",
            deployment_id=deployment.deployment_id,
            service_id=ref.service_id,
            commit=commit.sha,
            files=len(file_rows),
            symbols=symbols,
        )
        return IngestStats(
            nodes=stats.nodes + 1 + len(file_rows) + symbols,
            edges=stats.edges + 1 + len(file_rows) + symbols,
            dropped=dropped,
        )

    # ---------------------------------------------------------------- incidents

    async def ingest_incident(
        self,
        *,
        incident_id: str,
        severity: str,
        started_at: datetime,
        services: Sequence[ServiceRef] = (),
        alerts: Sequence[Mapping[str, Any]] = (),
        title: str = "",
        state: str = "",
    ) -> IngestStats:
        """Project an incident into the graph: AFFECTS services, RELATES_TO alerts.

        This is a projection of the Postgres row, never a second source of truth.
        Only the properties a traversal ranks on are copied.
        """
        now = self._now()
        await self._client.write(
            MERGE_INCIDENT,
            {
                "incident_id": incident_id,
                "severity": severity,
                "title": _truncate(title),
                "started_at": started_at,
                "state": state,
                "now": now,
            },
        )
        service_rows, dropped = self._bound(
            [_service_row(s) for s in services], what="incident_services"
        )
        for batch in _chunks(service_rows, self._max_batch):
            await self._client.write(
                MERGE_INCIDENT_SERVICES,
                {"incident_id": incident_id, "rows": list(batch), "now": now},
            )

        alert_rows, alert_dropped = self._bound(
            [
                {
                    "alert_id": str(a["alert_id"]),
                    "source": str(a.get("source", "")),
                    "severity": str(a.get("severity", "")),
                    "received_at": a.get("received_at"),
                }
                for a in alerts
                if a.get("alert_id")
            ],
            what="incident_alerts",
        )
        for batch in _chunks(alert_rows, self._max_batch):
            await self._client.write(
                MERGE_INCIDENT_ALERTS,
                {"incident_id": incident_id, "rows": list(batch), "now": now},
            )
        return IngestStats(
            nodes=1 + len(service_rows) + len(alert_rows),
            edges=len(service_rows) + len(alert_rows),
            dropped=dropped + alert_dropped,
        )

    async def ingest_causal_path(
        self, *, incident_id: str, path: Sequence[ServiceRef]
    ) -> IngestStats:
        """Record the ordered propagation path as PASSES_THROUGH with a position.

        Only services already in the graph are linked; the MATCH is deliberate,
        so a path naming an unknown service records fewer hops rather than
        inventing topology that was never observed.
        """
        rows, dropped = self._bound(
            [{"service_id": ref.service_id, "position": i} for i, ref in enumerate(path)],
            what="causal_path",
        )
        if not rows:
            return IngestStats(dropped=dropped)
        await self._client.write(
            MERGE_CAUSAL_PATH, {"incident_id": incident_id, "rows": rows, "now": self._now()}
        )
        return IngestStats(edges=len(rows), dropped=dropped)

    async def link_probable_cause(
        self,
        *,
        incident_id: str,
        target_label: NodeLabel,
        target_key: str,
        confidence: float,
        evidence_ids: Sequence[str],
    ) -> IngestStats:
        """CAUSED_BY - the one edge that carries a claim rather than an observation.

        It therefore refuses to exist without citations: an uncited cause edge
        would read exactly like observed topology to every later traversal
        (CLAUDE.md 3.2).
        """
        kind = validate_label(target_label)
        if kind not in MERGE_CAUSED_BY:
            raise ValidationError(
                "incident cause must be a service, deployment or commit",
                context={"label": kind.value},
            )
        if not evidence_ids:
            raise ValidationError(
                "CAUSED_BY requires at least one evidence reference",
                context={"incident_id": incident_id, "target": target_key},
            )
        if not 0.0 <= confidence <= 1.0:
            raise ValidationError(
                "confidence must be between 0 and 1", context={"confidence": confidence}
            )
        await self._client.write(
            MERGE_CAUSED_BY[kind],
            {
                "incident_id": incident_id,
                "target_key": target_key,
                "confidence": float(confidence),
                "evidence_ids": list(evidence_ids[:MAX_BATCH]),
                "now": self._now(),
            },
        )
        return IngestStats(edges=1)

    async def link_similar_incidents(
        self,
        incident_id: str,
        similar: Sequence[tuple[str, float]],
        *,
        method: str = "structural",
    ) -> IngestStats:
        """SIMILAR_TO with a score.

        The pair is written in sorted id order so that linking A->B and later
        B->A converges on one edge instead of two mirrored ones that would each
        be counted as an independent precedent.
        """
        rows: list[dict[str, Any]] = []
        for other_id, score in similar:
            if other_id == incident_id:
                continue
            low, high = sorted((incident_id, other_id))
            rows.append({"low_id": low, "high_id": high, "score": float(score)})
        bounded, dropped = self._bound(rows, what="similar_incidents")
        if not bounded:
            return IngestStats(dropped=dropped)
        for batch in _chunks(bounded, self._max_batch):
            await self._client.write(
                MERGE_SIMILAR_INCIDENTS,
                {"rows": list(batch), "method": method, "now": self._now()},
            )
        return IngestStats(edges=len(bounded), dropped=dropped)

    # ------------------------------------------------------------- remediation

    async def ingest_remediation(
        self,
        *,
        action_id: str,
        incident_id: str,
        service: ServiceRef,
        action_type: str,
        state: str,
        executed_at: datetime | None = None,
    ) -> IngestStats:
        """Project an executed action so future incidents can see what was tried."""
        await self._client.write(
            MERGE_REMEDIATION,
            {
                "action_id": action_id,
                "incident_id": incident_id,
                "service_id": service.service_id,
                "action_type": action_type,
                "state": state,
                "executed_at": executed_at,
                "now": self._now(),
            },
        )
        return IngestStats(nodes=1, edges=2)

    async def ingest_verification(
        self,
        *,
        verification_id: str,
        action_id: str,
        passed: bool,
        verified_at: datetime | None = None,
        summary: str = "",
    ) -> IngestStats:
        """Attach the deterministic verification outcome to its remediation."""
        await self._client.write(
            MERGE_VERIFICATION,
            {
                "verification_id": verification_id,
                "action_id": action_id,
                "passed": bool(passed),
                "verified_at": verified_at,
                "summary": _truncate(summary),
                "now": self._now(),
            },
        )
        return IngestStats(nodes=1, edges=1)

    # -------------------------------------------------------- discovery paths

    async def ingest_from_telemetry(
        self,
        prometheus: ServiceDiscoverySource,
        *,
        environment: str,
        workload: str = "default",
    ) -> list[ServiceRef]:
        """Discover services from metrics and refresh their nodes.

        This is the real ingestion path for the local reference environment:
        topology is observed, never declared in a fixture. A discovery failure
        propagates as ``SourceUnavailable`` - an empty list would be
        indistinguishable from "this environment runs nothing" and would silently
        age out every service's ``last_seen``.
        """
        names = await prometheus.known_services()
        refs = [
            ServiceRef.build(environment, workload, name)
            for name in sorted({n for n in names if n})
        ]
        if not refs:
            log.info(
                "telemetry reported no services", environment=environment, workload=workload
            )
            return []
        await self.ingest_services(refs)
        log.info(
            "topology refreshed from telemetry",
            environment=environment,
            workload=workload,
            services=len(refs),
        )
        return refs

    async def ingest_from_traces(
        self,
        source: TraceTopologySource | None,
        *,
        environment: str,
        workload: str = "default",
        lookback_s: float = 3600.0,
        limit: int = 50,
    ) -> IngestStats:
        """Derive CALLS edges from span parent/child service pairs.

        An unconfigured trace source is a missing capability, not an empty
        topology, so it raises instead of returning zero edges. Returning zero
        would let an investigation conclude "nothing calls this service" from the
        absence of a backend (PRD 13).
        """
        if source is None:
            raise SourceUnavailable(
                "trace topology source is not configured",
                context={"dependency": "traces", "environment": environment},
            )
        end = self._now().timestamp()
        observed = await source.service_call_edges(
            start=end - max(1.0, lookback_s), end=end, limit=limit
        )
        edges = [
            CallEdge(
                caller=ServiceRef.build(environment, workload, call.caller_service),
                callee=ServiceRef.build(environment, workload, call.callee_service),
                call_count=int(call.count),
                # Not every trace backend reports failures per edge; absent is 0
                # rather than an invented number.
                error_count=int(getattr(call, "error_count", 0) or 0),
                latency_p99_ms=getattr(call, "p99_ms", None),
            )
            for call in observed
            if call.caller_service and call.callee_service
        ]
        stats = await self.ingest_call_edges(edges)
        log.info(
            "topology refreshed from traces",
            environment=environment,
            workload=workload,
            observed=len(observed),
            edges=stats.edges,
            lookback_s=lookback_s,
        )
        return stats

    # ------------------------------------------------------------------ internals

    def _now(self) -> datetime:
        return self._clock.now()

    @staticmethod
    def _bound(rows: list[dict[str, Any]], *, what: str) -> tuple[list[dict[str, Any]], int]:
        """Cap a batch, loudly. Silent truncation is how a partial graph starts
        looking like a complete one.
        """
        if len(rows) <= MAX_ROWS:
            return rows, 0
        dropped = len(rows) - MAX_ROWS
        log.warning("ingest batch truncated", what=what, kept=MAX_ROWS, dropped=dropped)
        return rows[:MAX_ROWS], dropped


def call_edges_from_names(
    pairs: Iterable[tuple[str, str]], *, environment: str, workload: str = "default"
) -> list[CallEdge]:
    """Convenience for adapters that only know bare service names."""
    return [
        CallEdge(
            caller=ServiceRef.build(environment, workload, caller),
            callee=ServiceRef.build(environment, workload, callee),
        )
        for caller, callee in pairs
        if caller and callee
    ]


__all__ = [
    "MAX_BATCH",
    "MAX_ROWS",
    "CallEdge",
    "CommitRecord",
    "DependencyEdge",
    "DeploymentRecord",
    "EndpointRecord",
    "FileChange",
    "IngestStats",
    "InstanceRecord",
    "ObservedCall",
    "ServiceDiscoverySource",
    "TraceCallEdge",
    "TopologyIngestor",
    "TraceTopologySource",
    "call_edges_from_names",
]
