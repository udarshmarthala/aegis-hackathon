"""Was the conclusion grounded, and was the right evidence found?

Two different failures live here and the benchmark must tell them apart:

* **Evidence recall** - the investigation never looked at the thing that would
  have decided the question. A knowledge or tool-selection problem.
* **Unsupported claims** - the conclusion cited evidence that does not resolve:
  an id that does not exist, one belonging to another incident, or one that was
  refuted. That is fabrication, and it is the most dangerous output this system
  can produce, because it reads exactly like a grounded answer.

The citation side reuses ``EvidenceValidator`` rather than reimplementing
resolution, so the benchmark measures the same rule production enforces. A
citation pointing at a source that was *unavailable* is counted separately: the
investigation did not invent it, the world was missing (PRD 13).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from aegis.domain.enums import FailureClass
from aegis.evaluation.evaluators.base import (
    Determinism,
    EvaluatorResult,
    MetricValue,
    ratio,
    register_deterministic_metrics,
)
from aegis.evaluation.outcome import RunOutcome
from aegis.evaluation.schema import ExpectedEvidence, GroundTruth
from aegis.evidence.validator import ValidationReport

METRICS = (
    "evidence_precision",
    "evidence_recall",
    "evidence_completeness",
    "citation_validity",
    "unsupported_claim_rate",
    "citation_unavailable_rate",
    "tier_a_citation_rate",
)
register_deterministic_metrics("evidence", METRICS)

RECALL_FAILURE_THRESHOLD = 0.5


class _Validator(Protocol):
    """Structural view of ``EvidenceValidator``: only the method used here."""

    async def validate_citations(
        self, incident_id: str, evidence_ids: list[str]
    ) -> ValidationReport:
        ...


async def validate_outcome(
    validator: _Validator, outcome: RunOutcome
) -> ValidationReport | None:
    """Resolve the run's citations with the production validator.

    ``None`` when there is nothing to validate (no incident, or an abstention
    that cited nothing) - distinct from "validated and found wanting".
    """
    if outcome.incident_id is None or not outcome.cited_evidence_ids:
        return None
    return await validator.validate_citations(
        outcome.incident_id, list(outcome.cited_evidence_ids)
    )


def _matches(expected: ExpectedEvidence, observed_key: tuple[str, str, str | None]) -> bool:
    source_type, evidence_type, resource_id = observed_key
    if expected.source_type.value != source_type:
        return False
    if expected.evidence_type.value != evidence_type:
        return False
    # A resource-scoped expectation is only satisfied by evidence about that
    # resource: "some service's CPU" is not evidence about the faulty one.
    return expected.resource_id is None or expected.resource_id == resource_id


@dataclass(frozen=True, slots=True)
class EvidenceEvaluator:
    """Deterministic. Set matching plus the production citation validator."""

    name: str = "evidence"
    version: str = "1.0.0"
    determinism: Determinism = Determinism.DETERMINISTIC

    def evaluate(
        self,
        truth: GroundTruth,
        outcome: RunOutcome,
        report: ValidationReport | None = None,
    ) -> EvaluatorResult:
        metrics: list[MetricValue] = []
        failures: list[FailureClass] = []
        notes: list[str] = []

        usable = [e for e in outcome.evidence if not e.is_gap]
        expected: Sequence[ExpectedEvidence] = truth.expected_evidence
        required = truth.required_evidence

        matched_expected = [
            e for e in expected if any(_matches(e, obs.key) for obs in usable)
        ]
        relevant_observed = [
            obs for obs in usable if any(_matches(e, obs.key) for e in expected)
        ]

        precision = ratio(len(relevant_observed), len(usable)) if expected else None
        recall = ratio(len(matched_expected), len(expected))
        completeness = ratio(
            sum(1 for e in required if any(_matches(e, obs.key) for obs in usable)),
            len(required),
        )

        metrics.append(
            MetricValue(
                "evidence_precision",
                precision,
                sample_size=len(usable),
                detail={"collected": len(usable), "relevant": len(relevant_observed)},
            )
        )
        metrics.append(
            MetricValue(
                "evidence_recall",
                recall,
                sample_size=len(expected),
                detail={
                    "missing": [
                        f"{e.source_type.value}/{e.evidence_type.value}"
                        + (f"@{e.resource_id}" if e.resource_id else "")
                        for e in expected
                        if e not in matched_expected
                    ][:20]
                },
            )
        )
        metrics.append(
            MetricValue("evidence_completeness", completeness, sample_size=len(required))
        )

        if recall is not None and recall < RECALL_FAILURE_THRESHOLD:
            failures.append(FailureClass.EVIDENCE_FAILURE)
            notes.append(f"evidence recall {recall:.2f} below {RECALL_FAILURE_THRESHOLD}")

        cited = len(outcome.cited_evidence_ids)
        if report is None:
            # Two very different states share this branch and must not be
            # conflated, for the same reason "no evidence found" is not "source
            # unavailable": a conclusion that cited nothing is ungrounded, while
            # citations nobody validated are simply unmeasured.
            concluded_without_citations = (
                cited == 0 and not outcome.abstained and not outcome.is_harness_failure
            )
            if concluded_without_citations:
                failures.append(FailureClass.GROUNDING_FAILURE)
                notes.append("conclusion cited no evidence at all")
                metrics.append(MetricValue("citation_validity", 0.0, sample_size=0))
                metrics.append(MetricValue("unsupported_claim_rate", 1.0, sample_size=0))
            else:
                if cited:
                    notes.append(
                        f"{cited} citation(s) were not validated: no evidence "
                        "validator was supplied to the harness"
                    )
                metrics.append(MetricValue("citation_validity", None, sample_size=cited))
                metrics.append(
                    MetricValue("unsupported_claim_rate", None, sample_size=cited)
                )
            metrics.append(MetricValue("citation_unavailable_rate", None, sample_size=0))
            metrics.append(MetricValue("tier_a_citation_rate", None, sample_size=0))
        else:
            unsupported = len(report.unknown) + len(report.foreign) + len(report.refuted)
            metrics.append(
                MetricValue(
                    "citation_validity",
                    ratio(len(report.resolved), cited),
                    sample_size=cited,
                    detail={
                        "unknown": report.unknown[:10],
                        "foreign": report.foreign[:10],
                        "refuted": report.refuted[:10],
                    },
                )
            )
            metrics.append(
                MetricValue("unsupported_claim_rate", ratio(unsupported, cited), sample_size=cited)
            )
            metrics.append(
                MetricValue(
                    "citation_unavailable_rate",
                    ratio(len(report.unavailable), cited),
                    sample_size=cited,
                )
            )
            metrics.append(
                MetricValue(
                    "tier_a_citation_rate", ratio(report.tier_a_count, cited), sample_size=cited
                )
            )
            if unsupported:
                failures.append(FailureClass.GROUNDING_FAILURE)
                notes.extend(report.problems)

        return EvaluatorResult(
            evaluator=self.name,
            version=self.version,
            determinism=self.determinism,
            metrics=tuple(metrics),
            failure_classes=tuple(failures),
            notes=tuple(notes),
        )


def report_from_mapping(data: dict[str, Any]) -> ValidationReport:
    """Rebuild a ``ValidationReport`` from a persisted result row."""
    return ValidationReport(
        valid=bool(data.get("valid", False)),
        resolved=list(data.get("resolved", [])),
        unknown=list(data.get("unknown", [])),
        foreign=list(data.get("foreign", [])),
        refuted=list(data.get("refuted", [])),
        unavailable=list(data.get("unavailable", [])),
        tier_a_count=int(data.get("tier_a_count", 0)),
    )


__all__ = ["METRICS", "EvidenceEvaluator", "report_from_mapping", "validate_outcome"]
