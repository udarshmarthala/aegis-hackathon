"""Chunking, line fidelity and content-hash dedup.

A chunk whose reported line numbers are wrong produces a citation that points an
on-call engineer at the wrong code, which is worse than producing no citation at
all. Every case here therefore reconstructs the chunk from its own line range
and asserts the text matches.
"""

from __future__ import annotations

from typing import Any

import pytest

from aegis.core.errors import ValidationError
from aegis.retrieval.code import symptom_terms
from aegis.retrieval.documents import (
    DEFAULT_CHUNK_CHARS,
    DocumentStore,
    chunk_text,
    classify_path,
    content_digest,
    vector_literal,
)

SOURCE = "\n".join(f"line {i:03d} of the file" for i in range(1, 61))
SOURCE_LINES = SOURCE.splitlines()


class RecordingDatabase:
    """Captures every statement and its bind parameters."""

    def __init__(self, *, fetchval: Any = 0) -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        self._fetchval = fetchval

    async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
        self.calls.append((query, args))
        return []

    async def fetchrow(self, query: str, *args: Any) -> dict[str, Any]:
        self.calls.append((query, args))
        return {"id": f"mem_{len(self.calls)}"}

    async def fetchval(self, query: str, *args: Any) -> Any:
        self.calls.append((query, args))
        return self._fetchval

    async def execute(self, query: str, *args: Any) -> str:
        self.calls.append((query, args))
        return "DELETE 3"


def reconstruct(chunk: Any, lines: list[str] = SOURCE_LINES) -> str:
    return "\n".join(lines[chunk.start_line - 1 : chunk.end_line])


# --------------------------------------------------------------------------- #
# chunk boundaries and line numbers                                            #
# --------------------------------------------------------------------------- #


def test_short_text_is_one_chunk_spanning_every_line() -> None:
    chunks = chunk_text(SOURCE, max_chars=DEFAULT_CHUNK_CHARS)
    assert len(chunks) == 1
    assert chunks[0].start_line == 1
    assert chunks[0].end_line == len(SOURCE_LINES)
    assert chunks[0].text == SOURCE


def test_every_chunk_reports_the_lines_it_actually_contains() -> None:
    chunks = chunk_text(SOURCE, max_chars=120, overlap_chars=0)
    assert len(chunks) > 1
    for chunk in chunks:
        assert chunk.text == reconstruct(chunk)
        assert chunk.start_line >= 1
        assert chunk.end_line >= chunk.start_line


def test_chunks_without_overlap_tile_the_file_exactly_once() -> None:
    chunks = chunk_text(SOURCE, max_chars=120, overlap_chars=0)
    covered: list[int] = []
    for chunk in chunks:
        covered.extend(range(chunk.start_line, chunk.end_line + 1))
    assert covered == list(range(1, len(SOURCE_LINES) + 1))


def test_overlap_repeats_lines_across_the_boundary() -> None:
    """A symbol straddling a boundary must be retrievable from either side."""
    chunks = chunk_text(SOURCE, max_chars=200, overlap_chars=60)
    assert len(chunks) > 1
    for previous, following in zip(chunks, chunks[1:], strict=False):
        assert following.start_line <= previous.end_line
        assert following.start_line > previous.start_line  # always makes progress
        assert following.text == reconstruct(following)


def test_first_line_offset_shifts_every_reported_line() -> None:
    plain = chunk_text(SOURCE, max_chars=120, overlap_chars=0)
    offset = chunk_text(SOURCE, max_chars=120, overlap_chars=0, first_line=101)
    assert [c.start_line + 100 for c in plain] == [c.start_line for c in offset]
    assert [c.end_line + 100 for c in plain] == [c.end_line for c in offset]
    assert [c.text for c in plain] == [c.text for c in offset]


def test_a_line_longer_than_the_budget_is_emitted_whole() -> None:
    """Minified bundles exist. Truncating one loses the text that matched."""
    body = "short\n" + "x" * 5_000 + "\ntail"
    chunks = chunk_text(body, max_chars=100, overlap_chars=0)
    joined = "".join(c.text for c in chunks)
    assert "x" * 5_000 in joined
    assert chunks[-1].end_line == 3


def test_chunking_terminates_on_pathological_overlap() -> None:
    """Overlap must never rewind far enough to revisit the same start line."""
    chunks = chunk_text(SOURCE, max_chars=60, overlap_chars=59)
    starts = [c.start_line for c in chunks]
    assert starts == sorted(starts)
    assert len(set(starts)) == len(starts)
    assert chunks[-1].end_line == len(SOURCE_LINES)


def test_chunk_cap_bounds_a_hostile_document() -> None:
    body = "\n".join(f"l{i}" for i in range(5_000))
    chunks = chunk_text(body, max_chars=10, overlap_chars=0, max_chunks=25)
    assert len(chunks) == 25


def test_blank_and_whitespace_only_input_yields_nothing() -> None:
    assert chunk_text("") == []
    assert chunk_text("   \n\n  \n") == []


def test_invalid_chunk_parameters_are_rejected() -> None:
    with pytest.raises(ValidationError):
        chunk_text(SOURCE, max_chars=0)
    with pytest.raises(ValidationError):
        chunk_text(SOURCE, max_chars=100, overlap_chars=100)
    with pytest.raises(ValidationError):
        chunk_text(SOURCE, first_line=0)


def test_line_span_renders_a_citation_fragment() -> None:
    chunk = chunk_text(SOURCE)[0]
    assert chunk.line_span == f"L1-L{len(SOURCE_LINES)}"


# --------------------------------------------------------------------------- #
# dedup                                                                        #
# --------------------------------------------------------------------------- #


