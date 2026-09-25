"""Structured incident memory - the write path.

Memory is the one store in Aegis that feeds itself. What is written here is
retrieved during the next incident, cited as a historical precedent, and shapes
which hypotheses an investigator even considers.

That feedback loop is why the write path is the strictest in the codebase:

* Only a **non-abstaining** diagnosis may be written. An abstention is a correct
  outcome, but it is a statement about evidence, not about cause; recalling one
  as "what happened last time" manufactures a root cause out of an admission of
  ignorance.
* Only a **verified** fix may be written. An unverified remediation is a
  hypothesis. Recalled six weeks later it reads as a proven playbook, and the
  next operator applies it to a superficially similar incident.
* Only an **approved** memory becomes organisational knowledge (PRD FR-16).

A contaminated memory store does not fail loudly. It quietly biases retrieval
for every future incident, and the bias is undetectable from inside the
investigation that suffers from it. Hence a typed refusal at the boundary
rather than a flag on the row.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Final

from aegis.core.clock import SYSTEM_CLOCK, Clock
from aegis.core.errors import DomainError, SourceUnavailable, ValidationError
from aegis.core.ids import new_id
from aegis.core.logging import get_logger
from aegis.domain.models import Diagnosis, VerificationResult
from aegis.persistence.db import Database
from aegis.retrieval.documents import vector_literal
from aegis.retrieval.embeddings import MAX_INPUT_CHARS, EmbeddingClient

log = get_logger(__name__)

# Bumped whenever normalisation changes, so two signatures computed by
# different code versions can never be compared as if they were equivalent.
SIGNATURE_VERSION: Final = "v1"

MAX_TIMELINE_ENTRIES: Final = 100
MAX_LIST_ITEMS: Final = 50
MAX_TEXT_CHARS: Final = 8_000

_NON_WORD: Final = re.compile(r"[^a-z0-9\s]+")
_WS: Final = re.compile(r"\s+")
# Numbers, ids, hostnames and durations vary between two occurrences of the
# same failure. Normalising them away is what lets a recurrence be recognised
# as a recurrence instead of as a brand-new incident.
_VOLATILE: Final = re.compile(r"\b(?:\d+(?:\.\d+)?[a-z%]*|[0-9a-f]{8,})\b")
_SIGNATURE_STOPWORDS: Final = frozenset(
    {
        "the", "a", "an", "and", "or", "of", "to", "in", "on", "for", "with",
        "is", "are", "was", "were", "at", "by", "from", "that", "this", "it",
    }
)


class MemoryContaminationError(DomainError):
    """A write was refused because it would poison future retrieval.

    Distinct from ``ValidationError``: the payload is well-formed, it is the
    *epistemic status* of the content that fails. Surfaced to the API so an
    operator sees "this diagnosis was never verified" rather than "invalid
    request body".
    """

    code = "MEMORY_CONTAMINATION"
    http_status = 409


# --------------------------------------------------------------------------- #
# recurrence signature                                                         #
# --------------------------------------------------------------------------- #


def normalise_symptom(symptom: str, *, max_terms: int = 24) -> str:
    """Reduce symptom prose to a stable, order-independent term set.

    Sorted, de-duplicated and stripped of volatile values, so "p99 latency 4200ms
    on checkout-7" and "p99 latency 9100ms on checkout-3" normalise to the same
    string. Without that, every occurrence of a recurring failure gets its own
    signature and the "Recurring Failures" surface stays permanently empty.
    """
    lowered = symptom.lower()
    lowered = _VOLATILE.sub(" ", lowered)
    lowered = _NON_WORD.sub(" ", lowered)
    words = [w for w in _WS.split(lowered) if w and w not in _SIGNATURE_STOPWORDS]
    return " ".join(sorted(dict.fromkeys(words))[:max_terms])


def recurrence_signature(
    *, symptom: str, services: Sequence[str], cause_category: str
) -> str:
    """Deterministic fingerprint over (normalised symptom, services, cause).

    Pure and versioned: the same three inputs always produce the same value in
    every process, which is what makes occurrence counting an idempotent upsert
    rather than a read-modify-write race between concurrent workers.
    """
    normalised = normalise_symptom(symptom)
    service_key = ",".join(sorted({s.strip() for s in services if s.strip()}))
    cause = _WS.sub(" ", cause_category.strip().lower())
    digest = hashlib.sha256(
        f"{SIGNATURE_VERSION}\x00{normalised}\x00{service_key}\x00{cause}".encode()
    ).hexdigest()
    return f"{SIGNATURE_VERSION}:{digest[:40]}"


# --------------------------------------------------------------------------- #
# record                                                                       #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class IncidentMemory:
    """A persisted, approved, verified incident lesson."""

    id: str
    incident_id: str | None
    title: str
    symptoms: str
    root_cause: str
    cause_category: str
    affected_services: tuple[str, ...]
    contributing_factors: tuple[str, ...]
    successful_fix: str
    failed_attempts: tuple[str, ...]
    verification: str
    verification_passed: bool
    prevention: str
    follow_ups: tuple[str, ...]
    related_commits: tuple[str, ...]
    related_deployments: tuple[str, ...]
    timeline: tuple[Mapping[str, Any], ...]
    evidence_ids: tuple[str, ...]
    fingerprint: str
    occurrences: int
    approved: bool
    approved_by: str | None
    diagnosis_confidence: float
    first_seen_at: datetime | None = None
    last_seen_at: datetime | None = None
    evidence_pattern: Mapping[str, Any] = field(default_factory=dict)


class IncidentMemoryStore:
    """Writes verified incident knowledge. Refuses everything else."""

    __slots__ = ("_db", "_clock", "_embeddings")

    def __init__(
        self,
        db: Database,
        *,
        clock: Clock = SYSTEM_CLOCK,
        embeddings: EmbeddingClient | None = None,
    ) -> None:
        self._db = db
        self._clock = clock
        self._embeddings = embeddings

    async def write(
        self,
        *,
        diagnosis: Diagnosis,
        verification: VerificationResult | None,
        title: str,
        symptoms: str,
        successful_fix: str,
        approved_by: str,
        cause_category: str | None = None,
        contributing_factors: Sequence[str] = (),
        failed_attempts: Sequence[str] = (),
        prevention: str = "",
        follow_ups: Sequence[str] = (),
        related_commits: Sequence[str] = (),
        related_deployments: Sequence[str] = (),
        timeline: Sequence[Mapping[str, Any]] = (),
        evidence_pattern: Mapping[str, Any] | None = None,
    ) -> IncidentMemory:
        """Persist one incident lesson, or refuse.

        Raises ``MemoryContaminationError`` when the diagnosis abstained, cites
        no evidence, or the fix was never verified. Raises ``ValidationError``
        for a malformed payload. The two are kept apart because only the first
        is a judgement about whether the knowledge is safe to recall.
        """
        self._guard(diagnosis, verification, approved_by)

        if not title.strip() or not symptoms.strip():
            raise ValidationError(
                "a memory requires a title and a symptom description",
                context={"incident_id": diagnosis.incident_id},
            )
        if not successful_fix.strip():
            raise MemoryContaminationError(
                "refusing to store a memory with no confirmed fix",
                context={"incident_id": diagnosis.incident_id},
            )

        category = (cause_category or diagnosis.root_cause_category or "").strip()
        if not category:
            raise MemoryContaminationError(
                "refusing to store a memory with no root-cause category: "
                "an uncategorised memory cannot be matched to a recurrence",
                context={"incident_id": diagnosis.incident_id},
            )

        services = [s for s in dict.fromkeys(diagnosis.affected_services) if s][:MAX_LIST_ITEMS]
        fingerprint = recurrence_signature(
            symptom=symptoms, services=services, cause_category=category
        )
        now = self._clock.now()
        embedding = await self._embed(title, symptoms, diagnosis.statement)

        row = await self._db.fetchrow(
            """
            INSERT INTO incident_memories
                (id, incident_id, title, symptoms, root_cause, evidence_pattern,
                 affected_services, successful_fix, failed_attempts, verification,
                 prevention, fingerprint, occurrences, approved, approved_by,
                 signature_version, symptom_normalised, cause_category,
                 contributing_factors, timeline, related_commits, related_deployments,
                 follow_ups, verification_passed, diagnosis_confidence, evidence_ids,
                 first_seen_at, last_seen_at, embedding, created_at, updated_at)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,1,TRUE,$13,$14,$15,$16,
                    $17,$18,$19,$20,$21,TRUE,$22,$23,$24,$24,$25::vector,$24,$24)
            ON CONFLICT (fingerprint) WHERE fingerprint <> ''
            DO UPDATE SET
                -- The same failure recurring is one memory with a higher count,
                -- not N near-identical rows competing in the same ranking.
                occurrences          = incident_memories.occurrences + 1,
                incident_id          = EXCLUDED.incident_id,
                root_cause           = EXCLUDED.root_cause,
                successful_fix       = EXCLUDED.successful_fix,
                failed_attempts      = EXCLUDED.failed_attempts,
                verification         = EXCLUDED.verification,
                prevention           = EXCLUDED.prevention,
                contributing_factors = EXCLUDED.contributing_factors,
                timeline             = EXCLUDED.timeline,
                related_commits      = EXCLUDED.related_commits,
                related_deployments  = EXCLUDED.related_deployments,
                follow_ups           = EXCLUDED.follow_ups,
                evidence_ids         = EXCLUDED.evidence_ids,
                diagnosis_confidence = EXCLUDED.diagnosis_confidence,
                approved_by          = EXCLUDED.approved_by,
                last_seen_at         = EXCLUDED.last_seen_at,
                embedding            = COALESCE(EXCLUDED.embedding,
                                                incident_memories.embedding),
                updated_at           = EXCLUDED.updated_at
            RETURNING id, occurrences, first_seen_at, last_seen_at
            """,
            new_id("mem"),
            diagnosis.incident_id,
            title.strip()[:MAX_TEXT_CHARS],
            symptoms.strip()[:MAX_TEXT_CHARS],
            diagnosis.statement[:MAX_TEXT_CHARS],
            dict(evidence_pattern or {}),
            services,
            successful_fix.strip()[:MAX_TEXT_CHARS],
            _bounded(failed_attempts),
            _verification_note(verification),
            prevention[:MAX_TEXT_CHARS],
            fingerprint,
            approved_by,
            SIGNATURE_VERSION,
            normalise_symptom(symptoms),
            category,
            _bounded(contributing_factors or diagnosis.contributing_factors),
            list(timeline)[:MAX_TIMELINE_ENTRIES],
            _bounded(related_commits),
            _bounded(related_deployments),
            _bounded(follow_ups),
            float(diagnosis.confidence),
            _bounded(diagnosis.supporting_evidence),
            now,
            vector_literal(embedding),
        )
        assert row is not None

        log.info(
            "incident memory written",
            incident_id=diagnosis.incident_id,
            memory_id=row["id"],
            fingerprint=fingerprint,
            occurrences=row["occurrences"],
        )
        return IncidentMemory(
            id=row["id"],
            incident_id=diagnosis.incident_id,
            title=title.strip(),
            symptoms=symptoms.strip(),
            root_cause=diagnosis.statement,
            cause_category=category,
            affected_services=tuple(services),
            contributing_factors=tuple(
                _bounded(contributing_factors or diagnosis.contributing_factors)
            ),
            successful_fix=successful_fix.strip(),
            failed_attempts=tuple(_bounded(failed_attempts)),
            verification=_verification_note(verification),
            verification_passed=True,
            prevention=prevention,
            follow_ups=tuple(_bounded(follow_ups)),
            related_commits=tuple(_bounded(related_commits)),
            related_deployments=tuple(_bounded(related_deployments)),
            timeline=tuple(list(timeline)[:MAX_TIMELINE_ENTRIES]),
            evidence_ids=tuple(_bounded(diagnosis.supporting_evidence)),
            fingerprint=fingerprint,
            occurrences=int(row["occurrences"]),
            approved=True,
            approved_by=approved_by,
            diagnosis_confidence=float(diagnosis.confidence),
            first_seen_at=row["first_seen_at"],
            last_seen_at=row["last_seen_at"],
            evidence_pattern=dict(evidence_pattern or {}),
        )

    # ------------------------------------------------------------------ #
    # the gate                                                            #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _guard(
        diagnosis: Diagnosis, verification: VerificationResult | None, approved_by: str
    ) -> None:
        """The contamination gate. Deterministic, and never bypassable.

        No caller argument can relax any of these. An LLM proposes what to
        remember; this function decides whether it may be remembered
        (CLAUDE.md invariant 1).
        """
        if diagnosis.abstained:
            raise MemoryContaminationError(
                "refusing to store an abstained diagnosis: recalling an "
                "admission of insufficient evidence as a precedent would "
                "fabricate a root cause for every future similar incident",
                context={"incident_id": diagnosis.incident_id, "reason": "abstained"},
            )
        if not diagnosis.supporting_evidence:
            raise MemoryContaminationError(
                "refusing to store a diagnosis that cites no evidence",
                context={"incident_id": diagnosis.incident_id, "reason": "ungrounded"},
            )
        if verification is None:
            raise MemoryContaminationError(
                "refusing to store an unverified remediation: an unverified fix "
                "is a hypothesis, and a recalled hypothesis reads as a playbook",
                context={"incident_id": diagnosis.incident_id, "reason": "unverified"},
            )
        if not verification.passed:
            raise MemoryContaminationError(
                "refusing to store a remediation whose verification failed",
                context={
                    "incident_id": diagnosis.incident_id,
                    "reason": "verification_failed",
                    "failed_checks": [c.name for c in verification.failed_checks][:10],
                },
            )
        if not approved_by.strip():
            # PRD FR-16: a memory becomes organisational knowledge only once a
            # human puts their name on it.
            raise MemoryContaminationError(
                "refusing to store a memory with no human approver",
                context={"incident_id": diagnosis.incident_id, "reason": "unapproved"},
            )

    # ------------------------------------------------------------------ #
    # maintenance                                                         #
    # ------------------------------------------------------------------ #

    async def get(self, memory_id: str) -> IncidentMemory | None:
        row = await self._db.fetchrow(
    f"SELECT {MEMORY_COLUMNS} FROM incident_memories WHERE id = $1",  # noqa: S608
            memory_id,
        )
        return row_to_memory(row) if row is not None else None

    async def delete(self, memory_id: str) -> bool:
        """Retract a memory outright.

        There is no "mark as wrong but keep it retrievable" state: a memory that
        is retrievable is a memory that will be cited.
        """
        status = await self._db.execute(
            "DELETE FROM incident_memories WHERE id = $1", memory_id
        )
        deleted = status.endswith(" 1")
        if deleted:
            log.info("incident memory retracted", memory_id=memory_id)
        return deleted

    async def _embed(self, *parts: str) -> list[float] | None:
        if self._embeddings is None or not self._embeddings.configured:
            return None
        text = "\n".join(p for p in parts if p)[:MAX_INPUT_CHARS]
        if not text:
            return None
        try:
            return await self._embeddings.embed_one(text)
        except SourceUnavailable as exc:
            # The memory is still written and still lexically retrievable.
            # Losing the row would be far worse than losing its vector.
            log.warning("memory stored without embedding", reason=exc.message)
            return None


# --------------------------------------------------------------------------- #
# row plumbing shared with recall                                              #
# --------------------------------------------------------------------------- #

MEMORY_COLUMNS: Final = """
    id, incident_id, title, symptoms, root_cause, cause_category, evidence_pattern,
    affected_services, contributing_factors, successful_fix, failed_attempts,
    verification, verification_passed, prevention, follow_ups, related_commits,
    related_deployments, timeline, evidence_ids, fingerprint, occurrences,
    approved, approved_by, diagnosis_confidence, first_seen_at, last_seen_at
