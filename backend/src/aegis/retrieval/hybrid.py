"""Hybrid retrieval: lexical + vector + graph scope + recency, fused by RRF.

Why fusion rather than one ranker: in SRE retrieval the two signals fail in
opposite directions. Lexical search nails an exact error string, a metric name
or a symbol, and misses every paraphrase. Vector search finds "connection pool
exhausted" from "too many clients already", and happily returns a plausible but
literally wrong passage. Reciprocal Rank Fusion combines the *ranks*, not the
raw scores, so neither ranker's score distribution has to be calibrated against
the other - a calibration that silently rots whenever the embedding model
changes.

The fusion step is a pure function (``reciprocal_rank_fusion``) with no I/O, so
ranking behaviour is unit-testable without a database or a provider.

Degradation is explicit. When embeddings are unavailable the search still runs
lexically, but the result carries ``degraded=True`` and a reason, because a
lexical-only answer presented as a full one is how "we could not look properly"
gets mistaken for "there is nothing there" (PRD 13).
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any, Final

from aegis.core.clock import SYSTEM_CLOCK, Clock
from aegis.core.errors import SourceUnavailable, ValidationError
from aegis.core.logging import get_logger
from aegis.persistence.db import Database
from aegis.retrieval.documents import vector_literal
from aegis.retrieval.embeddings import EmbeddingClient

log = get_logger(__name__)


# --------------------------------------------------------------------------- #
# limits                                                                       #
# --------------------------------------------------------------------------- #

# A query longer than this is not a query, it is a pasted log file. Embedding
# it costs real money and the tsquery it produces matches nothing useful.
MAX_QUERY_CHARS: Final = 1_000
# Hard ceiling on returned chunks regardless of what a caller asks for. An
# agent context window is the real constraint and it is far below this.
MAX_RESULT_LIMIT: Final = 50
# Each signal fetches a deeper pool than the final limit so fusion has
# something to reorder; without headroom RRF degenerates to "whatever lexical
# said". Bounded so a pathological query cannot pull the table into memory.
_POOL_MULTIPLIER: Final = 4
_MAX_POOL: Final = 200


# --------------------------------------------------------------------------- #
# fusion weights                                                               #
# --------------------------------------------------------------------------- #

# RRF constant from Cormack et al. A larger k flattens the curve so rank 1 and
# rank 5 differ less; 60 is the published default and is deliberately not tuned
# per-corpus, because a weight tuned on last quarter's incidents is a weight
# that silently mis-ranks this quarter's.
RRF_K: Final = 60

# Exact tokens - error strings, symbol names, metric names - are the highest
# precision signal available during an incident, so lexical anchors the scale.
WEIGHT_LEXICAL: Final = 1.0
# Semantic recall catches the paraphrase between how an alert is worded and how
# a runbook is written. Slightly below lexical because embedding relevance
# drifts with the model version while a literal match does not.
WEIGHT_VECTOR: Final = 0.9
# Topology scope is Tier-B metadata and reliably points at the right area, but
# a fault frequently manifests one hop from where it lives. It boosts in-scope
# material rather than filtering out-of-scope material.
WEIGHT_GRAPH: Final = 0.7
# Recency is a tiebreaker, not evidence. Capped at half of lexical so a fresh
# but irrelevant document can never outrank an older exact match.
WEIGHT_RECENCY: Final = 0.5

# Half-life for temporal decay, measured from the incident start. Two weeks is
# roughly a deployment cadence: material older than that is usually background
# knowledge rather than a description of what just changed.
RECENCY_HALF_LIFE_DAYS: Final = 14.0

SIGNAL_LEXICAL: Final = "lexical"
SIGNAL_VECTOR: Final = "vector"
SIGNAL_GRAPH: Final = "graph"
SIGNAL_RECENCY: Final = "recency"

DEFAULT_WEIGHTS: Final[Mapping[str, float]] = {
    SIGNAL_LEXICAL: WEIGHT_LEXICAL,
    SIGNAL_VECTOR: WEIGHT_VECTOR,
    SIGNAL_GRAPH: WEIGHT_GRAPH,
    SIGNAL_RECENCY: WEIGHT_RECENCY,
}


class RetrievalScope(StrEnum):
    """Which corpora a search reads."""

    DOCUMENTS = "documents"  # runbooks, postmortems, design docs
    CODE = "code"            # repository chunks
    ALL = "all"


# --------------------------------------------------------------------------- #
# pure fusion                                                                  #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class FusedItem:
    """One fused candidate: total score plus what each signal contributed."""

    id: str
    score: float
    contributions: Mapping[str, float]


def reciprocal_rank_fusion(
    ranked_lists: Mapping[str, Sequence[str]],
    *,
    weights: Mapping[str, float] | None = None,
    k: int = RRF_K,
) -> list[FusedItem]:
    """Fuse ranked id lists into one ordering. Pure, deterministic, no I/O.

    Each signal contributes ``weight / (k + rank)`` for the ranks it produced,
    counting from 1. An id absent from a signal contributes nothing from it -
    absence is neutral, never a penalty, because a missing embedding must not
    push a perfectly good lexical hit down the page.

    Ties break on id ascending so that two runs over the same data produce the
    same order; a retrieval layer whose output reshuffles between runs makes
    every downstream evaluation unreproducible.
    """
    if k < 1:
        raise ValidationError("RRF k must be >= 1", context={"k": k})

    effective = dict(DEFAULT_WEIGHTS)
    if weights:
        effective.update(weights)

    totals: dict[str, float] = {}
    parts: dict[str, dict[str, float]] = {}
    for signal, ids in ranked_lists.items():
        weight = effective.get(signal, 0.0)
        if weight == 0.0:
            continue
        seen: set[str] = set()
        rank = 0
        for item_id in ids:
            # A signal that returns the same id twice must not double-count it.
            if item_id in seen:
                continue
            seen.add(item_id)
            rank += 1
            contribution = weight / (k + rank)
            totals[item_id] = totals.get(item_id, 0.0) + contribution
            parts.setdefault(item_id, {})[signal] = contribution

    ordered = sorted(totals.items(), key=lambda kv: (-kv[1], kv[0]))
    return [FusedItem(id=i, score=s, contributions=dict(parts[i])) for i, s in ordered]


def recency_rank(
    ages_days: Mapping[str, float], *, half_life_days: float = RECENCY_HALF_LIFE_DAYS
) -> list[str]:
    """Order ids newest-first relative to the incident start.

    Exposed as a rank list rather than a score so it enters fusion the same way
    every other signal does. Negative ages (material created after the incident
    began, e.g. the postmortem being written) are clamped to zero rather than
    boosted - a document written *because* of this incident is not evidence
    about its cause.
    """
    if half_life_days <= 0:
        raise ValidationError(
            "half_life_days must be > 0", context={"half_life_days": half_life_days}
        )
    scored = {i: 0.5 ** (max(age, 0.0) / half_life_days) for i, age in ages_days.items()}
    return [i for i, _ in sorted(scored.items(), key=lambda kv: (-kv[1], kv[0]))]


# --------------------------------------------------------------------------- #
# results                                                                      #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class RetrievedChunk:
    """A citable passage.

    Everything needed to render a precise citation is carried on the chunk
    itself. A retrieval result that cannot be traced back to a file and a line
    range, or to a titled document, is not usable as evidence.
    """

    id: str
    kind: str                       # "document" | "code"
    content: str
    score: float
    sub_scores: Mapping[str, float]
    source: str                     # doc_type, or "owner/repo"
    provenance_uri: str
    title: str = ""
    repo: str = ""
    ref: str = ""
    path: str = ""
    symbol: str = ""
    start_line: int = 0
    end_line: int = 0
    services: tuple[str, ...] = ()
    created_at: datetime | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @property
    def citation(self) -> str:
        """Human-readable locator shown next to the passage in the UI."""
        if self.kind == "code" and self.path:
            return f"{self.repo}/{self.path}:L{self.start_line}-L{self.end_line}"
        return self.title or self.provenance_uri or self.id


@dataclass(frozen=True, slots=True)
class SearchResult:
    """Ranked chunks plus the honest status of the search that produced them.

    Iterable and indexable so callers can treat it as the list of chunks it
    mostly is, while ``degraded`` travels with the data instead of being
    returned out-of-band and dropped at the first call site that forgets it.
    """

    chunks: tuple[RetrievedChunk, ...]
    degraded: bool = False
    degraded_reason: str = ""
    signals_used: tuple[str, ...] = ()
    query: str = ""

    def __iter__(self) -> Iterator[RetrievedChunk]:
        return iter(self.chunks)

    def __len__(self) -> int:
        return len(self.chunks)

    def __getitem__(self, index: int) -> RetrievedChunk:
        return self.chunks[index]

    @property
    def is_empty(self) -> bool:
        """True when the search ran and found nothing.

        Distinct from ``degraded``: empty-and-not-degraded means the corpus
        genuinely has no match, which is a finding. Empty-and-degraded means we
        could not look properly, which is an evidence gap.
        """
        return not self.chunks


# --------------------------------------------------------------------------- #
# retriever                                                                    #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class _Candidate:
    """A row fetched by one of the signal queries, before fusion."""

    id: str
    kind: str
    content: str
    source: str
    provenance_uri: str
    title: str
    repo: str
    ref: str
    path: str
    symbol: str
    start_line: int
    end_line: int
    services: tuple[str, ...]
    created_at: datetime | None
    metadata: Mapping[str, Any]


_DOC_COLUMNS = """
    id, 'document' AS kind, title, body AS content, doc_type AS source,
    provenance_uri, services, metadata, created_at,
    '' AS repo, '' AS ref, '' AS path, '' AS symbol, 0 AS start_line, 0 AS end_line