def test_digest_is_stable_and_field_order_sensitive() -> None:
    assert content_digest("a", "b") == content_digest("a", "b")
    assert content_digest("a", "b") != content_digest("b", "a")
    # A separator that cannot occur in the parts prevents ("ab","c") colliding
    # with ("a","bc").
    assert content_digest("ab", "c") != content_digest("a", "bc")


async def test_reingesting_identical_content_reuses_the_same_hash() -> None:
    db = RecordingDatabase()
    store = DocumentStore(db)  # type: ignore[arg-type]

    await store.upsert_code_file(
        repo="acme/payments", ref="sha-one", path="src/pool.py", content=SOURCE
    )
    first = [args for query, args in db.calls if "INSERT INTO code_documents" in query]
    db.calls.clear()

    await store.upsert_code_file(
        repo="acme/payments", ref="sha-two", path="src/pool.py", content=SOURCE
    )
    second = [args for query, args in db.calls if "INSERT INTO code_documents" in query]

    assert len(first) == len(second) > 0
    # content_hash is parameter 11; identical bodies must produce identical
    # keys so the unique index collapses the re-ingest into an update.
    assert [a[10] for a in first] == [a[10] for a in second]
    # The ref moved forward even though the content did not.
    assert first[0][2] == "sha-one"
    assert second[0][2] == "sha-two"


async def test_upsert_conflict_target_matches_the_dedup_index() -> None:
    db = RecordingDatabase()
    store = DocumentStore(db)  # type: ignore[arg-type]
    await store.upsert_code_file(
        repo="acme/payments", ref="sha", path="src/pool.py", content="def f():\n    pass\n"
    )
    query = next(q for q, _ in db.calls if "INSERT INTO code_documents" in q)
    assert "ON CONFLICT (repo, path, start_line, content_hash)" in query
    # A degraded sync must not wipe an embedding produced by a healthy one.
    assert "COALESCE(EXCLUDED.embedding, code_documents.embedding)" in query


async def test_is_indexed_is_true_only_when_every_chunk_is_present() -> None:
    expected = len(chunk_text(SOURCE))
    complete = DocumentStore(RecordingDatabase(fetchval=expected))  # type: ignore[arg-type]
    partial = DocumentStore(RecordingDatabase(fetchval=expected - 1))  # type: ignore[arg-type]

    assert await complete.is_indexed(repo="r", path="p", content=SOURCE) is True
    assert await partial.is_indexed(repo="r", path="p", content=SOURCE) is False


async def test_ingestion_without_an_embedding_client_stores_null_not_zeros() -> None:
    db = RecordingDatabase()
    store = DocumentStore(db, embeddings=None)  # type: ignore[arg-type]
    await store.upsert_code_file(
        repo="acme/payments", ref="sha", path="src/pool.py", content=SOURCE
    )
    inserts = [args for query, args in db.calls if "INSERT INTO code_documents" in query]
    # Parameter 14 is the embedding literal. NULL keeps the row lexically
    # searchable; a zero vector would be equidistant from every query.
    assert all(args[13] is None for args in inserts)


async def test_symbols_are_attached_to_the_chunk_that_declares_them() -> None:
    db = RecordingDatabase()
    store = DocumentStore(db)  # type: ignore[arg-type]
    await store.upsert_code_file(
        repo="acme/payments",
        ref="sha",
        path="src/pool.py",
        content=SOURCE,
        symbols={1: "acquire", 40: "release"},
        service_id="local:demo:payment",
    )
    inserts = [args for query, args in db.calls if "INSERT INTO code_documents" in query]
    # Parameter 5 is symbol, 8 is start_line.
    for args in inserts:
        expected = "release" if args[7] >= 40 else "acquire"
        assert args[4] == expected
        assert args[5] == "symbol"


async def test_document_delete_reports_what_it_removed() -> None:
    db = RecordingDatabase()
    store = DocumentStore(db)  # type: ignore[arg-type]
    assert await store.delete_by_ref("runbook", "rb-1") == 3
    assert await store.delete_code_path(repo="acme/payments", path="src/pool.py") == 3


async def test_empty_document_title_is_rejected() -> None:
    store = DocumentStore(RecordingDatabase())  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        await store.upsert_document(doc_type="runbook", title="  ", body="text")


async def test_code_upsert_requires_repo_ref_and_path() -> None:
    store = DocumentStore(RecordingDatabase())  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        await store.upsert_code_file(repo="", ref="sha", path="p", content="x")


# --------------------------------------------------------------------------- #
# classification and helpers                                                   #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("tests/test_pool.py", "test"),
        ("src/pool_test.go", "test"),
        ("web/app.spec.ts", "test"),
        ("infra/values.yaml", "config"),
        ("src/pool.py", "file"),
    ],
)
def test_path_classification(path: str, expected: str) -> None:
    assert classify_path(path) == expected


def test_vector_literal_round_trips_pgvector_syntax() -> None:
    assert vector_literal(None) is None
    assert vector_literal([1.0, -0.5]) == "[1.0,-0.5]"


def test_symptom_terms_keep_code_shaped_tokens_and_drop_filler() -> None:
    terms = symptom_terms(
        "The checkout service returned an error from payments.pool.acquire_connection"
    )
    assert "payments.pool.acquire_connection" in terms
    assert "checkout" in terms
    assert "error" not in terms
    assert "the" not in terms


def test_symptom_terms_are_deduplicated_ordered_and_capped() -> None:
    terms = symptom_terms("alpha beta alpha gamma", limit=2)
    assert terms == ("alpha", "beta")
