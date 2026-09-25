"""Ontology invariants.

The ontology is the only thing standing between caller input and an interpolated
Cypher clause, so these tests assert the rejection behaviour rule by rule rather
than checking that the enums merely exist.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

import pytest

from aegis.core.errors import SourceUnavailable, ValidationError
from aegis.domain.models import ServiceRef
from aegis.graph import ingest
from aegis.graph.ontology import (
    NODES,
    RELATIONSHIPS,
    NodeLabel,
    RelType,
    constraints_cypher,
    label_token,
    natural_key,
    rel_token,
    rel_union_token,
    validate_edge,
    validate_label,
    validate_node_properties,
    validate_rel_type,
)


class _NullWriter:
    """Accepts writes so validation can be asserted without a database."""

    def __init__(self) -> None:
        self.writes: list[tuple[str, dict[str, Any]]] = []

    async def write(self, cypher: str, params: dict[str, Any]) -> None:
        self.writes.append((cypher, params))


INJECTION_ATTEMPTS = [
    "Service) DETACH DELETE (n",
    "Service`) RETURN 1 //",
    "Service:Admin",
    "Service ",
    "service",
    "",
    "CALLS|SECRET",
]


def test_every_label_has_a_spec_keyed_on_a_required_property() -> None:
    assert set(NODES) == set(NodeLabel)
    for label, spec in NODES.items():
        assert spec.label is label
        # MERGE keys on this property, so a node that may omit it could be
        # created twice with the same identity.
        assert spec.key in spec.required


def test_every_relationship_has_a_spec_between_known_labels() -> None:
    assert set(RELATIONSHIPS) == set(RelType)
    for rel_type, spec in RELATIONSHIPS.items():
        assert spec.rel_type is rel_type
        assert spec.source and spec.target
        for label in (*spec.source, *spec.target):
            assert label in NODES


def test_constraints_are_idempotent_and_cover_every_label() -> None:
    statements = constraints_cypher()
    assert statements
    # Re-running ensure_schema on every boot must be a no-op, not an error.
    assert all("IF NOT EXISTS" in s for s in statements)
    for spec in NODES.values():
        expected = (
            f"FOR (n:{spec.label.value}) REQUIRE n.{spec.key} IS UNIQUE"
        )
        assert any(expected in s for s in statements), spec.label


def test_constraint_names_are_unique() -> None:
    # Two statements sharing a name means one schema object silently wins.
    names = [s.split()[2] for s in constraints_cypher()]
    assert len(names) == len(set(names))


def test_constraints_are_stable_across_calls() -> None:
    assert constraints_cypher() == constraints_cypher()


@pytest.mark.parametrize("attempt", INJECTION_ATTEMPTS)
def test_validate_label_rejects_anything_not_in_the_enum(attempt: str) -> None:
    with pytest.raises(ValidationError):
        validate_label(attempt)


@pytest.mark.parametrize("attempt", INJECTION_ATTEMPTS)
def test_label_token_never_returns_caller_text(attempt: str) -> None:
    with pytest.raises(ValidationError):
        label_token(attempt)


def test_label_token_returns_the_canonical_spelling() -> None:
    assert label_token("CodeSymbol") == "CodeSymbol"
    assert label_token(NodeLabel.SERVICE) == "Service"


def test_validate_rel_type_rejects_unknown_and_lowercase() -> None:
    for bad in ("calls", "CALLS|DEPENDS_ON", "DROP", "CALLS*0..99"):
        with pytest.raises(ValidationError):
            validate_rel_type(bad)
    assert rel_token("CALLS") == "CALLS"


def test_rel_union_is_sorted_and_validated() -> None:
    # Sorted so a provenance hash does not depend on set iteration order.
    assert rel_union_token([RelType.DEPENDS_ON, RelType.CALLS]) == "CALLS|DEPENDS_ON"
    assert rel_union_token([RelType.CALLS, RelType.CALLS]) == "CALLS"
    with pytest.raises(ValidationError):
        rel_union_token(["CALLS", "DELETE"])
    with pytest.raises(ValidationError):
        rel_union_token([])


def test_error_context_names_the_offending_value_and_the_allowed_set() -> None:
    with pytest.raises(ValidationError) as exc:
        validate_label("Wormhole")
    assert exc.value.context["label"] == "Wormhole"
    assert "Service" in exc.value.context["allowed"]


def test_natural_keys_are_single_properties() -> None:
    # A composite key would need a constraint form not available on every
    # supported Neo4j edition, leaving the MERGE unguarded.
    for label in NodeLabel:
        key = natural_key(label)
        assert isinstance(key, str)
        assert "," not in key


def test_validate_node_properties_rejects_missing_identity() -> None:
    with pytest.raises(ValidationError) as exc:
        validate_node_properties(NodeLabel.SERVICE, {"name": "checkout"})
    assert "service_id" in exc.value.context["missing"]


def test_validate_node_properties_rejects_empty_identity() -> None:
    with pytest.raises(ValidationError):
        validate_node_properties(
            NodeLabel.SERVICE,
            {"service_id": "", "name": "c", "environment": "local", "workload": "demo"},
        )


def test_validate_node_properties_accepts_a_complete_node() -> None:
    spec = validate_node_properties(
        NodeLabel.SERVICE,
        {
            "service_id": "local:demo:checkout",
            "name": "checkout",
            "environment": "local",
            "workload": "demo",
            "extra": "ignored",
        },
    )
    assert spec.label is NodeLabel.SERVICE


def test_validate_edge_accepts_defined_shapes() -> None:
    assert validate_edge(RelType.CALLS, NodeLabel.SERVICE, NodeLabel.SERVICE)
    assert validate_edge(RelType.DEPENDS_ON, NodeLabel.SERVICE, NodeLabel.CACHE)
    assert validate_edge(RelType.DEFINES, NodeLabel.FILE, NodeLabel.CODE_SYMBOL)


def test_validate_edge_rejects_undefined_shapes() -> None:
    # A Service cannot be owned by a Commit; writing it would create a subgraph
    # that later traversals would read as real topology.
    with pytest.raises(ValidationError) as exc:
        validate_edge(RelType.OWNED_BY, NodeLabel.SERVICE, NodeLabel.COMMIT)
    assert exc.value.context["rel_type"] == "OWNED_BY"
    assert exc.value.context["target"] == "Commit"

    with pytest.raises(ValidationError):
        validate_edge(RelType.DEPENDS_ON, NodeLabel.SERVICE, NodeLabel.INCIDENT)
    with pytest.raises(ValidationError):
        validate_edge(RelType.AFFECTS, NodeLabel.SERVICE, NodeLabel.INCIDENT)


def test_caused_by_carries_citation_properties() -> None:
    # CAUSED_BY is the only edge encoding a claim, so it must be able to cite.
    spec = RELATIONSHIPS[RelType.CAUSED_BY]
    assert "evidence_ids" in spec.properties
    assert "confidence" in spec.properties


# --------------------------------------------------------------------------- #
# the ingestor's generated Cypher must honour the same ontology                 #
# --------------------------------------------------------------------------- #


def ingest_statements() -> list[str]:
    """Every Cypher statement the ingestor can send."""
    statements: list[str] = list(constraints_cypher())
    for value in vars(ingest).values():
        if isinstance(value, str) and "MERGE" in value:
            statements.append(value)
        elif isinstance(value, Mapping):
            statements.extend(v for v in value.values() if isinstance(v, str) and "MERGE" in v)
    return statements


@pytest.mark.parametrize("statement", ingest_statements())
def test_ingest_statements_only_name_labels_from_the_ontology(statement: str) -> None:
    labels = set(re.findall(r"\(\s*\w*\s*:([A-Za-z]+)", statement))
    assert labels <= {node.value for node in NodeLabel}, labels


@pytest.mark.parametrize("statement", ingest_statements())
def test_ingest_statements_only_name_relationships_from_the_ontology(statement: str) -> None:
    found: set[str] = set()
    for group in re.findall(r"\[\s*\w*\s*:([A-Z_|]+)", statement):
        found.update(group.split("|"))
    assert found <= {rel.value for rel in RelType}, found


@pytest.mark.parametrize("statement", ingest_statements())
def test_ingestion_is_never_destructive(statement: str) -> None:
    # A pod churning must not be able to remove topology, and nothing in the
    # ingest path has any reason to delete.
    assert not re.search(r"\b(DELETE|DETACH|REMOVE|DROP)\b", statement), statement


def test_every_write_stamps_last_seen_so_staleness_stays_visible() -> None:
    for statement in ingest_statements():
        if statement.startswith("CREATE "):
            continue
        assert "last_seen" in statement, statement


async def test_ingest_rejects_a_dependency_on_a_non_infrastructure_label() -> None:
    ingestor = ingest.TopologyIngestor(_NullWriter())  # type: ignore[arg-type]
    edge = ingest.DependencyEdge(
        service=ServiceRef.build("local", "demo", "checkout"),
        resource_id="local:demo:inc",
        name="inc",
        kind=NodeLabel.INCIDENT,
    )
    with pytest.raises(ValidationError):
        await ingestor.ingest_dependencies([edge])


async def test_caused_by_refuses_to_be_written_without_citations() -> None:
    # An uncited cause edge reads exactly like observed topology to every later
    # traversal, so the ontology refuses to let one exist (CLAUDE.md 3.2).
    ingestor = ingest.TopologyIngestor(_NullWriter())  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        await ingestor.link_probable_cause(
            incident_id="inc_1",
            target_label=NodeLabel.DEPLOYMENT,
            target_key="dep-1",
            confidence=0.9,
            evidence_ids=[],
        )


async def test_caused_by_rejects_an_out_of_range_confidence() -> None:
    ingestor = ingest.TopologyIngestor(_NullWriter())  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        await ingestor.link_probable_cause(
            incident_id="inc_1",
            target_label=NodeLabel.COMMIT,
            target_key="abc123",
            confidence=1.5,
            evidence_ids=["ev_1"],
        )


async def test_self_calls_and_self_similarity_are_dropped() -> None:
    writer = _NullWriter()
    ingestor = ingest.TopologyIngestor(writer)  # type: ignore[arg-type]
    ref = ServiceRef.build("local", "demo", "checkout")
    assert (await ingestor.ingest_call_edges([ingest.CallEdge(ref, ref)])).edges == 0
    assert (await ingestor.link_similar_incidents("inc_1", [("inc_1", 1.0)])).edges == 0
    assert writer.writes == []


async def test_an_unconfigured_trace_source_is_unavailable_not_empty() -> None:
    # Returning zero edges would let an investigation conclude "nothing calls
    # this service" from the absence of a backend (PRD 13).
    ingestor = ingest.TopologyIngestor(_NullWriter())  # type: ignore[arg-type]
    with pytest.raises(SourceUnavailable):
        await ingestor.ingest_from_traces(None, environment="local", workload="demo")
