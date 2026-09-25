"""Corpus ingestion for hybrid retrieval.

Two tables are written here: ``retrieval_documents`` (runbooks, postmortems,
design docs) and ``code_documents`` (repository chunks). Both are read by
``retrieval.hybrid``; neither is ever read directly by an agent.

The invariants that matter operationally:

* Re-ingesting unchanged content is a no-op. Ingestion runs on a schedule, so
  without content-hash dedup the corpus doubles every sync and lexical ranking
  degrades as duplicates split the score.
* Code chunks carry real line numbers. A citation that resolves to the wrong
  lines is worse than no citation, because an operator will act on it.
* Embedding failures never fail ingestion. The row lands with a NULL embedding
  and is still lexically searchable - degraded, not lost.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any, Final

from aegis.core.errors import SourceUnavailable, ValidationError
from aegis.core.ids import new_id
from aegis.core.logging import get_logger
from aegis.persistence.db import Database
from aegis.retrieval.embeddings import MAX_INPUT_CHARS, EmbeddingClient

log = get_logger(__name__)

# Chunk budget in characters. Roughly 500 tokens at ~4 chars/token, which keeps
# a retrieved chunk small enough that several fit in an agent's context window
# alongside metrics and traces.
DEFAULT_CHUNK_CHARS: Final = 2_000
# Overlap keeps a symbol that straddles a boundary retrievable from either
# side. Too much overlap makes the same text win several ranking slots.
DEFAULT_OVERLAP_CHARS: Final = 200
# A single document can only ever become this many chunks. A generated file or
# a vendored blob must not be able to fill the corpus on its own.
MAX_CHUNKS_PER_DOCUMENT: Final = 200
# Result caps for the list/admin surfaces.
MAX_LIST_LIMIT: Final = 200

# Paths that make a chunk a test. Used to classify, never to exclude - the test
# stage of code localisation needs these rows.
_TEST_MARKERS: Final = ("test_", "_test.", "/tests/", "/test/", ".spec.", ".test.")


@dataclass(frozen=True, slots=True)
class Chunk:
    """One retrievable slice of a document, with the lines it came from."""

    index: int
    text: str
    start_line: int
    end_line: int

    @property
    def line_span(self) -> str:
        return f"L{self.start_line}-L{self.end_line}"


def content_digest(*parts: str) -> str:
    """Stable digest used for dedup. Order-sensitive, newline-delimited."""
    joined = "\n\x00".join(parts)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def classify_path(path: str) -> str:
    """Bucket a repository path into a ``code_documents.kind``."""
    lowered = path.lower()
    if any(marker in lowered for marker in _TEST_MARKERS):
        return "test"
    if lowered.endswith((".yaml", ".yml", ".toml", ".ini", ".env", ".conf", ".json")):
        return "config"
    return "file"


def chunk_text(
    text: str,
    *,
    max_chars: int = DEFAULT_CHUNK_CHARS,
    overlap_chars: int = DEFAULT_OVERLAP_CHARS,
    first_line: int = 1,
    max_chunks: int = MAX_CHUNKS_PER_DOCUMENT,
) -> list[Chunk]:
    """Split ``text`` into overlapping chunks that preserve line numbers.

    Splitting happens on line boundaries only. A chunk that begins mid-line
    would still be retrievable, but its reported ``start_line`` would be a lie,
    and every citation built from it would point an operator at the wrong code.

    A single line longer than ``max_chars`` is emitted whole rather than cut:
    minified bundles and embedded blobs exist, and truncating one silently loses
    the very text that matched.
    """
    if max_chars < 1:
        raise ValidationError("max_chars must be >= 1", context={"max_chars": max_chars})
    if not 0 <= overlap_chars < max_chars:
        raise ValidationError(
            "overlap_chars must be >= 0 and < max_chars",
            context={"overlap_chars": overlap_chars, "max_chars": max_chars},
        )
    if first_line < 1:
        raise ValidationError("first_line must be >= 1", context={"first_line": first_line})
    if not text.strip():
        return []

    lines = text.splitlines()
    if not lines:
        return []

    chunks: list[Chunk] = []
    cursor = 0  # index into `lines`
    while cursor < len(lines) and len(chunks) < max_chunks:
        size = 0
        end = cursor
        while end < len(lines):
            # +1 for the newline that splitlines() removed, so the budget
            # matches what the embedder and the model actually see.
            cost = len(lines[end]) + 1
            if size and size + cost > max_chars:
                break
            size += cost
            end += 1

        body = "\n".join(lines[cursor:end])
        if body.strip():
            chunks.append(
                Chunk(
                    index=len(chunks),
                    text=body,
                    start_line=first_line + cursor,
                    end_line=first_line + end - 1,
                )
            )

        if end >= len(lines):
            break

        # Step back far enough to cover `overlap_chars`, but always forward by
        # at least one line: a zero-length step is an infinite ingestion loop.
        back = 0
        budget = overlap_chars
        while back < end - cursor - 1 and budget > 0:
            budget -= len(lines[end - 1 - back]) + 1
            back += 1
        cursor = max(cursor + 1, end - back)

    if len(chunks) >= max_chunks and cursor < len(lines):
        log.warning(
            "document truncated at chunk cap",
            max_chunks=max_chunks,
            lines_total=len(lines),
            lines_indexed=cursor,
        )
    return chunks


def vector_literal(vector: list[float] | None) -> str | None:
    """Render a vector for pgvector.

    asyncpg has no codec for ``vector``, so the value crosses the wire as text
    and is cast in SQL. Formatting it here keeps every call site identical.
    """
    if vector is None:
        return None
    return "[" + ",".join(repr(float(x)) for x in vector) + "]"


class DocumentStore:
    """Writes and maintains the retrieval corpus."""

    __slots__ = ("_db", "_embeddings")

    def __init__(self, db: Database, embeddings: EmbeddingClient | None = None) -> None:
        self._db = db
        self._embeddings = embeddings

    # ------------------------------------------------------------------ #
    # embedding helper                                                    #
    # ------------------------------------------------------------------ #

    async def _embed(self, texts: list[str]) -> list[list[float] | None]:
        """Embed or return NULLs, never zero vectors.

        A NULL embedding is a row that lexical search still finds and vector
        search correctly skips. A zero vector would be equidistant from every
        query and would quietly pollute the top-k of every semantic search.
        """
        if self._embeddings is None or not self._embeddings.configured or not texts:
            return [None] * len(texts)
        try:
            return list(await self._embeddings.embed(texts))
        except SourceUnavailable as exc:
            log.warning(
                "ingesting without embeddings", count=len(texts), reason=exc.message
            )
            return [None] * len(texts)

    # ------------------------------------------------------------------ #
    # retrieval_documents                                                 #
    # ------------------------------------------------------------------ #

    async def upsert_document(
        self,
        *,
        doc_type: str,
        title: str,
        body: str,
        ref_id: str | None = None,
        services: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        provenance_uri: str = "",
        chunked: bool = True,
    ) -> list[str]:
        """Ingest one document, returning the ids of every row it occupies.

        Returns the existing ids when nothing changed, so a caller can tell an
        unchanged sync from a failed one by the absence of an exception rather
        than by counting rows.
        """
        if not title.strip():
            raise ValidationError("document title must not be empty")
        pieces = (
            chunk_text(body)
            if chunked
            else [Chunk(index=0, text=body, start_line=1, end_line=max(1, body.count("\n") + 1))]
        )
        if not pieces:
            return []

        vectors = await self._embed([f"{title}\n{c.text}"[:MAX_INPUT_CHARS] for c in pieces])
        ids: list[str] = []
        for chunk, vector in zip(pieces, vectors, strict=True):
            digest = content_digest(doc_type, ref_id or "", title, str(chunk.index), chunk.text)
            row = await self._db.fetchrow(
                """
                INSERT INTO retrieval_documents
                    (id, doc_type, ref_id, title, body, services, metadata,
                     embedding, content_hash, provenance_uri, chunk_index, updated_at)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8::vector,$9,$10,$11, now())
                ON CONFLICT (content_hash) WHERE content_hash <> ''
                DO UPDATE SET
                    services       = EXCLUDED.services,
                    metadata       = EXCLUDED.metadata,
                    provenance_uri = EXCLUDED.provenance_uri,
                    -- Never overwrite a good embedding with NULL: a degraded
                    -- sync would otherwise erase semantic search for this row.
                    embedding      = COALESCE(EXCLUDED.embedding, retrieval_documents.embedding),
                    updated_at     = now()
                RETURNING id
                """,
                new_id("mem"),
                doc_type,
                ref_id,
                title,
                chunk.text,
                services or [],
                dict(metadata or {}, chunk_lines=chunk.line_span),
                vector_literal(vector),
                digest,
                provenance_uri,
                chunk.index,
            )
            assert row is not None
            ids.append(row["id"])
        return ids

    async def delete_document(self, document_id: str) -> bool:
        status = await self._db.execute(
            "DELETE FROM retrieval_documents WHERE id = $1", document_id
        )
        return status.endswith(" 1")

    async def delete_by_ref(self, doc_type: str, ref_id: str) -> int:
        """Drop every chunk of a source document, e.g. when a runbook is deleted."""
        status = await self._db.execute(
            "DELETE FROM retrieval_documents WHERE doc_type = $1 AND ref_id = $2",
            doc_type,
            ref_id,
        )
        return int(status.rsplit(" ", 1)[-1] or 0)

    async def list_documents(
        self, *, doc_type: str | None = None, limit: int = 50
    ) -> list[dict[str, Any]]:
        rows = await self._db.fetch(
            """
            SELECT id, doc_type, ref_id, title, services, provenance_uri,
                   chunk_index, created_at, (embedding IS NOT NULL) AS has_embedding
            FROM retrieval_documents
            WHERE ($1::text IS NULL OR doc_type = $1)
            ORDER BY created_at DESC
            LIMIT $2
            """,
            doc_type,
            min(max(limit, 1), MAX_LIST_LIMIT),
        )
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------ #
    # code_documents                                                      #
    # ------------------------------------------------------------------ #

    async def upsert_code_file(
        self,
        *,
        repo: str,
        ref: str,
        path: str,
        content: str,
        language: str = "",
        service_id: str | None = None,
        symbols: dict[int, str] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> list[str]:
        """Chunk and store one source file.

        ``symbols`` maps a 1-based line number to the symbol that starts there;
        a chunk is labelled with the last symbol that opened at or before its
        first line, which is what makes ``symbol`` usable as a search key.
        """
        if not repo or not ref or not path:
            raise ValidationError(
                "repo, ref and path are required to store code",
                context={"repo": repo, "ref": ref, "path": path},
            )
        pieces = chunk_text(content)
        if not pieces:
            return []

        kind = classify_path(path)
        vectors = await self._embed(
            [f"{path}\n{c.text}"[:MAX_INPUT_CHARS] for c in pieces]
        )
        ids: list[str] = []
        for chunk, vector in zip(pieces, vectors, strict=True):
            symbol = _symbol_for_line(symbols, chunk.start_line)
            digest = content_digest(repo, path, str(chunk.start_line), chunk.text)
            row = await self._db.fetchrow(
                """
                INSERT INTO code_documents
                    (id, repo, ref, path, symbol, kind, language, start_line, end_line,
                     content, content_hash, service_id, metadata, embedding, updated_at)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14::vector, now())
                ON CONFLICT (repo, path, start_line, content_hash)
                DO UPDATE SET
                    -- Identical content at a newer sha: move the citation
                    -- pointer forward instead of storing the same bytes twice.
                    ref        = EXCLUDED.ref,
                    service_id = COALESCE(EXCLUDED.service_id, code_documents.service_id),
                    symbol     = COALESCE(EXCLUDED.symbol, code_documents.symbol),
                    metadata   = EXCLUDED.metadata,
                    embedding  = COALESCE(EXCLUDED.embedding, code_documents.embedding),
                    updated_at = now()
                RETURNING id
                """,
                new_id("mem"),
                repo,
                ref,
                path,
                symbol,
                "symbol" if symbol and kind == "file" else kind,
                language,
                chunk.start_line,
                chunk.end_line,
                chunk.text,
                digest,
                service_id,
                dict(metadata or {}, chunk_index=chunk.index),
                vector_literal(vector),
            )
            assert row is not None
            ids.append(row["id"])

        log.info(
            "code file indexed", repo=repo, path=path, ref=ref[:12], chunks=len(ids), kind=kind
        )
        return ids

    async def is_indexed(self, *, repo: str, path: str, content: str) -> bool:
        """True when this exact file body is already stored.

        Lets an ingester skip the read, the chunking and the embedding spend for
        a file that has not changed, which is the common case on every sync.
        """
        pieces = chunk_text(content)
        if not pieces:
            return False
        digests = [content_digest(repo, path, str(c.start_line), c.text) for c in pieces]
        found = await self._db.fetchval(
            """
            SELECT count(*) FROM code_documents
            WHERE repo = $1 AND path = $2 AND content_hash = ANY($3)
            """,
            repo,
            path,
            digests,
        )
        return int(found or 0) == len(digests)

    async def delete_code_path(self, *, repo: str, path: str) -> int:
        status = await self._db.execute(
            "DELETE FROM code_documents WHERE repo = $1 AND path = $2", repo, path
        )
        return int(status.rsplit(" ", 1)[-1] or 0)

    async def list_code_paths(self, *, repo: str, limit: int = 100) -> list[dict[str, Any]]:
        rows = await self._db.fetch(
            """
            SELECT path, max(ref) AS ref, count(*) AS chunks,
                   bool_or(embedding IS NOT NULL) AS has_embedding
            FROM code_documents
            WHERE repo = $1
            GROUP BY path
            ORDER BY path
            LIMIT $2
            """,
            repo,
            min(max(limit, 1), MAX_LIST_LIMIT),
        )
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------ #
    # service -> repo mapping                                             #
    # ------------------------------------------------------------------ #

    async def map_service_to_repo(
        self,
        *,
        service_id: str,
        repo: str,
        default_ref: str = "main",
        path_prefix: str = "",
        language: str = "",
        rank: int = 100,
    ) -> None:
        await self._db.execute(
            """
            INSERT INTO service_repositories
                (service_id, repo, default_ref, path_prefix, language, rank)
            VALUES ($1,$2,$3,$4,$5,$6)
            ON CONFLICT (service_id, repo, path_prefix) DO UPDATE SET
                default_ref = EXCLUDED.default_ref,
                language    = EXCLUDED.language,
                rank        = EXCLUDED.rank
            """,
            service_id,
            repo,
            default_ref,
            path_prefix,
            language,
            rank,
        )


def _symbol_for_line(symbols: dict[int, str] | None, line: int) -> str | None:
    if not symbols:
        return None
    candidates = [start for start in symbols if start <= line]
    if not candidates:
        return None
    return symbols[max(candidates)]


__all__ = [
    "DEFAULT_CHUNK_CHARS",
    "DEFAULT_OVERLAP_CHARS",
    "MAX_CHUNKS_PER_DOCUMENT",
    "Chunk",
    "DocumentStore",
    "chunk_text",
    "classify_path",
    "content_digest",
    "vector_literal",
]
