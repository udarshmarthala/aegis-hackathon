"""Derived confidence.

The number shown to an operator is computed from measurable properties of the
evidence, never copied from a model's self-report. A model asked "how sure are
you" produces a fluent number with no relationship to correctness; this produces
one that can be calibrated against the benchmark with Brier score and ECE.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from aegis.domain.models import EvidenceItem, Hypothesis

CONFIDENCE_MODEL_VERSION: Final = "1.0.0"

# Weights are versioned so a benchmark result is attributable to a specific
# confidence model. Changing these is an AI-behaviour change and needs a re-run.
W_COVERAGE: Final = 0.40
W_CORROBORATION: Final = 0.25
W_TEST_PASS: Final = 0.20
W_SOURCE_RELIABILITY: Final = 0.15
W_CONTRADICTION_PENALTY: Final = 0.30
# An unreachable source makes the picture incomplete; say so numerically.
W_GAP_PENALTY: Final = 0.10


@dataclass(frozen=True, slots=True)
class ConfidenceBreakdown:
    """Every input exposed, so the UI can explain the number (UX spec 22)."""

    value: float
    coverage: float
    corroboration: float
    test_pass_rate: float
    source_reliability: float
    contradiction_ratio: float
    gap_ratio: float
    supporting_count: int
    contradicting_count: int
    model_version: str = CONFIDENCE_MODEL_VERSION

    def explain(self) -> list[str]:
        return [
            f"{self.supporting_count} supporting, {self.contradicting_count} contradicting",
            f"{self.coverage:.0%} of predictions tested",
            f"{self.test_pass_rate:.0%} of tested predictions held",
            f"source reliability {self.source_reliability:.2f}",
        ] + ([f"{self.gap_ratio:.0%} of sources unavailable"] if self.gap_ratio else [])


def _clamp(x: float) -> float:
    return max(0.0, min(1.0, x))


def compute(
    hypothesis: Hypothesis,
    evidence: list[EvidenceItem],
) -> ConfidenceBreakdown:
    """Derive calibrated confidence for one hypothesis."""
    by_id = {e.id: e for e in evidence}
    supporting = [by_id[i] for i in hypothesis.supporting if i in by_id]
    contradicting = [by_id[i] for i in hypothesis.contradicting if i in by_id]
    usable = [e for e in evidence if not e.is_gap]
    gaps = [e for e in evidence if e.is_gap]

    coverage = hypothesis.coverage
    test_pass_rate = hypothesis.test_pass_rate

    # Corroboration: independent sources agreeing matters more than volume from
    # one source, so this counts distinct sources rather than distinct items.
    distinct_sources = {e.source for e in supporting}
    corroboration = min(len(distinct_sources) / 3.0, 1.0)

    source_reliability = (
        sum(e.trust_class.weight for e in supporting) / len(supporting)
        if supporting
        else 0.0
    )

    total_cited = len(supporting) + len(contradicting)
    contradiction_ratio = len(contradicting) / total_cited if total_cited else 0.0

    gap_ratio = len(gaps) / (len(usable) + len(gaps)) if (usable or gaps) else 0.0

    raw = (
        W_COVERAGE * coverage
        + W_CORROBORATION * corroboration
        + W_TEST_PASS * test_pass_rate
        + W_SOURCE_RELIABILITY * source_reliability
        - W_CONTRADICTION_PENALTY * contradiction_ratio
        - W_GAP_PENALTY * gap_ratio
    )

    # No supporting evidence at all means no confidence, whatever else scored.
    value = 0.0 if not supporting else _clamp(raw)

    return ConfidenceBreakdown(
        value=round(value, 4),
        coverage=round(coverage, 4),
        corroboration=round(corroboration, 4),
        test_pass_rate=round(test_pass_rate, 4),
        source_reliability=round(source_reliability, 4),
        contradiction_ratio=round(contradiction_ratio, 4),
        gap_ratio=round(gap_ratio, 4),
        supporting_count=len(supporting),
        contradicting_count=len(contradicting),
    )
