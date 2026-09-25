"""Hybrid retrieval over runbooks, postmortems and source code.

The public surface is deliberately small: ingest through ``DocumentStore``,
search through ``HybridRetriever``, localise code through ``CodeRetriever``.
Nothing outside this package should build SQL against the corpus tables.
"""

from __future__ import annotations

from aegis.retrieval.code import (
    CodeLocalization,
    CodeRetriever,
    CommitRecord,
    CommitSource,
    FileCandidate,
    StageTrace,
    SymbolCandidate,
    TestCandidate,
    localization_summary,
    symptom_terms,
)
from aegis.retrieval.documents import (
    Chunk,
    DocumentStore,
    chunk_text,
    classify_path,
    content_digest,
    vector_literal,
)
from aegis.retrieval.embeddings import EmbeddingClient
from aegis.retrieval.hybrid import (
    DEFAULT_WEIGHTS,
    MAX_QUERY_CHARS,
    MAX_RESULT_LIMIT,
    RRF_K,
    FusedItem,
    HybridRetriever,
    RetrievalScope,
    RetrievedChunk,
    SearchResult,
    code_provenance_uri,
    recency_rank,
    reciprocal_rank_fusion,
)

__all__ = [
    "DEFAULT_WEIGHTS",
    "MAX_QUERY_CHARS",
    "MAX_RESULT_LIMIT",
    "RRF_K",
    "Chunk",
    "CodeLocalization",
    "CodeRetriever",
    "CommitRecord",
    "CommitSource",
    "DocumentStore",
    "EmbeddingClient",
    "FileCandidate",
    "FusedItem",
    "HybridRetriever",
    "RetrievalScope",
    "RetrievedChunk",
    "SearchResult",
    "StageTrace",
    "SymbolCandidate",
    "TestCandidate",
    "chunk_text",
    "classify_path",
    "code_provenance_uri",
    "content_digest",
    "localization_summary",
    "recency_rank",
    "reciprocal_rank_fusion",
    "symptom_terms",
    "vector_literal",
]
