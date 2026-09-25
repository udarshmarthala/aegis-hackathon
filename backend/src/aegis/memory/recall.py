"""Structured incident memory - the read path.

Recall answers one question during an incident: "have we seen this before, and
what did we learn?" Two mechanisms answer it, and they are deliberately not
merged:

* **Recurrence signature** - an exact fingerprint match. Strong, cheap, and
  means the same failure mode on the same services with the same cause category.
* **Hybrid similarity** - lexical plus semantic search over the memory corpus.
  Weaker, and catches the case where the symptom is worded differently or the
  blast radius differs.

A signature match and a similarity match carry different confidence and say so.
Collapsing them into one number would let a loose textual resemblance be
presented with the authority of an exact recurrence.

Everything returned here is Tier C. The evidence store assigns that trust from
``SourceType.MEMORY``; nothing in this module chooses it.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Final

from aegis.core.clock import SYSTEM_CLOCK, Clock
from aegis.core.errors import ValidationError
from aegis.core.ids import is_id
from aegis.core.logging import get_logger
from aegis.domain.enums import EvidenceType, SourceType
from aegis.evidence.store import EvidenceStore
from aegis.memory.store import (
    MEMORY_COLUMNS,
    IncidentMemory,
    normalise_symptom,
    recurrence_signature,
    row_to_memory,
)
from aegis.persistence.db import Database
from aegis.retrieval.hybrid import HybridRetriever, RetrievalScope, SearchResult

log = get_logger(__name__)

MAX_RECALL_LIMIT: Final = 20
MAX_QUERY_CHARS: Final = 1_000
MAX_PATTERN_ROWS: Final = 50
MAX_WINDOW_DAYS: Final = 365

MATCH_SIGNATURE: Final = "signature"
MATCH_SIMILARITY: Final = "similarity"

# An exact recurrence signature is a structural match on normalised symptom,
# services and cause category - about as strong as retrieval gets. It stops
# short of 1.0 because the normalisation that produced it is lossy by design.
CONFIDENCE_SIGNATURE: Final = 0.9
# Textual similarity is a starting point for a human, not a conclusion. Capped
# well below the signature ceiling so the two are never confusable in the UI.
CONFIDENCE_SIMILARITY_MAX: Final = 0.6
# A memory that has fired repeatedly is more likely to be the same thing again.
# Bounded so a noisy recurring alert cannot dominate every future recall.
OCCURRENCE_BONUS_PER_REPEAT: Final = 0.02
OCCURRENCE_BONUS_CAP: Final = 0.08


@dataclass(frozen=True, slots=True)
class MemoryMatch:
    """A prior incident offered as precedent, with why and how strongly."""

    memory: IncidentMemory
    confidence: float
    match_type: str
    reason: str
    provenance_uri: str
    shared_services: tuple[str, ...] = ()

    @property
    def is_exact_recurrence(self) -> bool:
        return self.match_type == MATCH_SIGNATURE


@dataclass(frozen=True, slots=True)
class RecurringPattern:
    """One repeated failure mode, for the Recurring Failures surface."""

    fingerprint: str
    title: str
    cause_category: str
    services: tuple[str, ...]
    occurrences: int
    first_seen_at: datetime | None
    last_seen_at: datetime | None
    prevention: str
    follow_ups: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RecallResult:
    """Matches plus the honest status of the lookup that produced them."""

    matches: tuple[MemoryMatch, ...]
    degraded: bool = False
    degraded_reason: str = ""

    def __iter__(self) -> Iterator[MemoryMatch]:
        return iter(self.matches)

    def __len__(self) -> int:
        return len(self.matches)

    @property
    def is_empty(self) -> bool:
        """No precedent found. Distinct from a degraded lookup."""
        return not self.matches


class IncidentMemoryRecall:
    """Finds prior incidents that resemble the one in progress."""

    __slots__ = ("_db", "_retriever", "_clock")

    def __init__(
        self,
        db: Database,
        retriever: HybridRetriever | None = None,
        *,
        clock: Clock = SYSTEM_CLOCK,
    ) -> None:
        self._db = db
        self._retriever = retriever
        self._clock = clock

    async def similar(
        self,
        incident_id_or_symptom: str,
        services: Sequence[str] = (),
        limit: int = 5,
        *,
        cause_category: str = "",
    ) -> RecallResult:
        """Recall precedents for an incident id or a raw symptom description.

        Accepts either because the caller changes over the incident lifecycle:
        triage has only alert text, while a later investigation has a persisted
        incident whose symptoms are already recorded.
        """
        symptom, resolved_services = await self._resolve(incident_id_or_symptom, services)
        if not symptom.strip():
            raise ValidationError(
                "recall requires a symptom description or a known incident",
                context={"input": incident_id_or_symptom[:120]},
            )
        if len(symptom) > MAX_QUERY_CHARS:
            symptom = symptom[:MAX_QUERY_CHARS]
        capped = min(max(int(limit), 1), MAX_RECALL_LIMIT)

        matches: dict[str, MemoryMatch] = {}
        for match in await self._by_signature(symptom, resolved_services, cause_category, capped):
            matches[match.memory.id] = match

        degraded = False
        reason = ""
        if self._retriever is None:
            # Signature-only recall still works, but it cannot find a
            # differently worded description of the same failure.
            degraded = True
            reason = "no_retriever_configured_signature_matching_only"
        else:
            result = await self._retriever.search(
                symptom,
                scope=RetrievalScope.DOCUMENTS,
                limit=capped * 2,
                service_ids=list(resolved_services),
            )
            degraded = result.degraded
            reason = result.degraded_reason
            for match in await self._by_similarity(result, resolved_services, capped):
                # A signature hit already outranks any similarity hit for the
                # same memory; never downgrade it.
                matches.setdefault(match.memory.id, match)

        ordered = sorted(
            matches.values(), key=lambda m: (-m.confidence, m.memory.id)
        )[:capped]
        log.info(
            "memory recall",
            services=list(resolved_services)[:10],
            matches=len(ordered),
            degraded=degraded,
        )
        return RecallResult(matches=tuple(ordered), degraded=degraded, degraded_reason=reason)

    async def _resolve(
        self, value: str, services: Sequence[str]
    ) -> tuple[str, tuple[str, ...]]:
        cleaned = [s for s in dict.fromkeys(services) if s]
        if not is_id(value, "inc"):
            return value, tuple(cleaned)
        row = await self._db.fetchrow(
            "SELECT title, summary, affected_services FROM incidents WHERE id = $1", value
        )
        if row is None:
            raise ValidationError("unknown incident", context={"incident_id": value})
        symptom = f"{row['title']} {row['summary']}".strip()
        merged = list(dict.fromkeys([*cleaned, *(row["affected_services"] or [])]))
        return symptom, tuple(merged)

    # ------------------------------------------------------------------ #
    # signature matching                                                  #
    # ------------------------------------------------------------------ #

    async def _by_signature(
        self,
        symptom: str,
        services: Sequence[str],
        cause_category: str,
        limit: int,
    ) -> list[MemoryMatch]:
        """Exact fingerprint lookup.

        When the cause category is not yet known - the common case during
        triage, before a diagnosis exists - the fingerprint cannot be computed,
        so this falls back to matching the normalised symptom and the service
        set, which is the same structural comparison minus one dimension.
        """
        rows: list[Any]
        if cause_category.strip():
            fingerprint = recurrence_signature(
                symptom=symptom, services=services, cause_category=cause_category
            )
            rows = list(
                await self._db.fetch(
                    f"""
                    SELECT {MEMORY_COLUMNS} FROM incident_memories
                    WHERE approved = TRUE AND fingerprint = $1
                    ORDER BY last_seen_at DESC
                    LIMIT $2
                    """,  # noqa: S608 - MEMORY_COLUMNS is a module constant, not input
                    fingerprint,
                    limit,
                )
            )
        else:
            rows = list(
                await self._db.fetch(
                    f"""
                    SELECT {MEMORY_COLUMNS} FROM incident_memories
                    WHERE approved = TRUE
                      AND symptom_normalised = $1
                      AND ($2::text[] = '{{}}'::text[] OR affected_services && $2::text[])
                    ORDER BY occurrences DESC, last_seen_at DESC
                    LIMIT $3
                    """,  # noqa: S608 - MEMORY_COLUMNS is a module constant, not input
                    normalise_symptom(symptom),
                    list(services),
                    limit,
                )
            )

        out: list[MemoryMatch] = []
        for row in rows:
            memory = row_to_memory(row)
            shared = tuple(sorted(set(memory.affected_services) & set(services)))
            out.append(
                MemoryMatch(
                    memory=memory,
                    confidence=_with_occurrence_bonus(CONFIDENCE_SIGNATURE, memory.occurrences),
                    match_type=MATCH_SIGNATURE,
                    reason=(
                        f"exact recurrence signature; seen {memory.occurrences} time(s), "
                        f"cause category '{memory.cause_category}'"
                    ),
                    provenance_uri=_provenance(memory),
                    shared_services=shared,
                )
            )
        return out

    # ------------------------------------------------------------------ #
    # similarity matching                                                 #
    # ------------------------------------------------------------------ #

    async def _by_similarity(
        self, result: SearchResult, services: Sequence[str], limit: int
    ) -> list[MemoryMatch]:
        """Turn hybrid hits over memory-derived documents into matches.

        The retriever ranks the shared corpus, which includes chunks ingested
        from memories. Only rows that resolve back to an approved memory are
        returned - an unapproved memory is retrievable for a human browsing the
        corpus, but must not be cited to an agent as precedent.
        """
        ref_ids = [
            chunk.metadata.get("memory_id") or chunk.metadata.get("ref_id")
            for chunk in result
            if chunk.kind == "document"
        ]
        candidates = [str(r) for r in ref_ids if r][: limit * 2]
        if not candidates:
            return []

        rows = await self._db.fetch(
            f"""
            SELECT {MEMORY_COLUMNS} FROM incident_memories
            WHERE approved = TRUE AND id = ANY($1::text[])
            LIMIT $2
            """,  # noqa: S608 - MEMORY_COLUMNS is a module constant, not input
            candidates,
            limit,
        )
        by_id = {r["id"]: r for r in rows}

        out: list[MemoryMatch] = []
        for rank, memory_id in enumerate(candidates, start=1):
            row = by_id.get(memory_id)
            if row is None:
                continue
            memory = row_to_memory(row)
            shared = tuple(sorted(set(memory.affected_services) & set(services)))
            # Rank-derived rather than score-derived: RRF scores are only
            # meaningful relative to one another within a single query.
            base = CONFIDENCE_SIMILARITY_MAX * (1.0 / (1.0 + 0.5 * (rank - 1)))
            if shared:
                base = min(CONFIDENCE_SIMILARITY_MAX, base * 1.2)
            out.append(
                MemoryMatch(
                    memory=memory,
                    confidence=round(_with_occurrence_bonus(base, memory.occurrences), 4),
                    match_type=MATCH_SIMILARITY,
                    reason=(
                        f"textual similarity (rank {rank})"
                        + (f"; shares services {', '.join(shared)}" if shared else "")
                    ),
                    provenance_uri=_provenance(memory),
                    shared_services=shared,
                )
            )
        return out

    # ------------------------------------------------------------------ #
    # recurring patterns                                                  #
    # ------------------------------------------------------------------ #

    async def recurring_patterns(
        self, window_days: int = 90, *, min_occurrences: int = 2, limit: int = 20
    ) -> list[RecurringPattern]:
        """Repeated failure modes inside a window.

        Powers the "Recurring Failures" surface, whose product purpose is to
        turn a list of individually-resolved incidents into one piece of
        engineering work. Filtered to approved memories: an unapproved row is
        one operator's draft, not an organisational pattern.
        """
        if not 1 <= window_days <= MAX_WINDOW_DAYS:
            raise ValidationError(
                "window_days must be between 1 and 365", context={"window_days": window_days}
            )
        rows = await self._db.fetch(
            """
            SELECT fingerprint, title, cause_category, affected_services, occurrences,
                   first_seen_at, last_seen_at, prevention, follow_ups
            FROM incident_memories
            WHERE approved = TRUE
              AND fingerprint <> ''
              AND occurrences >= $1
              AND last_seen_at >= $2
            ORDER BY occurrences DESC, last_seen_at DESC
            LIMIT $3
            """,
            max(min_occurrences, 2),
            self._clock.now() - timedelta(days=window_days),
            min(max(limit, 1), MAX_PATTERN_ROWS),
        )
        return [
            RecurringPattern(
                fingerprint=r["fingerprint"],
                title=r["title"],
                cause_category=r["cause_category"],
                services=tuple(r["affected_services"] or ()),
                occurrences=int(r["occurrences"]),
                first_seen_at=r["first_seen_at"],
                last_seen_at=r["last_seen_at"],
                prevention=r["prevention"],
                follow_ups=tuple(r["follow_ups"] or ()),
            )
            for r in rows
        ]

    # ------------------------------------------------------------------ #
    # evidence                                                            #
    # ------------------------------------------------------------------ #

    async def to_evidence(
        self,
        evidence_store: EvidenceStore,
        incident_id: str,
        result: RecallResult,
        *,
        max_items: int = 5,
    ) -> list[str]:
        """Record recalled precedents as HISTORICAL_INCIDENT evidence.

        ``SourceType.MEMORY`` is passed through so the store assigns Tier C.
        This module never states a trust class; if it did, two places could
        disagree about how much a memory is worth.
        """
        recorded: list[str] = []
        for match in result.matches[:max_items]:
            memory = match.memory
            item = await evidence_store.record(
                incident_id=incident_id,
                source="incident_memory",
                source_type=SourceType.MEMORY,
                evidence_type=EvidenceType.HISTORICAL_INCIDENT,
                summary=(
                    f"prior incident '{memory.title}' ({match.match_type}, "
                    f"confidence {match.confidence:.2f}): {match.reason}"
                ),
                structured_value={
                    "memory_id": memory.id,
                    "prior_incident_id": memory.incident_id,
                    "fingerprint": memory.fingerprint,
                    "cause_category": memory.cause_category,
                    "occurrences": memory.occurrences,
                    "affected_services": list(memory.affected_services),
                    "shared_services": list(match.shared_services),
                    "successful_fix": memory.successful_fix,
                    "match_type": match.match_type,
                    "confidence": match.confidence,
                    "approved_by": memory.approved_by,
                },
                content=f"{memory.symptoms}\n\nRoot cause: {memory.root_cause}",
                provenance_uri=match.provenance_uri,
                observed_at=memory.last_seen_at,
            )
            recorded.append(item.id)

        if result.degraded:
            gap = await evidence_store.record_unavailable(
                incident_id=incident_id,
                source="incident_memory",
                source_type=SourceType.MEMORY,
                reason=result.degraded_reason or "memory recall degraded",
            )
            recorded.append(gap.id)
        return recorded


# --------------------------------------------------------------------------- #
# helpers                                                                      #
# --------------------------------------------------------------------------- #


def _provenance(memory: IncidentMemory) -> str:
    """Point at the prior incident when there is one, else at the memory row."""
    if memory.incident_id:
        return f"aegis://incident/{memory.incident_id}"
    return f"aegis://memory/{memory.id}"


def _with_occurrence_bonus(base: float, occurrences: int) -> float:
    bonus = min(OCCURRENCE_BONUS_CAP, OCCURRENCE_BONUS_PER_REPEAT * max(occurrences - 1, 0))
    return round(min(1.0, base + bonus), 4)


__all__ = [
    "CONFIDENCE_SIGNATURE",
    "CONFIDENCE_SIMILARITY_MAX",
    "MATCH_SIGNATURE",
    "MATCH_SIMILARITY",
    "IncidentMemoryRecall",
    "MemoryMatch",
    "RecallResult",
    "RecurringPattern",
]
