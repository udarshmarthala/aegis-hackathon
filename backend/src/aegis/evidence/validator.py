"""Grounding enforcement.

The reasoning contract (AIArchitecture 14) is:

    claim -> evidence ids -> validation -> claim allowed

A claim whose citations do not resolve is not softened or annotated, it is
rejected. The orchestrator converts a rejection into an abstention, which is a
legitimate outcome, rather than letting an ungrounded assertion reach an
operator or a policy decision.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from aegis.core.errors import EvidenceError
from aegis.core.logging import get_logger
from aegis.domain.enums import EvidenceStatus, TrustClass
from aegis.domain.models import Diagnosis, EvidenceItem

log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class ValidationReport:
    valid: bool
    resolved: list[str] = field(default_factory=list)
    unknown: list[str] = field(default_factory=list)      # cited but nonexistent
    foreign: list[str] = field(default_factory=list)      # belongs to another incident
    refuted: list[str] = field(default_factory=list)
    unavailable: list[str] = field(default_factory=list)  # source was down
    tier_a_count: int = 0

    @property
    def problems(self) -> list[str]:
        out: list[str] = []
        if self.unknown:
            out.append(f"cited evidence does not exist: {', '.join(self.unknown)}")
        if self.foreign:
            out.append(f"cited evidence belongs to another incident: {', '.join(self.foreign)}")
        if self.refuted:
            out.append(f"cited evidence was refuted: {', '.join(self.refuted)}")
        if self.unavailable:
            out.append(
                "cited evidence came from an unavailable source: "
                + ", ".join(self.unavailable)
            )
        return out


class EvidenceValidator:
    """Validates citations against what is actually stored."""

    __slots__ = ("_store",)

    def __init__(self, store: object) -> None:
        # Typed loosely to avoid a circular import with EvidenceStore; the only
        # methods used are get_many() and list_for_incident().
        self._store = store

    async def validate_citations(
        self, incident_id: str, evidence_ids: list[str]
    ) -> ValidationReport:
        if not evidence_ids:
            return ValidationReport(valid=False)

        found: dict[str, EvidenceItem] = await self._store.get_many(evidence_ids)  # type: ignore[attr-defined]

        resolved: list[str] = []
        unknown: list[str] = []
        foreign: list[str] = []
        refuted: list[str] = []
        unavailable: list[str] = []
        tier_a = 0

        for eid in evidence_ids:
            item = found.get(eid)
            if item is None:
                unknown.append(eid)
                continue
            if item.incident_id != incident_id:
                foreign.append(eid)
                continue
            if item.status is EvidenceStatus.REFUTED:
                refuted.append(eid)
                continue
            if item.status is EvidenceStatus.SOURCE_UNAVAILABLE:
                unavailable.append(eid)
                continue
            resolved.append(eid)
            if item.trust_class is TrustClass.TIER_A:
                tier_a += 1

        report = ValidationReport(
            valid=bool(resolved) and not (unknown or foreign or refuted or unavailable),
            resolved=resolved,
            unknown=unknown,
            foreign=foreign,
            refuted=refuted,
            unavailable=unavailable,
            tier_a_count=tier_a,
        )
        if not report.valid:
            log.warning(
                "citation validation failed",
                incident_id=incident_id,
                cited=len(evidence_ids),
                resolved=len(resolved),
                problems=report.problems,
            )
        return report

    async def validate_diagnosis(self, diagnosis: Diagnosis) -> ValidationReport:
        """An abstention needs no citations; a conclusion always does."""
        if diagnosis.abstained:
            return ValidationReport(valid=True)

        report = await self.validate_citations(
            diagnosis.incident_id, diagnosis.supporting_evidence
        )
        if not report.valid:
            raise EvidenceError(
                "diagnosis is not grounded in valid evidence",
                context={
                    "incident_id": diagnosis.incident_id,
                    "problems": report.problems,
                },
            )
        if report.tier_a_count == 0:
            raise EvidenceError(
                "diagnosis cites no direct machine observation",
                context={"incident_id": diagnosis.incident_id},
            )
        return report


def abstain(incident_id: str, reason: str, missing: list[str] | None = None) -> Diagnosis:
    """Build a well-formed 'we do not know yet'.

    Abstention is a product feature, not an error path - an on-call engineer is
    better served by an honest gap than by a confident guess (PRD 4.5).
    """
    return Diagnosis(
        incident_id=incident_id,
        abstained=True,
        statement=reason,
        confidence=0.0,
        missing_evidence=missing or [],
        uncertainty=reason,
    )