"""


def row_to_memory(row: Any) -> IncidentMemory:
    return IncidentMemory(
        id=row["id"],
        incident_id=row["incident_id"],
        title=row["title"],
        symptoms=row["symptoms"],
        root_cause=row["root_cause"],
        cause_category=row["cause_category"],
        affected_services=tuple(row["affected_services"] or ()),
        contributing_factors=tuple(row["contributing_factors"] or ()),
        successful_fix=row["successful_fix"],
        failed_attempts=tuple(row["failed_attempts"] or ()),
        verification=row["verification"],
        verification_passed=bool(row["verification_passed"]),
        prevention=row["prevention"],
        follow_ups=tuple(row["follow_ups"] or ()),
        related_commits=tuple(row["related_commits"] or ()),
        related_deployments=tuple(row["related_deployments"] or ()),
        timeline=tuple(row["timeline"] or ()),
        evidence_ids=tuple(row["evidence_ids"] or ()),
        fingerprint=row["fingerprint"],
        occurrences=int(row["occurrences"] or 1),
        approved=bool(row["approved"]),
        approved_by=row["approved_by"],
        diagnosis_confidence=float(row["diagnosis_confidence"] or 0.0),
        first_seen_at=row["first_seen_at"],
        last_seen_at=row["last_seen_at"],
        evidence_pattern=dict(row["evidence_pattern"] or {}),
    )


def _bounded(values: Sequence[str]) -> list[str]:
    return [v for v in values if v][:MAX_LIST_ITEMS]


def _verification_note(verification: VerificationResult | None) -> str:
    if verification is None:
        return ""
    passed = sum(1 for c in verification.checks if c.passed)
    return (
        f"{passed}/{len(verification.checks)} checks passed"
        f"{'; ' + verification.notes if verification.notes else ''}"
    )[:MAX_TEXT_CHARS]


__all__ = [
    "MEMORY_COLUMNS",
    "SIGNATURE_VERSION",
    "IncidentMemory",
    "IncidentMemoryStore",
    "MemoryContaminationError",
    "normalise_symptom",
    "recurrence_signature",
    "row_to_memory",
]
