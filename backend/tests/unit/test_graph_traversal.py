"""Read-side graph behaviour, with a fake Neo4j client.

What matters here is not that the queries run - it is that depth is clamped
before it reaches Cypher, that an empty result and an unreachable database stay
distinguishable, and that GraphRAG ranks and truncates deterministically.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest

from aegis.core.clock import FrozenClock
from aegis.core.errors import SourceUnavailable
from aegis.domain.models import BlastRadius
from aegis.graph.graphrag import (
    MAX_SEEDS,
    TEMPORAL_WINDOW_S,
    W_CAUSAL,
    GraphRAG,
    ScoredNode,
    apply_budget,
    score_nodes,
    temporal_boost,
)
from aegis.graph.traversal import (
    CYPHER,
    MAX_DEPTH,
    DeploymentSummary,
    GraphTraversal,
    QueryRecorder,
    provenance_uri,
    query_hash,
    rendered_cypher,
)

NOW = datetime(2026, 9, 18, 12, 0, tzinfo=UTC)

GATEWAY = "local:demo:gateway"
CHECKOUT = "local:demo:checkout"
PAYMENT = "local:demo:payment"
LEDGER = "local:demo:ledger"


class FakeNeo4jClient:
    """Routes a query to a canned response by hashing the rendered Cypher.

    Routing on the real templates means a test cannot pass against a query the
    traversal no longer sends.
    """

    def __init__(
        self,
        responses: dict[str, list[dict[str, Any]]] | None = None,
        *,
        fail: bool = False,
    ) -> None:
        self.responses = responses or {}
        self.fail = fail
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._routes: dict[str, str] = {}
        for name in CYPHER:
            for depth in (None, *range(1, MAX_DEPTH + 1)):
                try:
                    text = rendered_cypher(name, depth=depth)
                except (KeyError, IndexError, ValueError):
                    continue
                self._routes[query_hash(text)] = name

    async def run(self, cypher: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        self.calls.append((cypher, params))
        if self.fail:
            raise SourceUnavailable(
                "neo4j query failed: FakeDriverError", context={"dependency": "neo4j"}
            )
        return [dict(row) for row in self.responses.get(self.operation_of(cypher), [])]

    async def write(self, cypher: str, params: dict[str, Any]) -> None:
        self.calls.append((cypher, params))

    def operation_of(self, cypher: str) -> str:
        return self._routes.get(query_hash(cypher), "unknown")

    @property
    def operations(self) -> list[str]:
        return [self.operation_of(c) for c, _ in self.calls]

    def params_for(self, operation: str) -> dict[str, Any]:
        for cypher, params in self.calls:
            if self.operation_of(cypher) == operation:
                return params
        raise AssertionError(f"{operation} was never queried")

    def cypher_for(self, operation: str) -> str:
        for cypher, _ in self.calls:
            if self.operation_of(cypher) == operation:
                return cypher
        raise AssertionError(f"{operation} was never queried")


def traversal(**responses: list[dict[str, Any]]) -> tuple[GraphTraversal, FakeNeo4jClient]:
    client = FakeNeo4jClient(responses)
    return GraphTraversal(client), client  # type: ignore[arg-type]


def service_node(service_id: str, hops: int, **props: Any) -> dict[str, Any]:
    return {
        "labels": ["Service"],
        "props": {
            "service_id": service_id,
            "name": service_id.rsplit(":", 1)[-1],
            "environment": "local",
            **props,
        },
        "hops": hops,
    }


# --------------------------------------------------------------------------- #
# depth clamping                                                               #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("requested", [7, 50, 10_000])
async def test_depth_is_clamped_to_the_ceiling_before_reaching_cypher(requested: int) -> None:
    trav, client = traversal(blast_radius=[], request_share=[])
    await trav.blast_radius(CHECKOUT, max_depth=requested)
    assert f"*1..{MAX_DEPTH}]" in client.cypher_for("blast_radius")
    assert f"*1..{requested}]" not in client.cypher_for("blast_radius")


@pytest.mark.parametrize("requested", [0, -1])
async def test_depth_below_one_is_raised_to_one(requested: int) -> None:
    trav, client = traversal(blast_radius=[], request_share=[])
    await trav.blast_radius(CHECKOUT, max_depth=requested)
    assert "*1..1]" in client.cypher_for("blast_radius")


async def test_causal_path_depth_is_clamped_too() -> None:
    trav, client = traversal(causal_paths=[])
    await trav.causal_paths(GATEWAY, PAYMENT, max_depth=99)
    assert f"*1..{MAX_DEPTH}]" in client.cypher_for("causal_paths")


async def test_neighbourhood_depth_is_clamped_on_both_queries() -> None:
    trav, client = traversal(neighbourhood_nodes=[], neighbourhood_edges=[])
    await trav.neighbourhood(CHECKOUT, depth=42)
    assert f"*0..{MAX_DEPTH}]" in client.cypher_for("neighbourhood_nodes")
    assert f"*1..{MAX_DEPTH}]" in client.cypher_for("neighbourhood_edges")


async def test_result_limits_are_clamped_to_their_ceiling() -> None:
    trav, client = traversal(blast_radius=[], request_share=[])
    await trav.blast_radius(CHECKOUT, 2, limit=10_000)
    assert client.params_for("blast_radius")["limit"] == 100


# --------------------------------------------------------------------------- #
# blast radius                                                                 #
# --------------------------------------------------------------------------- #


async def test_blast_radius_splits_direct_callers_from_deeper_ones() -> None:
    trav, _ = traversal(
        blast_radius=[
            {"service_id": CHECKOUT, "name": "checkout", "customer_facing": False, "hops": 1},
            {"service_id": GATEWAY, "name": "gateway", "customer_facing": True, "hops": 2},
        ],
        request_share=[{"total": 1000, "impacted": 250}],
    )
    radius = await trav.blast_radius(PAYMENT, max_depth=3)
    assert radius.directly_affected == [CHECKOUT]
    assert radius.downstream == [GATEWAY]
    assert radius.customer_facing is True
    assert radius.estimated_request_share == pytest.approx(0.25)
    assert radius.size == 2


async def test_blast_radius_with_no_dependents_is_an_empty_answer_not_an_error() -> None:
    # "Nothing depends on this service" is a finding. It must not look like a
    # failed query, and it must not trigger a request-share round trip.
    trav, client = traversal(blast_radius=[], request_share=[{"total": 5, "impacted": 5}])
    radius = await trav.blast_radius(LEDGER)
    assert radius == BlastRadius()
    assert "request_share" not in client.operations


async def test_request_share_is_zero_when_no_call_counts_were_ingested() -> None:
    trav, _ = traversal(
        blast_radius=[
            {"service_id": CHECKOUT, "name": "checkout", "customer_facing": False, "hops": 1}
        ],
        request_share=[{"total": 0, "impacted": 0}],
    )
    radius = await trav.blast_radius(PAYMENT)
    assert radius.estimated_request_share == 0.0


async def test_request_share_cannot_exceed_one() -> None:
    trav, _ = traversal(
        blast_radius=[
            {"service_id": CHECKOUT, "name": "checkout", "customer_facing": False, "hops": 1}
        ],
        request_share=[{"total": 10, "impacted": 40}],
    )
    radius = await trav.blast_radius(PAYMENT)
    assert radius.estimated_request_share == 1.0


async def test_a_driver_failure_propagates_and_is_never_an_empty_result() -> None:
    trav = GraphTraversal(FakeNeo4jClient(fail=True))  # type: ignore[arg-type]
    with pytest.raises(SourceUnavailable):
        await trav.blast_radius(PAYMENT)
    with pytest.raises(SourceUnavailable):
        await trav.owning_team(PAYMENT)
    with pytest.raises(SourceUnavailable):
        await trav.neighbourhood(PAYMENT)
    with pytest.raises(SourceUnavailable):
        await trav.similar_incidents("inc_1")


# --------------------------------------------------------------------------- #
# other reads                                                                  #
# --------------------------------------------------------------------------- #


async def test_causal_paths_return_ordered_node_lists() -> None:
    trav, _ = traversal(
        causal_paths=[
            {"path": [GATEWAY, CHECKOUT, PAYMENT], "hops": 2},
            {"path": [GATEWAY, LEDGER, CHECKOUT, PAYMENT], "hops": 3},
        ]
    )
    paths = await trav.causal_paths(GATEWAY, PAYMENT)
    assert paths[0].nodes == (GATEWAY, CHECKOUT, PAYMENT)
    assert paths[0].hops == 2
    assert paths[1].hops == 3


async def test_neighbourhood_drops_edges_whose_endpoints_were_truncated() -> None:
    # A dangling edge renders as a line to nowhere in the service-graph view.
    trav, _ = traversal(
        neighbourhood_nodes=[service_node(CHECKOUT, 0), service_node(PAYMENT, 1)],
        neighbourhood_edges=[
            {
                "rel_type": "CALLS",
                "props": {"call_count": 12, "error_count": 1, "latency_p99_ms": 90.0},
                "source_labels": ["Service"],
                "source_props": {"service_id": CHECKOUT},
                "target_labels": ["Service"],
                "target_props": {"service_id": PAYMENT},
            },
            {
                "rel_type": "CALLS",
                "props": {},
                "source_labels": ["Service"],
                "source_props": {"service_id": CHECKOUT},
                "target_labels": ["Service"],
                "target_props": {"service_id": "local:demo:unknown"},
            },
        ],
    )
    view = await trav.neighbourhood(CHECKOUT, depth=2)
    assert [n["id"] for n in view["nodes"]] == [CHECKOUT, PAYMENT]
    assert len(view["edges"]) == 1
    assert view["edges"][0] == {
        "source": CHECKOUT,
        "target": PAYMENT,
        "type": "CALLS",
        "call_count": 12,
        "error_count": 1,
        "latency_p99_ms": 90.0,
    }


async def test_nodes_with_labels_outside_the_ontology_are_dropped() -> None:
    trav, _ = traversal(
        neighbourhood_nodes=[
            service_node(CHECKOUT, 0),
            {"labels": ["Wormhole"], "props": {"id": "x"}, "hops": 1},
        ],
        neighbourhood_edges=[],
    )
    view = await trav.neighbourhood(CHECKOUT)
    assert [n["id"] for n in view["nodes"]] == [CHECKOUT]


async def test_owning_team_returns_none_when_the_graph_holds_no_owner() -> None:
    trav, _ = traversal(owning_team=[])
    assert await trav.owning_team(CHECKOUT) is None


async def test_owning_team_returns_the_team() -> None:
    trav, _ = traversal(
        owning_team=[{"team_id": "commerce", "name": "Commerce", "contact": "#commerce"}]
    )
    team = await trav.owning_team(CHECKOUT)
    assert team is not None
    assert (team.team_id, team.name, team.contact) == ("commerce", "Commerce", "#commerce")


async def test_recent_deployments_parse_commit_lineage_and_timestamps() -> None:
    trav, _ = traversal(
        recent_deployments=[
            {
                "deployment_id": "dep-2",
                "version": "1.4.1",
                "status": "succeeded",
                "deployed_at": "2026-09-18T11:55:00+00:00",
                "commit_sha": "abc123",
                "commit_repo": "demo/shop",
                "commit_author": "ada",
                "commit_authored_at": None,
            }
        ]
    )
    deployments = await trav.recent_deployments_for(CHECKOUT, limit=5)
    assert deployments[0].deployment_id == "dep-2"
    assert deployments[0].deployed_at == datetime(2026, 9, 18, 11, 55, tzinfo=UTC)
    assert deployments[0].commit_sha == "abc123"


async def test_services_sharing_dependency_resolves_the_resource_label() -> None:
    trav, _ = traversal(
        services_sharing_dependency=[
            {
                "resource_id": "local:demo:orders-db",
                "name": "orders-db",
                "labels": ["Database"],
                "service_ids": [PAYMENT, CHECKOUT],
            }
        ]
    )
    shared = await trav.services_sharing_dependency(CHECKOUT)
    assert shared[0].label.value == "Database"
    # Sorted so a correlated-failure summary reads the same on every run.
    assert shared[0].service_ids == (CHECKOUT, PAYMENT)


async def test_similar_incidents_are_ordered_and_typed() -> None:
    trav, _ = traversal(
        similar_incidents=[
            {
                "incident_id": "inc_a",
                "score": 0.91,
                "method": "structural",
                "severity": "P2",
                "title": "checkout latency",
                "started_at": None,
            }
        ]
    )
    similar = await trav.similar_incidents("inc_b", limit=3)
    assert similar[0].incident_id == "inc_a"
    assert similar[0].score == pytest.approx(0.91)


async def test_no_similar_incidents_is_an_empty_list() -> None:
    trav, _ = traversal(similar_incidents=[])
    assert await trav.similar_incidents("inc_b") == []


# --------------------------------------------------------------------------- #
# provenance                                                                   #
# --------------------------------------------------------------------------- #


def test_provenance_uri_carries_a_query_hash_and_replayable_params() -> None:
    cypher = rendered_cypher("blast_radius", depth=3)
    uri = provenance_uri(cypher, {"service_id": PAYMENT, "limit": 100})
    parsed = urlparse(uri)
    assert parsed.scheme == "neo4j"
    assert parsed.netloc == query_hash(cypher)
    assert '"service_id":"local:demo:payment"' in parse_qs(parsed.query)["params"][0]


def test_provenance_uri_is_stable_for_equivalent_params() -> None:
    cypher = rendered_cypher("blast_radius", depth=3)
    assert provenance_uri(cypher, {"a": 1, "b": 2}) == provenance_uri(cypher, {"b": 2, "a": 1})


def test_query_recorder_is_bounded() -> None:
    recorder = QueryRecorder(limit=2)
    for i in range(5):
        recorder.add("blast_radius", f"MATCH {i}", {"i": i})
    assert len(recorder.records) == 2


async def test_recorder_captures_the_query_a_traversal_actually_sent() -> None:
    trav, _ = traversal(blast_radius=[], request_share=[])
    recorder = QueryRecorder()
    await trav.blast_radius(PAYMENT, 2, recorder=recorder)
    record = recorder.first("blast_radius")
    assert record is not None
    assert record.cypher_sha256 == query_hash(rendered_cypher("blast_radius", depth=2))
    assert record.params["service_id"] == PAYMENT


# --------------------------------------------------------------------------- #
# graphrag ranking - pure, deterministic, no model                             #
# --------------------------------------------------------------------------- #


def deployment(deployment_id: str, minutes_before: float) -> DeploymentSummary:
    return DeploymentSummary(
        deployment_id=deployment_id,
        version="1.0.0",
        status="succeeded",
        deployed_at=NOW - timedelta(minutes=minutes_before),
    )


def test_closer_nodes_outrank_distant_ones() -> None:
    scored = score_nodes(
        hops={GATEWAY: 0, CHECKOUT: 1, PAYMENT: 3},
        meta={},
        on_causal_path=set(),
        deployments={},
        incident_started_at=NOW,
    )
    assert [n.service_id for n in scored] == [GATEWAY, CHECKOUT, PAYMENT]
    assert scored[0].score > scored[1].score > scored[2].score


def test_being_on_a_causal_path_outranks_an_equally_distant_node() -> None:
    scored = score_nodes(
        hops={CHECKOUT: 2, LEDGER: 2},
        meta={},
        on_causal_path={CHECKOUT},
        deployments={},
        incident_started_at=NOW,
    )
    ranked = {n.service_id: n for n in scored}
    assert scored[0].service_id == CHECKOUT
    assert ranked[CHECKOUT].score - ranked[LEDGER].score == pytest.approx(W_CAUSAL)
    assert "on causal path between seeds" in ranked[CHECKOUT].reasons


def test_a_recent_deployment_boosts_a_node_and_is_explained() -> None:
    scored = score_nodes(
        hops={CHECKOUT: 2, LEDGER: 2},
        meta={},
        on_causal_path=set(),
        deployments={CHECKOUT: [deployment("dep-1", minutes_before=5)]},
        incident_started_at=NOW,
    )
    ranked = {n.service_id: n for n in scored}
    assert scored[0].service_id == CHECKOUT
    assert ranked[CHECKOUT].score > ranked[LEDGER].score
    assert any("dep-1" in reason for reason in ranked[CHECKOUT].reasons)


def test_an_old_deployment_does_not_boost_anything() -> None:
    boost, reason = temporal_boost([deployment("dep-old", minutes_before=600)], NOW)
    assert boost == 0.0
    assert reason is None


def test_temporal_boost_decays_with_distance_from_incident_start() -> None:
    near, _ = temporal_boost([deployment("near", minutes_before=1)], NOW)
    far, _ = temporal_boost([deployment("far", minutes_before=50)], NOW)
    assert near > far > 0.0
    edge, _ = temporal_boost(
        [deployment("edge", minutes_before=TEMPORAL_WINDOW_S / 60)], NOW
    )
    assert edge == 0.0


def test_a_deployment_after_the_incident_start_still_counts() -> None:
    # Deciding which of the two caused the other is not this layer's job.
    boost, _ = temporal_boost([deployment("after", minutes_before=-5)], NOW)
    assert boost > 0.0


def test_deployments_without_a_timestamp_are_ignored() -> None:
    undated = DeploymentSummary(
        deployment_id="dep-x", version="1", status="unknown", deployed_at=None
    )
    assert temporal_boost([undated], NOW) == (0.0, None)


def test_ranking_is_stable_for_tied_scores() -> None:
    hops = {LEDGER: 2, CHECKOUT: 2, PAYMENT: 2}
    first = score_nodes(
        hops=hops, meta={}, on_causal_path=set(), deployments={}, incident_started_at=NOW
    )
    second = score_nodes(
        hops=dict(reversed(list(hops.items()))),
        meta={},
        on_causal_path=set(),
        deployments={},
        incident_started_at=NOW,
    )
    assert [n.service_id for n in first] == [n.service_id for n in second]
    assert [n.service_id for n in first] == sorted(hops)


def test_budget_truncates_by_score_and_records_what_was_dropped() -> None:
    scored = score_nodes(
        hops={f"local:demo:svc-{i:02d}": i % 4 for i in range(20)},
        meta={},
        on_causal_path=set(),
        deployments={},
        incident_started_at=NOW,
    )
    kept, truncation = apply_budget(scored, 5)
    assert len(kept) == 5
    assert truncation is not None
    assert truncation["dropped"] == 15
    assert truncation["candidates"] == 20
    assert truncation["cutoff_score"] >= truncation["highest_dropped_score"]
    assert kept == scored[:5]


def test_budget_that_fits_records_no_truncation() -> None:
    scored = (ScoredNode(CHECKOUT, "checkout", "Service", 0, 1.0, ()),)
    kept, truncation = apply_budget(scored, 40)
    assert kept == scored
    assert truncation is None


# --------------------------------------------------------------------------- #
# graphrag expansion                                                           #
# --------------------------------------------------------------------------- #


def graphrag_fixture() -> tuple[GraphRAG, FakeNeo4jClient]:
    client = FakeNeo4jClient(
        {
            "neighbourhood_nodes": [
                service_node(GATEWAY, 0),
                service_node(CHECKOUT, 1),
                service_node(PAYMENT, 2),
                service_node(LEDGER, 2),
            ],
            "neighbourhood_edges": [
                {
                    "rel_type": "CALLS",
                    "props": {"call_count": 40},
                    "source_labels": ["Service"],
                    "source_props": {"service_id": GATEWAY},
                    "target_labels": ["Service"],
                    "target_props": {"service_id": CHECKOUT},
                }
            ],
            "blast_radius": [
                {"service_id": CHECKOUT, "name": "checkout", "customer_facing": True, "hops": 1},
                {"service_id": LEDGER, "name": "ledger", "customer_facing": False, "hops": 2},
            ],
            "request_share": [{"total": 100, "impacted": 30}],
            "causal_paths": [{"path": [GATEWAY, CHECKOUT, PAYMENT], "hops": 2}],
            "recent_deployments": [
                {
                    "deployment_id": "dep-9",
                    "version": "2.0.0",
                    "status": "succeeded",
                    "deployed_at": NOW - timedelta(minutes=3),
                    "commit_sha": "deadbeef",
                    "commit_repo": "demo/shop",
                    "commit_author": "ada",
                    "commit_authored_at": None,
                }
            ],
            "owning_team": [{"team_id": "commerce", "name": "Commerce", "contact": None}],
        }
    )
    trav = GraphTraversal(client)  # type: ignore[arg-type]
    return GraphRAG(trav, clock=FrozenClock(NOW)), client


async def test_expand_ranks_seeds_first_and_records_provenance() -> None:
    rag, client = graphrag_fixture()
    context = await rag.expand(
        [GATEWAY, PAYMENT], incident_id="inc_1", incident_started_at=NOW, depth=2
    )
    assert context.seeds == (GATEWAY, PAYMENT)
    ids = context.service_ids()
    assert ids[0] in (GATEWAY, PAYMENT)
    assert set(ids) == {GATEWAY, CHECKOUT, PAYMENT, LEDGER}

    # Every query that ran is citable and replayable.
    queries = context.provenance["queries"]
    assert {q["operation"] for q in queries} >= {
        "neighbourhood_nodes",
        "blast_radius",
        "causal_paths",
        "recent_deployments",
        "owning_team",
    }
    assert all(q["uri"].startswith("neo4j://") for q in queries)
    assert context.provenance["depth"] == 2
    assert context.provenance["truncated"] is None


async def test_expand_drops_seeds_from_their_own_blast_radius() -> None:
    rag, _ = graphrag_fixture()
    context = await rag.expand(
        [GATEWAY, CHECKOUT], incident_id="inc_1", incident_started_at=NOW
    )
    assert CHECKOUT not in context.blast_radius.directly_affected
    assert context.blast_radius.downstream == [LEDGER]
    assert context.blast_radius.customer_facing is True


async def test_expand_never_exceeds_its_budget_and_says_so() -> None:
    rag, _ = graphrag_fixture()
    context = await rag.expand(
        [GATEWAY], incident_id="inc_1", incident_started_at=NOW, budget=2
    )
    assert len(context.services) == 2
    assert context.truncated
    assert context.provenance["truncated"]["dropped"] == 2
    assert context.provenance["returned"] == 2


async def test_expand_keeps_only_edges_between_surviving_nodes() -> None:
    rag, _ = graphrag_fixture()
    context = await rag.expand(
        [GATEWAY], incident_id="inc_1", incident_started_at=NOW, budget=1
    )
    assert context.service_ids() == (GATEWAY,)
    assert context.edges == ()


async def test_expand_caps_the_number_of_seeds_it_explores() -> None:
    rag, _ = graphrag_fixture()
    seeds = [f"local:demo:svc-{i}" for i in range(MAX_SEEDS + 3)]
    context = await rag.expand(seeds, incident_id="inc_1", incident_started_at=NOW)
    assert len(context.seeds) == MAX_SEEDS


async def test_expand_with_no_seeds_returns_an_empty_context_not_an_error() -> None:
    rag, client = graphrag_fixture()
    context = await rag.expand([], incident_id="inc_1", incident_started_at=NOW)
    assert context.services == ()
    assert context.blast_radius == BlastRadius()
    assert client.calls == []


async def test_expand_propagates_source_unavailable() -> None:
    trav = GraphTraversal(FakeNeo4jClient(fail=True))  # type: ignore[arg-type]
    rag = GraphRAG(trav, clock=FrozenClock(NOW))
    # The caller records one evidence gap for the graph; this layer must not
    # invent a partial context that reads as complete.
    with pytest.raises(SourceUnavailable):
        await rag.expand([GATEWAY], incident_id="inc_1", incident_started_at=NOW)


async def test_expand_is_deterministic() -> None:
    rag_a, _ = graphrag_fixture()
    rag_b, _ = graphrag_fixture()
    kwargs = {"incident_id": "inc_1", "incident_started_at": NOW, "depth": 2}
    first = await rag_a.expand([GATEWAY, PAYMENT], **kwargs)  # type: ignore[arg-type]
    second = await rag_b.expand([GATEWAY, PAYMENT], **kwargs)  # type: ignore[arg-type]
    assert [s.as_dict() for s in first.services] == [s.as_dict() for s in second.services]
    assert first.causal_paths == second.causal_paths


# --------------------------------------------------------------------------- #
# evidence emission                                                            #
# --------------------------------------------------------------------------- #


class FakeEvidenceStore:
    def __init__(self) -> None:
        self.recorded: list[dict[str, Any]] = []

    async def record(self, **kwargs: Any) -> dict[str, Any]:
        self.recorded.append(kwargs)
        return kwargs


async def test_to_evidence_writes_graph_sourced_topology_and_blast_radius_items() -> None:
    rag, _ = graphrag_fixture()
    context = await rag.expand(
        [GATEWAY, PAYMENT], incident_id="inc_1", incident_started_at=NOW
    )
    store = FakeEvidenceStore()
    items = await rag.to_evidence(store, "inc_1", context)  # type: ignore[arg-type]

    assert len(items) == 2
    kinds = [item["evidence_type"].value for item in store.recorded]
    assert kinds == ["topology_path", "blast_radius"]
    for item in store.recorded:
        assert item["source_type"].value == "graph"
        assert item["provenance_uri"].startswith("neo4j://")
    topology = store.recorded[0]
    assert topology["structured_value"]["paths"] == [[GATEWAY, CHECKOUT, PAYMENT]]


async def test_to_evidence_still_records_when_the_graph_found_no_path() -> None:
    # An absent item reads as "never looked"; an empty one is a real finding.
    client = FakeNeo4jClient(
        {
            "neighbourhood_nodes": [service_node(GATEWAY, 0)],
            "neighbourhood_edges": [],
            "blast_radius": [],
            "causal_paths": [],
            "recent_deployments": [],
            "owning_team": [],
        }
    )
    rag = GraphRAG(GraphTraversal(client), clock=FrozenClock(NOW))  # type: ignore[arg-type]
    context = await rag.expand(
        [GATEWAY, PAYMENT], incident_id="inc_1", incident_started_at=NOW
    )
    store = FakeEvidenceStore()
    await rag.to_evidence(store, "inc_1", context)  # type: ignore[arg-type]
    assert "no structural path found" in store.recorded[0]["summary"]
    assert store.recorded[0]["structured_value"]["paths"] == []
    assert store.recorded[1]["structured_value"]["downstream"] == []
