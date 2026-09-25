"""The operational graph schema - one authoritative definition, no second copy.

What belongs in Neo4j, and what does not
----------------------------------------
Neo4j holds **topology**: what calls what, what a service depends on, which
deployment carried which commit, who owns a service, and the structural path an
incident propagated along. Those questions are traversals, and a traversal
expressed as a recursive join degrades exactly when the estate gets large enough
to need it (ESD 22, 23).

**Postgres stays the system of record.** Incident state, evidence rows, policy
decisions, approvals, actions and audit entries are written there and read from
there. The ``Incident``, ``Alert``, ``Remediation`` and ``Verification`` nodes
defined here are *projections*: an identifier plus the handful of properties a
traversal needs to rank and join. Nothing decides anything from them. When the
graph and Postgres disagree, Postgres is right and the graph is stale - which is
why every node carries ``last_seen`` (CLAUDE.md 3.10).

Injection safety
----------------
Labels and relationship types cannot be parameterised in Cypher; they are
interpolated into the pattern. So they are a closed enum here, and
``label_token`` / ``rel_token`` are the only supported way to turn a string into
one. A caller that passes ``"Service) DETACH DELETE (n"`` gets a
``ValidationError``, never a clause.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum

from aegis.core.errors import ValidationError


class NodeLabel(StrEnum):
    """Every label Aegis is allowed to write or match."""

    SERVICE = "Service"
    INSTANCE = "Instance"
    DATABASE = "Database"
    CACHE = "Cache"
    QUEUE = "Queue"
    DEPLOYMENT = "Deployment"
    COMMIT = "Commit"
    FILE = "File"
    CODE_SYMBOL = "CodeSymbol"
    TEAM = "Team"
    INCIDENT = "Incident"
    ALERT = "Alert"
    REMEDIATION = "Remediation"
    VERIFICATION = "Verification"
    ENDPOINT = "Endpoint"


class RelType(StrEnum):
    """Every relationship type Aegis is allowed to write or traverse."""

    CALLS = "CALLS"
    DEPENDS_ON = "DEPENDS_ON"
    DEPLOYED_AS = "DEPLOYED_AS"
    CREATED_BY = "CREATED_BY"
    MODIFIES = "MODIFIES"
    DEFINES = "DEFINES"
    OWNED_BY = "OWNED_BY"
    AFFECTS = "AFFECTS"
    CAUSED_BY = "CAUSED_BY"
    ASSOCIATED_WITH = "ASSOCIATED_WITH"
    SIMILAR_TO = "SIMILAR_TO"
    TARGETS = "TARGETS"
    VERIFIED_BY = "VERIFIED_BY"
    RELATES_TO = "RELATES_TO"
    PASSES_THROUGH = "PASSES_THROUGH"
    EXPOSES = "EXPOSES"
    RUNS_ON = "RUNS_ON"


# Written on every node and every edge by the ingestor. ``last_seen`` is what
# makes a stale subgraph visible instead of silently authoritative: a service
# that stopped reporting keeps its node, and its age becomes a fact the caller
# can weigh rather than an absence it cannot detect.
TEMPORAL_PROPERTIES: tuple[str, ...] = ("first_seen", "last_seen")


@dataclass(frozen=True, slots=True)
class NodeSpec:
    """A label, the property that identifies it, and what a write must supply.

    ``key`` is deliberately a single property. A composite key would need a
    composite uniqueness constraint, which is not available on every Neo4j
    edition this has to run on, and an unconstrained MERGE is how a churning pod
    quietly becomes two nodes (ESD 7).
    """

    label: NodeLabel
    key: str
    required: tuple[str, ...]
    indexed: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class RelSpec:
    """A relationship type and the endpoint labels it is legal between."""

    rel_type: RelType
    source: tuple[NodeLabel, ...]
    target: tuple[NodeLabel, ...]
    properties: tuple[str, ...] = ()


NODES: Mapping[NodeLabel, NodeSpec] = {
    NodeLabel.SERVICE: NodeSpec(
        label=NodeLabel.SERVICE,
        key="service_id",
        required=("service_id", "name", "environment", "workload"),
        indexed=("environment", "last_seen"),
    ),
    NodeLabel.INSTANCE: NodeSpec(
        label=NodeLabel.INSTANCE,
        key="instance_id",
        required=("instance_id", "name"),
        indexed=("last_seen",),
    ),
    NodeLabel.DATABASE: NodeSpec(
        label=NodeLabel.DATABASE,
        key="resource_id",
        required=("resource_id", "name"),
        indexed=("environment",),
    ),
    NodeLabel.CACHE: NodeSpec(
        label=NodeLabel.CACHE,
        key="resource_id",
        required=("resource_id", "name"),
        indexed=("environment",),
    ),
    NodeLabel.QUEUE: NodeSpec(
        label=NodeLabel.QUEUE,
        key="resource_id",
        required=("resource_id", "name"),
        indexed=("environment",),
    ),
    NodeLabel.DEPLOYMENT: NodeSpec(
        label=NodeLabel.DEPLOYMENT,
        key="deployment_id",
        required=("deployment_id", "service_id", "version"),
        indexed=("deployed_at", "service_id"),
    ),
    NodeLabel.COMMIT: NodeSpec(
        label=NodeLabel.COMMIT,
        key="sha",
        required=("sha",),
        indexed=("authored_at",),
    ),
    NodeLabel.FILE: NodeSpec(
        label=NodeLabel.FILE,
        key="file_id",
        required=("file_id", "path"),
        indexed=("repo",),
    ),
    NodeLabel.CODE_SYMBOL: NodeSpec(
        label=NodeLabel.CODE_SYMBOL,
        key="symbol_id",
        required=("symbol_id", "name"),
        indexed=("file_id",),
    ),
    NodeLabel.TEAM: NodeSpec(
        label=NodeLabel.TEAM,
        key="team_id",
        required=("team_id", "name"),
    ),
    NodeLabel.INCIDENT: NodeSpec(
        label=NodeLabel.INCIDENT,
        key="incident_id",
        required=("incident_id",),
        indexed=("started_at", "severity"),
    ),
    NodeLabel.ALERT: NodeSpec(
        label=NodeLabel.ALERT,
        key="alert_id",
        required=("alert_id",),
        indexed=("received_at",),
    ),
    NodeLabel.REMEDIATION: NodeSpec(
        label=NodeLabel.REMEDIATION,
        key="action_id",
        required=("action_id", "action_type"),
        indexed=("executed_at",),
    ),
    NodeLabel.VERIFICATION: NodeSpec(
        label=NodeLabel.VERIFICATION,
        key="verification_id",
        required=("verification_id", "passed"),
        indexed=("verified_at",),
    ),
    NodeLabel.ENDPOINT: NodeSpec(
        label=NodeLabel.ENDPOINT,
        key="endpoint_id",
        required=("endpoint_id", "route"),
        indexed=("service_id",),
    ),
}

# Infrastructure a service can DEPENDS_ON. Kept as a set so the ingestor can
# reject "depends on an Incident" without enumerating the negative cases.
INFRASTRUCTURE_LABELS: frozenset[NodeLabel] = frozenset(
    {NodeLabel.DATABASE, NodeLabel.CACHE, NodeLabel.QUEUE}
)

# Labels an Incident may be attributed to. CAUSED_BY is the only edge in the
# ontology that encodes a *claim* rather than an observation, which is why its
# spec demands ``evidence_ids`` and ``confidence`` (CLAUDE.md 3.2).
CAUSE_LABELS: frozenset[NodeLabel] = frozenset(
    {NodeLabel.SERVICE, NodeLabel.DEPLOYMENT, NodeLabel.COMMIT}
)

_SERVICE = (NodeLabel.SERVICE,)

RELATIONSHIPS: Mapping[RelType, RelSpec] = {
    RelType.CALLS: RelSpec(
        rel_type=RelType.CALLS,
        source=_SERVICE,
        target=_SERVICE,
        properties=("call_count", "error_count", "latency_p99_ms"),
    ),
    RelType.DEPENDS_ON: RelSpec(
        rel_type=RelType.DEPENDS_ON,
        source=_SERVICE,
        target=tuple(sorted(INFRASTRUCTURE_LABELS)),
        properties=("technology",),
    ),
    RelType.DEPLOYED_AS: RelSpec(
        rel_type=RelType.DEPLOYED_AS,
        source=_SERVICE,
        target=(NodeLabel.DEPLOYMENT,),
    ),
    RelType.CREATED_BY: RelSpec(
        rel_type=RelType.CREATED_BY,
        source=(NodeLabel.DEPLOYMENT,),
        target=(NodeLabel.COMMIT,),
    ),
    RelType.MODIFIES: RelSpec(
        rel_type=RelType.MODIFIES,
        source=(NodeLabel.COMMIT,),
        target=(NodeLabel.FILE,),
        properties=("additions", "deletions"),
    ),
    RelType.DEFINES: RelSpec(
        rel_type=RelType.DEFINES,
        source=(NodeLabel.FILE,),
        target=(NodeLabel.CODE_SYMBOL,),
    ),
    RelType.OWNED_BY: RelSpec(
        rel_type=RelType.OWNED_BY,
        source=_SERVICE,
        target=(NodeLabel.TEAM,),
    ),
    RelType.AFFECTS: RelSpec(
        rel_type=RelType.AFFECTS,
        source=(NodeLabel.INCIDENT,),
        target=_SERVICE,
    ),
    RelType.CAUSED_BY: RelSpec(
        rel_type=RelType.CAUSED_BY,
        source=(NodeLabel.INCIDENT,),
        target=tuple(sorted(CAUSE_LABELS)),
        properties=("confidence", "evidence_ids"),
    ),
    RelType.ASSOCIATED_WITH: RelSpec(
        rel_type=RelType.ASSOCIATED_WITH,
        source=(NodeLabel.REMEDIATION,),
        target=(NodeLabel.INCIDENT,),
    ),
    RelType.SIMILAR_TO: RelSpec(
        rel_type=RelType.SIMILAR_TO,
        source=(NodeLabel.INCIDENT,),
        target=(NodeLabel.INCIDENT,),
        properties=("score", "method"),
    ),
    RelType.TARGETS: RelSpec(
        rel_type=RelType.TARGETS,
        source=(NodeLabel.REMEDIATION,),
        target=_SERVICE,
    ),
    RelType.VERIFIED_BY: RelSpec(
        rel_type=RelType.VERIFIED_BY,
        source=(NodeLabel.REMEDIATION,),
        target=(NodeLabel.VERIFICATION,),
    ),
    RelType.RELATES_TO: RelSpec(
        rel_type=RelType.RELATES_TO,
        source=(NodeLabel.INCIDENT,),
        target=(NodeLabel.ALERT,),
    ),
    RelType.PASSES_THROUGH: RelSpec(
        rel_type=RelType.PASSES_THROUGH,
        source=(NodeLabel.INCIDENT,),
        target=_SERVICE,
        properties=("position",),
    ),
    RelType.EXPOSES: RelSpec(
        rel_type=RelType.EXPOSES,
        source=_SERVICE,
        target=(NodeLabel.ENDPOINT,),
    ),
    RelType.RUNS_ON: RelSpec(
        rel_type=RelType.RUNS_ON,
        source=_SERVICE,
        target=(NodeLabel.INSTANCE,),
    ),
}


# --------------------------------------------------------------------------- #
# validation - the only doorway from a string to a Cypher token                #
# --------------------------------------------------------------------------- #


def validate_label(value: str | NodeLabel) -> NodeLabel:
    """Return the enum member, or raise. Never returns the input unchanged."""
    try:
        return NodeLabel(value)
    except ValueError as exc:
        raise ValidationError(
            "unknown graph node label",
            context={"label": str(value), "allowed": sorted(x.value for x in NodeLabel)},
        ) from exc


def validate_rel_type(value: str | RelType) -> RelType:
    """Return the enum member, or raise. Never returns the input unchanged."""
    try:
        return RelType(value)
    except ValueError as exc:
        raise ValidationError(
            "unknown graph relationship type",
            context={"rel_type": str(value), "allowed": sorted(x.value for x in RelType)},
        ) from exc


def label_token(value: str | NodeLabel) -> str:
    """The literal that is safe to interpolate after ``:`` in a node pattern."""
    return validate_label(value).value


def rel_token(value: str | RelType) -> str:
    """The literal that is safe to interpolate after ``:`` in an edge pattern."""
    return validate_rel_type(value).value


def rel_union_token(values: Iterable[str | RelType]) -> str:
    """``CALLS|DEPENDS_ON`` for multi-type traversal, every member validated.

    Sorted so the rendered Cypher is byte-identical across calls - a provenance
    hash an operator re-runs must not depend on set iteration order.
    """
    tokens = sorted({rel_token(v) for v in values})
    if not tokens:
        raise ValidationError("relationship union must name at least one type")
    return "|".join(tokens)


def node_spec(label: str | NodeLabel) -> NodeSpec:
    return NODES[validate_label(label)]


def rel_spec(rel_type: str | RelType) -> RelSpec:
    return RELATIONSHIPS[validate_rel_type(rel_type)]


def natural_key(label: str | NodeLabel) -> str:
    """The property name used in every MERGE for this label."""
    return node_spec(label).key


def validate_node_properties(label: str | NodeLabel, props: Mapping[str, object]) -> NodeSpec:
    """Reject a write that would create a node missing its identity.

    A half-populated node is worse than no node: it still matches traversals,
    and then answers them with nulls.
    """
    spec = node_spec(label)
    missing = [p for p in spec.required if props.get(p) in (None, "")]
    if missing:
        raise ValidationError(
            "graph node is missing required properties",
            context={"label": spec.label.value, "missing": missing},
        )
    return spec


def validate_edge(
    rel_type: str | RelType, source: str | NodeLabel, target: str | NodeLabel
) -> RelSpec:
    """Reject an edge the ontology does not define, e.g. a Service OWNED_BY a
    Commit. Catching it here stops a malformed subgraph being written and then
    traversed later as though it were real topology.
    """
    spec = rel_spec(rel_type)
    src, dst = validate_label(source), validate_label(target)
    if src not in spec.source or dst not in spec.target:
        raise ValidationError(
            "relationship is not defined between these labels",
            context={
                "rel_type": spec.rel_type.value,
                "source": src.value,
                "target": dst.value,
                "allowed_source": [s.value for s in spec.source],
                "allowed_target": [t.value for t in spec.target],
            },
        )
    return spec


# --------------------------------------------------------------------------- #
# schema statements                                                            #
# --------------------------------------------------------------------------- #


def constraints_cypher() -> list[str]:
    """Uniqueness constraints and range indexes for the whole ontology.

    Every statement is ``IF NOT EXISTS`` so ``ensure_schema`` is safe on every
    boot, not only the first. The uniqueness constraint - not the MERGE pattern -
    is what actually guarantees identity under concurrent ingestion: two workers
    merging the same service in the same instant would otherwise race into two
    nodes carrying the same id.
    """
    statements: list[str] = []
    for spec in NODES.values():
        label, name = spec.label.value, spec.label.name.lower()
        statements.append(
            f"CREATE CONSTRAINT {name}_{spec.key}_unique IF NOT EXISTS "
            f"FOR (n:{label}) REQUIRE n.{spec.key} IS UNIQUE"
        )
        statements.extend(
            f"CREATE INDEX {name}_{prop}_idx IF NOT EXISTS FOR (n:{label}) ON (n.{prop})"
            for prop in spec.indexed
        )
    return statements


__all__ = [
    "CAUSE_LABELS",
    "INFRASTRUCTURE_LABELS",
    "NODES",
    "RELATIONSHIPS",
    "TEMPORAL_PROPERTIES",
    "NodeLabel",
    "NodeSpec",
    "RelSpec",
    "RelType",
    "constraints_cypher",
    "label_token",
    "natural_key",
    "node_spec",
    "rel_spec",
    "rel_token",
    "rel_union_token",
    "validate_edge",
    "validate_label",
    "validate_node_properties",
    "validate_rel_type",
]
