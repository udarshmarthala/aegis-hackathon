"""Rank fusion and degraded-mode reporting.

Ranking is where retrieval either helps or quietly misleads, so these tests
assert the ordering arithmetic directly rather than through a search. The
degraded-mode cases exist because a lexical-only search presented as a complete
one is an operational bug, not a cosmetic one.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from aegis.core.errors import SourceUnavailable, ValidationError
from aegis.core.resilience import reset_breakers
from aegis.retrieval.hybrid import (
    RRF_K,
    SIGNAL_GRAPH,
    SIGNAL_LEXICAL,
    SIGNAL_RECENCY,
    SIGNAL_VECTOR,
    WEIGHT_LEXICAL,
    WEIGHT_VECTOR,
    HybridRetriever,
    RetrievalScope,
    code_provenance_uri,
    recency_rank,
    reciprocal_rank_fusion,
)

NOW = datetime(2026, 9, 18, 12, 0, tzinfo=UTC)

_DOC_LEXICAL = "FROM retrieval_documents, plainto_tsquery"
_DOC_VECTOR = "FROM retrieval_documents\n    WHERE embedding IS NOT NULL"
_DOC_GRAPH = "services && $2::text[]"


# --------------------------------------------------------------------------- #
# fakes                                                                        #
# --------------------------------------------------------------------------- #


def doc_row(row_id: str, *, title: str = "", services: tuple[str, ...] = (), age_days: int = 0):
    return {
        "id": row_id,
        "kind": "document",
        "title": title or row_id,
        "content": f"body of {row_id}",
        "source": "runbook",
        "provenance_uri": f"aegis://doc/{row_id}",
        "services": list(services),
        "metadata": {},
        "created_at": NOW - timedelta(days=age_days),
        "repo": "",
        "ref": "",
        "path": "",
        "symbol": "",
        "start_line": 0,
        "end_line": 0,
    }


class FakeDatabase:
    """Routes queries to canned rows by a distinctive SQL fragment."""

    def __init__(self, rules: list[tuple[str, list[dict[str, Any]]]] | None = None) -> None:
        self.rules = rules or []
        self.queries: list[str] = []

    async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
        self.queries.append(query)
        for needle, rows in self.rules:
            if needle in query:
                return rows
        return []

    async def fetchrow(self, query: str, *args: Any) -> dict[str, Any] | None:
        rows = await self.fetch(query, *args)
        return rows[0] if rows else None

    async def fetchval(self, query: str, *args: Any) -> Any:
        self.queries.append(query)
        return None

    async def execute(self, query: str, *args: Any) -> str:
        self.queries.append(query)
        return "OK 1"


class FakeEmbeddings:
    def __init__(self, *, configured: bool = True, fail: bool = False) -> None:
        self.configured = configured
        self._fail = fail
        self.calls = 0

    async def embed_one(self, text: str) -> list[float]:
        self.calls += 1
        if self._fail:
            raise SourceUnavailable("provider timed out", context={"provider": "openai"})
        return [0.1, 0.2, 0.3]


@pytest.fixture(autouse=True)
def _clean_breakers() -> None:
    reset_breakers()


# --------------------------------------------------------------------------- #
# pure fusion                                                                  #
# --------------------------------------------------------------------------- #


def test_rrf_scores_match_the_formula() -> None:
    fused = reciprocal_rank_fusion({SIGNAL_LEXICAL: ["a", "b"]})
    assert fused[0].id == "a"
    assert fused[0].score == pytest.approx(WEIGHT_LEXICAL / (RRF_K + 1))
    assert fused[1].score == pytest.approx(WEIGHT_LEXICAL / (RRF_K + 2))


def test_agreement_between_signals_beats_a_single_first_place() -> None:
    """A document both rankers like outranks one only lexical search liked.

    This is the whole reason for fusing: a single ranker's top hit is a guess,
    two rankers agreeing is corroboration.
    """
    fused = reciprocal_rank_fusion(
        {
            SIGNAL_LEXICAL: ["only-lexical", "agreed"],
            SIGNAL_VECTOR: ["agreed", "only-vector"],
        }
    )
    assert fused[0].id == "agreed"
    assert fused[0].score == pytest.approx(
        WEIGHT_LEXICAL / (RRF_K + 2) + WEIGHT_VECTOR / (RRF_K + 1)
    )
    assert set(fused[0].contributions) == {SIGNAL_LEXICAL, SIGNAL_VECTOR}


def test_absence_from_a_signal_is_neutral_not_a_penalty() -> None:
    fused = reciprocal_rank_fusion(
        {SIGNAL_LEXICAL: ["a"], SIGNAL_VECTOR: ["b"]}
    )
    by_id = {f.id: f for f in fused}
    assert SIGNAL_VECTOR not in by_id["a"].contributions
    assert by_id["a"].score > 0
    # Lexical outranks vector purely on weight, never because "a" was missing
    # from the vector list.
    assert by_id["a"].score > by_id["b"].score


def test_lexical_outranks_vector_at_the_same_rank() -> None:
    fused = reciprocal_rank_fusion({SIGNAL_LEXICAL: ["lex"], SIGNAL_VECTOR: ["vec"]})
    assert [f.id for f in fused] == ["lex", "vec"]


def test_duplicate_ids_inside_one_signal_are_counted_once() -> None:
    once = reciprocal_rank_fusion({SIGNAL_LEXICAL: ["a", "b"]})
    twice = reciprocal_rank_fusion({SIGNAL_LEXICAL: ["a", "a", "b"]})
    assert [f.id for f in once] == [f.id for f in twice]
    assert once[0].score == pytest.approx(twice[0].score)
    assert once[1].score == pytest.approx(twice[1].score)


def test_ties_break_deterministically_on_id() -> None:
    first = reciprocal_rank_fusion({SIGNAL_LEXICAL: ["zeta"], SIGNAL_VECTOR: ["alpha"]})
    second = reciprocal_rank_fusion({SIGNAL_VECTOR: ["alpha"], SIGNAL_LEXICAL: ["zeta"]})
    assert [f.id for f in first] == [f.id for f in second]

    tied = reciprocal_rank_fusion(
        {SIGNAL_LEXICAL: ["zeta", "alpha"]}, weights={SIGNAL_LEXICAL: 0.0}
    )
    assert tied == []


def test_custom_weights_override_defaults() -> None:
    fused = reciprocal_rank_fusion(
        {SIGNAL_LEXICAL: ["lex"], SIGNAL_VECTOR: ["vec"]},
        weights={SIGNAL_VECTOR: 10.0},
    )
    assert fused[0].id == "vec"


def test_graph_signal_lifts_an_in_scope_document() -> None:
    """Topology scope reorders, it does not filter.

    "out-of-scope" still appears in the result; it just loses to the document
    the graph implicated.
    """
    fused = reciprocal_rank_fusion(
        {
            SIGNAL_LEXICAL: ["out-of-scope", "in-scope"],
            SIGNAL_GRAPH: ["in-scope"],
        }
    )
    assert [f.id for f in fused] == ["in-scope", "out-of-scope"]
    assert "out-of-scope" in {f.id for f in fused}


def test_rrf_rejects_a_nonsensical_k() -> None:
    with pytest.raises(ValidationError):
        reciprocal_rank_fusion({SIGNAL_LEXICAL: ["a"]}, k=0)


def test_recency_orders_newest_first_and_clamps_the_future() -> None:
    ranked = recency_rank({"old": 30.0, "fresh": 1.0, "future": -5.0})
    assert ranked[0] in {"fresh", "future"}
    assert ranked[-1] == "old"
    # A document created after the incident started must not outrank one
    # created just before it - it cannot be evidence about the cause.
    assert recency_rank({"future": -50.0, "fresh": 0.0}) == ["fresh", "future"]


def test_recency_rejects_a_zero_half_life() -> None:
    with pytest.raises(ValidationError):
        recency_rank({"a": 1.0}, half_life_days=0.0)


def test_code_provenance_pins_a_sha_and_line_range() -> None:
    uri = code_provenance_uri(
        repo="acme/payments", ref="deadbeef", path="src/pool.py", start_line=10, end_line=42
    )
    assert uri == "github://acme/payments/blob/deadbeef/src/pool.py#L10-L42"
    assert code_provenance_uri(
        repo="acme/payments", ref="deadbeef", path="src/pool.py", start_line=0, end_line=0
    ).endswith("src/pool.py")


# --------------------------------------------------------------------------- #
# degraded mode                                                                #
# --------------------------------------------------------------------------- #


async def test_search_without_embeddings_is_flagged_degraded() -> None:
    db = FakeDatabase([(_DOC_LEXICAL, [doc_row("d1"), doc_row("d2")])])
    result = await HybridRetriever(db, None).search(  # type: ignore[arg-type]
        "connection pool exhausted",
        scope=RetrievalScope.DOCUMENTS,
        limit=5,
        reference_time=NOW,
    )
    assert len(result) == 2
    assert result.degraded is True
    assert result.degraded_reason == "embeddings_not_configured"
    assert SIGNAL_VECTOR not in result.signals_used
    # Results were still returned: degraded is about completeness, not failure.
    assert result.is_empty is False


async def test_unreachable_embedding_provider_degrades_with_its_own_reason() -> None:
    db = FakeDatabase([(_DOC_LEXICAL, [doc_row("d1")])])
    embeddings = FakeEmbeddings(fail=True)
    result = await HybridRetriever(db, embeddings).search(  # type: ignore[arg-type]
        "latency spike", scope=RetrievalScope.DOCUMENTS, limit=5, reference_time=NOW
    )
    assert result.degraded is True
    assert result.degraded_reason.startswith("embedding_source_unavailable")
    assert "provider timed out" in result.degraded_reason
    # Distinguishable from the not-configured case, which is a different fix.
    assert result.degraded_reason != "embeddings_not_configured"


async def test_full_search_is_not_flagged_degraded() -> None:
    db = FakeDatabase(
        [
            (_DOC_VECTOR, [doc_row("d2"), doc_row("d3")]),
            (_DOC_LEXICAL, [doc_row("d1"), doc_row("d2")]),
        ]
    )
    embeddings = FakeEmbeddings()
    result = await HybridRetriever(db, embeddings).search(  # type: ignore[arg-type]
        "pool exhausted", scope=RetrievalScope.DOCUMENTS, limit=5, reference_time=NOW
    )
    assert result.degraded is False
    assert result.degraded_reason == ""
    assert SIGNAL_VECTOR in result.signals_used
    assert embeddings.calls == 1
    # d2 was found by both signals, so it must lead.
    assert result[0].id == "d2"
    assert set(result[0].sub_scores) >= {SIGNAL_LEXICAL, SIGNAL_VECTOR}


async def test_empty_and_not_degraded_is_a_finding_not_a_gap() -> None:
    db = FakeDatabase([])
    embeddings = FakeEmbeddings()
    result = await HybridRetriever(db, embeddings).search(  # type: ignore[arg-type]
        "nothing matches this", scope=RetrievalScope.DOCUMENTS, limit=5, reference_time=NOW
    )
    assert result.is_empty is True
    assert result.degraded is False


async def test_graph_scope_adds_a_signal_and_boosts_scoped_rows() -> None:
    scoped = doc_row("scoped", services=("local:demo:payment",))
    db = FakeDatabase(
        [
            (_DOC_GRAPH, [scoped]),
            (_DOC_LEXICAL, [doc_row("unscoped"), scoped]),
        ]
    )
    result = await HybridRetriever(db, None).search(  # type: ignore[arg-type]
        "timeouts",
        scope=RetrievalScope.DOCUMENTS,
        limit=5,
        service_ids=["local:demo:payment"],
        reference_time=NOW,
    )
    assert SIGNAL_GRAPH in result.signals_used
    assert result[0].id == "scoped"
    assert {c.id for c in result} == {"scoped", "unscoped"}


async def test_recency_signal_participates_when_timestamps_exist() -> None:
    db = FakeDatabase(
        [(_DOC_LEXICAL, [doc_row("stale", age_days=180), doc_row("recent", age_days=1)])]
    )
    result = await HybridRetriever(db, None).search(  # type: ignore[arg-type]
        "disk pressure", scope=RetrievalScope.DOCUMENTS, limit=5, reference_time=NOW
    )
    assert SIGNAL_RECENCY in result.signals_used
    # "stale" wins lexical rank 1; recency is a tiebreaker and must not flip it.
    assert result[0].id == "stale"
    assert SIGNAL_RECENCY in result[1].sub_scores


async def test_result_limit_is_capped_and_query_length_is_bounded() -> None:
    rows = [doc_row(f"d{i}") for i in range(120)]
    db = FakeDatabase([(_DOC_LEXICAL, rows)])
    retriever = HybridRetriever(db, None)  # type: ignore[arg-type]

    result = await retriever.search(
        "anything", scope=RetrievalScope.DOCUMENTS, limit=10_000, reference_time=NOW
    )
    assert len(result) == 50

    with pytest.raises(ValidationError):
        await retriever.search("x" * 1_001, scope=RetrievalScope.DOCUMENTS, limit=5)
    with pytest.raises(ValidationError):
        await retriever.search("   ", scope=RetrievalScope.DOCUMENTS, limit=5)


async def test_code_scope_builds_a_citable_provenance_uri() -> None:
    row = doc_row("c1")
    row.update(
        kind="code",
        repo="acme/payments",
        ref="abc123",
        path="src/pool.py",
        start_line=10,
        end_line=42,
        provenance_uri="",
        source="acme/payments",
    )
    db = FakeDatabase([("FROM code_documents, plainto_tsquery", [row])])
    result = await HybridRetriever(db, None).search(  # type: ignore[arg-type]
        "pool", scope=RetrievalScope.CODE, limit=5, reference_time=NOW
    )
    assert result[0].provenance_uri == (
        "github://acme/payments/blob/abc123/src/pool.py#L10-L42"
    )
    assert result[0].citation == "acme/payments/src/pool.py:L10-L42"