"""

_CODE_COLUMNS = """
    id, 'code' AS kind, coalesce(symbol, path) AS title, content, repo AS source,
    '' AS provenance_uri,
    CASE WHEN service_id IS NULL THEN '{}'::text[] ELSE ARRAY[service_id] END AS services,
    metadata, indexed_at AS created_at,
    repo, ref, path, coalesce(symbol, '') AS symbol, start_line, end_line
"""


# Every query below interpolates only the two module-level column constants
# above - fixed SQL text, never caller input. All *values* cross as asyncpg bind
# parameters. Audited; the noqa markers are why each one is safe.

_SQL_DOC_LEXICAL = f"""
    SELECT {_DOC_COLUMNS}, ts_rank_cd(tsv, q) AS signal
    FROM retrieval_documents, plainto_tsquery('english', $1) q
    WHERE tsv @@ q
    ORDER BY signal DESC, id
    LIMIT $2
"""  # noqa: S608

_SQL_CODE_LEXICAL = f"""
    SELECT {_CODE_COLUMNS}, ts_rank_cd(tsv, q) AS signal
    FROM code_documents, plainto_tsquery('english', $1) q
    WHERE tsv @@ q
    ORDER BY signal DESC, id
    LIMIT $2
"""  # noqa: S608

_SQL_DOC_VECTOR = f"""
    SELECT {_DOC_COLUMNS}, 1 - (embedding <=> $1::vector) AS signal
    FROM retrieval_documents
    WHERE embedding IS NOT NULL
    ORDER BY embedding <=> $1::vector, id
    LIMIT $2
