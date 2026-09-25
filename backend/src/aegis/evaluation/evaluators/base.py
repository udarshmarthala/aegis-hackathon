"""Evaluator contract, and the rule that keeps judging honest.

**Deterministic evaluation is the default.** Precision, recall, calibration,
policy correctness and cost are arithmetic over recorded facts; running a model
over them would add noise, cost and a second thing to debug, and would make the
benchmark unable to certify itself.

**LLM-as-judge is the documented exception**, for the handful of dimensions that
genuinely resist deterministic scoring - chiefly whether a root-cause *sentence*
means the same thing as the ground-truth sentence when the wording differs.

That policy is enforced here rather than written down and hoped for. Every
deterministic evaluator registers the metric names it owns at import time; a
judge that tries to score a registered name raises ``ValidationError`` when it
is constructed. A reviewer therefore cannot quietly move a scored dimension
from arithmetic to a model, because the code refuses.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Final, Protocol, runtime_checkable

from aegis.core.errors import ValidationError
from aegis.domain.enums import FailureClass


class Determinism(StrEnum):
    """How a number was produced. Always carried on the result."""

    DETERMINISTIC = "deterministic"
    JUDGED = "judged"


@dataclass(frozen=True, slots=True)
class MetricValue:
    """One measurement.

    ``value=None`` means *not applicable to this scenario* - a localization
    score for a correctly abstaining run, say - and is excluded from aggregates
    rather than counted as zero. Scoring "we correctly said we do not know" as a
    zero would train the benchmark to punish honesty.
    """

    name: str
    value: float | None
    unit: str = "ratio"
    sample_size: int = 1
    detail: dict[str, Any] = field(default_factory=dict)

    @property
    def applicable(self) -> bool:
        return self.value is not None

    def as_json(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "value": self.value,
            "unit": self.unit,
            "n": self.sample_size,
            **({"detail": self.detail} if self.detail else {}),
        }


@dataclass(frozen=True, slots=True)
class EvaluatorResult:
    """The typed output of one evaluator on one scenario (or one run)."""

    evaluator: str
    version: str
    determinism: Determinism
    metrics: tuple[MetricValue, ...] = ()
    failure_classes: tuple[FailureClass, ...] = ()
    notes: tuple[str, ...] = ()
    judge_model: str | None = None
    judge_prompt_version: str | None = None

    def __post_init__(self) -> None:
        if self.determinism is Determinism.JUDGED and not self.judge_model:
            # A judged number with no recorded model cannot be reproduced or
            # recalibrated later, so it is not admissible.
            raise ValidationError(
                f"judged result from {self.evaluator!r} must record its judge model",
                context={"evaluator": self.evaluator},
            )
        if self.determinism is Determinism.DETERMINISTIC and self.judge_model:
            raise ValidationError(
                f"deterministic result from {self.evaluator!r} must not name a judge model",
                context={"evaluator": self.evaluator},
            )

    @property
    def is_deterministic(self) -> bool:
        return self.determinism is Determinism.DETERMINISTIC

    def metric(self, name: str) -> MetricValue | None:
        for m in self.metrics:
            if m.name == name:
                return m
        return None

    def value(self, name: str) -> float | None:
        m = self.metric(name)
        return m.value if m else None

    def as_mapping(self) -> dict[str, float | None]:
        return {m.name: m.value for m in self.metrics}

    def as_json(self) -> dict[str, Any]:
        return {
            "evaluator": self.evaluator,
            "version": self.version,
            "determinism": self.determinism.value,
            "metrics": [m.as_json() for m in self.metrics],
            "failure_classes": [f.value for f in self.failure_classes],
            "notes": list(self.notes),
            **(
                {"judge_model": self.judge_model, "judge_prompt_version": self.judge_prompt_version}
                if self.determinism is Determinism.JUDGED
                else {}
            ),
        }


@runtime_checkable
class ScenarioEvaluator(Protocol):
    """Anything that scores one scenario outcome."""

    name: str
    version: str
    determinism: Determinism

    def evaluate(self, *args: Any, **kwargs: Any) -> EvaluatorResult:
        ...


# --------------------------------------------------------------------------- #
# the judge policy, as code                                                    #
# --------------------------------------------------------------------------- #

_DETERMINISTIC_METRICS: dict[str, str] = {}


def register_deterministic_metrics(evaluator: str, names: Iterable[str]) -> None:
    """Claim metric names for deterministic scoring.

    Re-registration by the same evaluator is fine (module reimport in tests);
    two different evaluators claiming one name is a bug worth failing on, since
    the aggregate would silently mix two definitions of the same number.
    """
    for name in names:
        owner = _DETERMINISTIC_METRICS.get(name)
        if owner is not None and owner != evaluator:
            raise ValidationError(
                f"metric {name!r} is already owned by the {owner!r} evaluator",
                context={"metric": name, "owner": owner, "claimant": evaluator},
            )
        _DETERMINISTIC_METRICS[name] = evaluator


def deterministic_metrics() -> Mapping[str, str]:
    """Read-only view of every deterministically scored metric name."""
    return dict(_DETERMINISTIC_METRICS)


def assert_judge_allowed(dimension: str) -> None:
    """Refuse to judge a dimension a deterministic evaluator already owns."""
    owner = _DETERMINISTIC_METRICS.get(dimension)
    if owner is not None:
        raise ValidationError(
            f"dimension {dimension!r} is scored deterministically by {owner!r}; "
            "an LLM judge may not be used where a deterministic evaluator exists",
            context={"dimension": dimension, "owner": owner},
        )


# --------------------------------------------------------------------------- #
# small shared arithmetic                                                      #
# --------------------------------------------------------------------------- #

EPSILON: Final = 1e-9


def precision_recall_f1(
    predicted: Iterable[str], expected: Iterable[str]
) -> tuple[float | None, float | None, float | None]:
    """Set-level precision, recall and F1.

    ``None`` where the quantity is undefined: precision with nothing predicted,
    recall with nothing expected. Reporting 0.0 for "there was nothing to get
    wrong" is the classic way a benchmark average becomes meaningless.
    """
    pred = {p for p in predicted if p}
    exp = {e for e in expected if e}
    true_positives = len(pred & exp)

    precision = None if not pred else true_positives / len(pred)
    recall = None if not exp else true_positives / len(exp)
    if precision is None or recall is None or precision + recall < EPSILON:
        f1 = None if (precision is None or recall is None) else 0.0
    else:
        f1 = 2 * precision * recall / (precision + recall)
    return precision, recall, f1


def ratio(numerator: int, denominator: int) -> float | None:
    """``None`` when there is nothing to take a ratio of."""
    if denominator <= 0:
        return None
    return numerator / denominator


def mean(values: Iterable[float]) -> float | None:
    data = list(values)
    if not data:
        return None
    return sum(data) / len(data)


__all__ = [
    "EPSILON",
    "Determinism",
    "EvaluatorResult",
    "MetricValue",
    "ScenarioEvaluator",
    "assert_judge_allowed",
    "deterministic_metrics",
    "mean",
    "precision_recall_f1",
    "ratio",
    "register_deterministic_metrics",
]
