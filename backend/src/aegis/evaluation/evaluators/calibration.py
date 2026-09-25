"""Is the confidence number worth anything, and does the system know when to stop?

A confidence of 0.9 is a claim about the world: across many such claims, about
nine in ten should be right. Brier score and expected calibration error test
exactly that, which is why ``evidence.confidence`` derives its number from
measurable properties instead of asking a model how sure it feels - a
self-reported number has nothing to calibrate against.

Abstention is scored here too, in both directions:

* **Under-abstention** - concluded when the evidence could not support it. The
  dangerous direction: a confident wrong answer sends an on-call engineer down
  the wrong path at 3am.
* **Over-abstention** - abstained when the evidence was there. Not dangerous,
  but a system that always abstains is useless, and a benchmark that only
  punished the first direction would reward exactly that.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from aegis.core.errors import ValidationError
from aegis.evaluation.evaluators.base import (
    Determinism,
    EvaluatorResult,
    MetricValue,
    ratio,
    register_deterministic_metrics,
)
from aegis.evaluation.outcome import RunOutcome
from aegis.evaluation.schema import GroundTruth

ABSTENTION_METRICS = (
    "abstention_correct",
    "under_abstention",
    "over_abstention",
)
CALIBRATION_METRICS = (
    "brier_score",
    "expected_calibration_error",
    "max_calibration_error",
    "mean_confidence",
    "accuracy",
    "abstention_correctness_rate",
    "under_abstention_rate",
    "over_abstention_rate",
)
register_deterministic_metrics("abstention", ABSTENTION_METRICS)
register_deterministic_metrics("calibration", CALIBRATION_METRICS)

DEFAULT_BINS = 10


@dataclass(frozen=True, slots=True)
class ReliabilityBin:
    """One bucket of the reliability diagram the report plots."""

    lower: float
    upper: float
    count: int
    mean_confidence: float | None
    accuracy: float | None

    @property
    def gap(self) -> float | None:
        if self.mean_confidence is None or self.accuracy is None:
            return None
        return self.accuracy - self.mean_confidence

    def as_json(self) -> dict[str, float | int | None]:
        return {
            "lower": self.lower,
            "upper": self.upper,
            "count": self.count,
            "mean_confidence": self.mean_confidence,
            "accuracy": self.accuracy,
            "gap": self.gap,
        }


def brier_score(pairs: Sequence[tuple[float, bool]]) -> float | None:
    """Mean squared error between stated confidence and the outcome.

    0.0 is perfect, 0.25 is what you get by always saying 0.5, and 1.0 is
    confidently wrong every time.
    """
    if not pairs:
        return None
    return sum((p - (1.0 if correct else 0.0)) ** 2 for p, correct in pairs) / len(pairs)


def reliability_bins(
    pairs: Sequence[tuple[float, bool]], bins: int = DEFAULT_BINS
) -> list[ReliabilityBin]:
    """Bucket predictions by confidence. Equal-width bins, upper edge inclusive."""
    if bins <= 0:
        raise ValidationError("reliability bins must be positive", context={"bins": bins})
    buckets: list[list[tuple[float, bool]]] = [[] for _ in range(bins)]
    for confidence, correct in pairs:
        clamped = min(max(confidence, 0.0), 1.0)
        index = min(int(clamped * bins), bins - 1)
        buckets[index].append((clamped, correct))

    out: list[ReliabilityBin] = []
    for i, bucket in enumerate(buckets):
        lower, upper = i / bins, (i + 1) / bins
        if not bucket:
            out.append(ReliabilityBin(lower, upper, 0, None, None))
            continue
        out.append(
            ReliabilityBin(
                lower=lower,
                upper=upper,
                count=len(bucket),
                mean_confidence=sum(c for c, _ in bucket) / len(bucket),
                accuracy=sum(1 for _, ok in bucket if ok) / len(bucket),
            )
        )
    return out


def calibration_error(
    pairs: Sequence[tuple[float, bool]], bins: int = DEFAULT_BINS
) -> tuple[float | None, float | None]:
    """(expected, maximum) calibration error over the reliability bins."""
    if not pairs:
        return None, None
    total = len(pairs)
    expected = 0.0
    worst = 0.0
    for b in reliability_bins(pairs, bins):
        if b.count == 0 or b.gap is None:
            continue
        gap = abs(b.gap)
        expected += (b.count / total) * gap
        worst = max(worst, gap)
    return expected, worst


def outcome_is_correct(truth: GroundTruth, outcome: RunOutcome) -> bool:
    """The binary event a confidence number is a prediction about.

    Correct means: the root-cause service and category both match. For a
    false-positive scenario it means Aegis did not manufacture a root cause.
    """
    if truth.is_false_positive:
        # ``detected`` is tri-state: only an observed False counts as "did not
        # raise an incident". Unknown detection is not evidence of restraint,
        # and crediting it here would reward a run nobody could observe.
        return outcome.abstained or outcome.detected is False
    if outcome.abstained:
        return False
    if truth.root_cause_service and outcome.root_cause_service != truth.root_cause_service:
        return False
    return not (
        truth.root_cause_category
        and outcome.root_cause_category != truth.root_cause_category
    )


@dataclass(frozen=True, slots=True)
class AbstentionEvaluator:
    """Per-scenario. Did it stop when it should have, and not when it should not?"""

    name: str = "abstention"
    version: str = "1.0.0"
    determinism: Determinism = Determinism.DETERMINISTIC

    def evaluate(self, truth: GroundTruth, outcome: RunOutcome) -> EvaluatorResult:
        should = truth.should_abstain or truth.is_false_positive
        did = outcome.abstained
        correct = should == did

        notes: list[str] = []
        if should and not did:
            notes.append(
                "concluded where the evidence does not support a conclusion "
                f"(confidence {outcome.confidence:.2f})"
            )
        elif did and not should:
            notes.append("abstained although the scenario is decidable from the evidence")

        return EvaluatorResult(
            evaluator=self.name,
            version=self.version,
            determinism=self.determinism,
            metrics=(
                MetricValue(
                    "abstention_correct",
                    1.0 if correct else 0.0,
                    detail={"should_abstain": should, "abstained": did},
                ),
                MetricValue("under_abstention", 1.0 if (should and not did) else 0.0),
                MetricValue("over_abstention", 1.0 if (did and not should) else 0.0),
            ),
            notes=tuple(notes),
        )


@dataclass(frozen=True, slots=True)
class CalibrationEvaluator:
    """Run-level. Consumes every scenario at once; a single point cannot calibrate."""

    bins: int = DEFAULT_BINS
    name: str = "calibration"
    version: str = "1.0.0"
    determinism: Determinism = Determinism.DETERMINISTIC

    def evaluate(
        self, pairs: Sequence[tuple[GroundTruth, RunOutcome]]
    ) -> EvaluatorResult:
        scored: list[tuple[float, bool]] = []
        abstention_correct = 0
        under = 0
        over = 0
        considered = 0

        for truth, outcome in pairs:
            # Harness failures are not the model's confidence being wrong.
            if outcome.is_harness_failure:
                continue
            considered += 1
            should = truth.should_abstain or truth.is_false_positive
            if should == outcome.abstained:
                abstention_correct += 1
            elif should:
                under += 1
            else:
                over += 1
            # An abstention makes no probabilistic claim, so it contributes to
            # abstention scoring but not to the calibration curve.
            if not outcome.abstained:
                scored.append((outcome.confidence, outcome_is_correct(truth, outcome)))

        ece, mce = calibration_error(scored, self.bins)
        bins = reliability_bins(scored, self.bins)

        return EvaluatorResult(
            evaluator=self.name,
            version=self.version,
            determinism=self.determinism,
            metrics=(
                MetricValue("brier_score", brier_score(scored), unit="score",
                            sample_size=len(scored)),
                MetricValue("expected_calibration_error", ece, sample_size=len(scored)),
                MetricValue("max_calibration_error", mce, sample_size=len(scored)),
                MetricValue(
                    "mean_confidence",
                    (sum(c for c, _ in scored) / len(scored)) if scored else None,
                    sample_size=len(scored),
                ),
                MetricValue(
                    "accuracy",
                    (sum(1 for _, ok in scored if ok) / len(scored)) if scored else None,
                    sample_size=len(scored),
                ),
                MetricValue(
                    "abstention_correctness_rate",
                    ratio(abstention_correct, considered),
                    sample_size=considered,
                ),
                MetricValue("under_abstention_rate", ratio(under, considered),
                            sample_size=considered),
                MetricValue("over_abstention_rate", ratio(over, considered),
                            sample_size=considered),
            ),
            notes=(
                f"{len(bins)} reliability bins over {len(scored)} non-abstaining runs",
            ),
        )

    def bins_for(
        self, pairs: Sequence[tuple[GroundTruth, RunOutcome]]
    ) -> list[ReliabilityBin]:
        """Plot data for the report, from the same pairs the metrics used."""
        scored = [
            (o.confidence, outcome_is_correct(t, o))
            for t, o in pairs
            if not o.is_harness_failure and not o.abstained
        ]
        return reliability_bins(scored, self.bins)


__all__ = [
    "ABSTENTION_METRICS",
    "CALIBRATION_METRICS",
    "DEFAULT_BINS",
    "AbstentionEvaluator",
    "CalibrationEvaluator",
    "ReliabilityBin",
    "brier_score",
    "calibration_error",
    "outcome_is_correct",
    "reliability_bins",
]