"""  # noqa: S608

_SQL_CODE_VECTOR = f"""
    SELECT {_CODE_COLUMNS}, 1 - (embedding <=> $1::vector) AS signal
    FROM code_documents
    WHERE embedding IS NOT NULL
    ORDER BY embedding <=> $1::vector, id
    LIMIT $2
"""  # noqa: S608

_SQL_DOC_GRAPH = f"""
    SELECT {_DOC_COLUMNS}, ts_rank_cd(tsv, q) AS signal
    FROM retrieval_documents, plainto_tsquery('english', $1) q
    WHERE services && $2::text[] AND tsv @@ q
    ORDER BY signal DESC, id
    LIMIT $3
"""  # noqa: S608

_SQL_CODE_GRAPH = f"""
    SELECT {_CODE_COLUMNS}, ts_rank_cd(tsv, q) AS signal
    FROM code_documents, plainto_tsquery('english', $1) q
    WHERE service_id = ANY($2::text[]) AND tsv @@ q
    ORDER BY signal DESC, id
    LIMIT $3
"""  # noqa: S608


class HybridRetriever:
    """Lexical + vector + graph + recency retrieval over the Aegis corpus."""

    __slots__ = ("_db", "_embeddings", "_clock")

    def __init__(
        self,
        db: Database,
        embeddings: EmbeddingClient | None = None,
        *,
        clock: Clock = SYSTEM_CLOCK,
    ) -> None:
        self._db = db
        self._embeddings = embeddings
        self._clock = clock

    async def search(
        self,
        query: str,
        *,
        scope: RetrievalScope = RetrievalScope.ALL,
        limit: int = 10,
        incident_id: str | None = None,
        service_ids: Sequence[str] | None = None,
        reference_time: datetime | None = None,
    ) -> SearchResult:
        """Retrieve the ``limit`` best chunks for ``query``.

        ``service_ids`` is the graph-derived scope: the services topology
        analysis already implicated. It is supplied by the caller rather than
        queried here, because Neo4j being down must degrade retrieval quality
        and never block it (CLAUDE.md invariant 9).
        """
        cleaned = query.strip()
        if not cleaned:
            raise ValidationError("retrieval query must not be empty")
        if len(cleaned) > MAX_QUERY_CHARS:
            raise ValidationError(
                "retrieval query exceeds the maximum length",
                context={"length": len(cleaned), "max": MAX_QUERY_CHARS},
            )
        effective_limit = min(max(int(limit), 1), MAX_RESULT_LIMIT)
        pool = min(effective_limit * _POOL_MULTIPLIER, _MAX_POOL)
        services = [s for s in (service_ids or []) if s][:32]

        candidates: dict[str, _Candidate] = {}
        ranked: dict[str, list[str]] = {}
        signals: list[str] = []

        lexical = await self._lexical(cleaned, scope, pool)
        _absorb(candidates, lexical)
        ranked[SIGNAL_LEXICAL] = [c.id for c in lexical]
        signals.append(SIGNAL_LEXICAL)

        degraded, reason = await self._vector_signal(
            cleaned, scope, pool, candidates, ranked, signals
        )

        if services:
            scoped = await self._graph_scoped(cleaned, scope, pool, services)
            _absorb(candidates, scoped)
            ranked[SIGNAL_GRAPH] = [c.id for c in scoped]
            signals.append(SIGNAL_GRAPH)

        anchor = await self._anchor_time(incident_id, reference_time)
        ages = {
            c.id: (anchor - c.created_at).total_seconds() / 86_400.0
            for c in candidates.values()
            if c.created_at is not None
        }
        if ages:
            ranked[SIGNAL_RECENCY] = recency_rank(ages)
            signals.append(SIGNAL_RECENCY)

        fused = reciprocal_rank_fusion(ranked)
        chunks = tuple(
            _to_chunk(candidates[item.id], item)
            for item in fused[:effective_limit]
            if item.id in candidates
        )

        if degraded:
            log.warning(
                "hybrid search degraded",
                incident_id=incident_id,
                reason=reason,
                results=len(chunks),
            )
        return SearchResult(
            chunks=chunks,
            degraded=degraded,
            degraded_reason=reason,
            signals_used=tuple(signals),
            query=cleaned,
        )

    # ------------------------------------------------------------------ #
    # signals                                                             #
    # ------------------------------------------------------------------ #

    async def _vector_signal(
        self,
        query: str,
        scope: RetrievalScope,
        pool: int,
        candidates: dict[str, _Candidate],
        ranked: dict[str, list[str]],
        signals: list[str],
    ) -> tuple[bool, str]:
        """Run the semantic signal, or report precisely why it did not run."""
        if self._embeddings is None or not self._embeddings.configured:
            return True, "embeddings_not_configured"
        try:
            vector = await self._embeddings.embed_one(query)
        except SourceUnavailable as exc:
            return True, f"embedding_source_unavailable: {exc.message}"

        rows = await self._vector(vector, scope, pool)
        _absorb(candidates, rows)
        ranked[SIGNAL_VECTOR] = [c.id for c in rows]
        signals.append(SIGNAL_VECTOR)
        return False, ""

    async def _lexical(
        self, query: str, scope: RetrievalScope, pool: int
    ) -> list[_Candidate]:
        out: list[_Candidate] = []
        if scope in (RetrievalScope.DOCUMENTS, RetrievalScope.ALL):
            out.extend(_rows(await self._db.fetch(_SQL_DOC_LEXICAL, query, pool)))
        if scope in (RetrievalScope.CODE, RetrievalScope.ALL):
            out.extend(_rows(await self._db.fetch(_SQL_CODE_LEXICAL, query, pool)))
        # Interleaving two corpora by their own ts_rank would compare scores
        # across different tsvector populations, which is meaningless. Ranking
        # each corpus and letting RRF merge them avoids that comparison.
        return out

    async def _vector(
        self, vector: list[float], scope: RetrievalScope, pool: int
    ) -> list[_Candidate]:
        literal = vector_literal(vector)
        out: list[_Candidate] = []
        if scope in (RetrievalScope.DOCUMENTS, RetrievalScope.ALL):
            out.extend(_rows(await self._db.fetch(_SQL_DOC_VECTOR, literal, pool)))
        if scope in (RetrievalScope.CODE, RetrievalScope.ALL):
            out.extend(_rows(await self._db.fetch(_SQL_CODE_VECTOR, literal, pool)))
        return out

    async def _graph_scoped(
        self, query: str, scope: RetrievalScope, pool: int, services: list[str]
    ) -> list[_Candidate]:
        """Lexical search restricted to the services topology implicated.

        Restricting the *signal* rather than the whole search is deliberate: an
        out-of-scope document can still win on lexical and vector evidence, so a
        wrong topology guess degrades ranking instead of hiding the answer.
        """
        out: list[_Candidate] = []
        if scope in (RetrievalScope.DOCUMENTS, RetrievalScope.ALL):
            out.extend(_rows(await self._db.fetch(_SQL_DOC_GRAPH, query, services, pool)))
        if scope in (RetrievalScope.CODE, RetrievalScope.ALL):
            out.extend(_rows(await self._db.fetch(_SQL_CODE_GRAPH, query, services, pool)))
        return out

    async def _anchor_time(
        self, incident_id: str | None, reference_time: datetime | None
    ) -> datetime:
        """The point recency decays away from.

        The incident's own start, not 'now': an investigation that runs for an
        hour must not reshuffle its retrieval results as it goes.
        """
        if reference_time is not None:
            return reference_time
        if incident_id:
            started = await self._db.fetchval(
                "SELECT created_at FROM incidents WHERE id = $1", incident_id
            )
            if isinstance(started, datetime):
                return started
        return self._clock.now()


# --------------------------------------------------------------------------- #
# row plumbing                                                                 #
# --------------------------------------------------------------------------- #


def _rows(rows: Sequence[Any]) -> list[_Candidate]:
    return [
        _Candidate(
            id=r["id"],
            kind=r["kind"],
            content=r["content"] or "",
            source=r["source"] or "",
            provenance_uri=r["provenance_uri"] or "",
            title=r["title"] or "",
            repo=r["repo"] or "",
            ref=r["ref"] or "",
            path=r["path"] or "",
            symbol=r["symbol"] or "",
            start_line=int(r["start_line"] or 0),
            end_line=int(r["end_line"] or 0),
            services=tuple(r["services"] or ()),
            created_at=r["created_at"],
            metadata=dict(r["metadata"] or {}),
        )
        for r in rows
    ]


def _absorb(store: dict[str, _Candidate], found: Sequence[_Candidate]) -> None:
    for candidate in found:
        store.setdefault(candidate.id, candidate)


def _to_chunk(candidate: _Candidate, fused: FusedItem) -> RetrievedChunk:
    provenance = candidate.provenance_uri
    if not provenance and candidate.kind == "code":
        provenance = code_provenance_uri(
            repo=candidate.repo,
            ref=candidate.ref,
            path=candidate.path,
            start_line=candidate.start_line,
            end_line=candidate.end_line,
        )
    return RetrievedChunk(
        id=candidate.id,
        kind=candidate.kind,
        content=candidate.content,
        score=fused.score,
        sub_scores=dict(fused.contributions),
        source=candidate.source,
        provenance_uri=provenance,
        title=candidate.title,
        repo=candidate.repo,
        ref=candidate.ref,
        path=candidate.path,
        symbol=candidate.symbol,
        start_line=candidate.start_line,
        end_line=candidate.end_line,
        services=candidate.services,
        created_at=candidate.created_at,
        metadata=candidate.metadata,
    )


def code_provenance_uri(
    *, repo: str, ref: str, path: str, start_line: int, end_line: int
) -> str:
    """The citation form used everywhere code is referenced.

    Pinned to a commit sha rather than a branch. A citation against ``main``
    stops pointing at the reviewed lines the moment anyone merges.
    """
    anchor = f"#L{start_line}-L{end_line}" if start_line and end_line else ""
    return f"github://{repo}/blob/{ref}/{path}{anchor}"


__all__ = [
    "DEFAULT_WEIGHTS",
    "MAX_QUERY_CHARS",
    "MAX_RESULT_LIMIT",
    "RECENCY_HALF_LIFE_DAYS",
    "RRF_K",
    "SIGNAL_GRAPH",
    "SIGNAL_LEXICAL",
    "SIGNAL_RECENCY",
    "SIGNAL_VECTOR",
    "WEIGHT_GRAPH",
    "WEIGHT_LEXICAL",
    "WEIGHT_RECENCY",
    "WEIGHT_VECTOR",
    "FusedItem",
    "HybridRetriever",
    "RetrievalScope",
    "RetrievedChunk",
    "SearchResult",
    "code_provenance_uri",
    "recency_rank",
    "reciprocal_rank_fusion",
]
