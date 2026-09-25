"""Typed results: one scenario, and one run.

Aggregation rules live here because they are where a benchmark is most easily
made to lie:

* **Harness failures are excluded from quality aggregates.** A Prometheus outage
  is not a localization failure. They are counted and reported separately, and
  a run with many of them is flagged rather than averaged over (ESD section 32).
* **Inapplicable metrics are excluded, not zeroed.** A correct abstention has no
  localization score. Counting it as 0.0 would make abstaining - the safe
  behaviour - look worse than a confident wrong answer.
* **Aggregates carry their sample size and a confidence interval.** "0.81 over
  4 scenarios" and "0.81 over 120" are different claims, and PRD 9.3 requires
  the distinction to survive into the report.
"""

from __future__ import annotations

import random
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Final

from aegis.domain.enums import FailureClass
from aegis.evaluation.evaluators.base import Determinism, EvaluatorResult
from aegis.evaluation.evaluators.calibration import ReliabilityBin
from aegis.evaluation.outcome import RunOutcome

# Bounded on purpose: a resample loop is the kind of thing that quietly turns a
# 200-scenario report into a minute of CPU.
BOOTSTRAP_RESAMPLES: Final = 200
BOOTSTRAP_SEED: Final = 20260920
MIN_BOOTSTRAP_SAMPLES: Final = 5


@dataclass(frozen=True, slots=True)
class MetricAggregate:
    """One metric across many scenarios."""

    name: str
    mean: float | None
    sample_size: int
    ci_low: float | None = None
    ci_high: float | None = None
    determinism: Determinism = Determinism.DETERMINISTIC
    unit: str = "ratio"

    def as_json(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "mean": self.mean,
            "n": self.sample_size,
            "ci_low": self.ci_low,
            "ci_high": self.ci_high,
            "determinism": self.determinism.value,
            "unit": self.unit,
        }


def bootstrap_ci(
    values: Sequence[float], *, resamples: int = BOOTSTRAP_RESAMPLES, seed: int = BOOTSTRAP_SEED
) -> tuple[float | None, float | None]:
    """Percentile bootstrap for the mean, seeded so a report is reproducible.

    Below ``MIN_BOOTSTRAP_SAMPLES`` no interval is reported: resampling four
    numbers produces a confident-looking interval that means nothing.
    """
    if len(values) < MIN_BOOTSTRAP_SAMPLES:
        return None, None
    rng = random.Random(seed)  # noqa: S311 - statistics, not security
    n = len(values)
    means: list[float] = []
    for _ in range(resamples):
        sample = [values[rng.randrange(n)] for _ in range(n)]
        means.append(sum(sample) / n)
    means.sort()
    low = means[int(0.025 * (resamples - 1))]
    high = means[int(0.975 * (resamples - 1))]
    return round(low, 6), round(high, 6)


@dataclass(frozen=True, slots=True)
class ScenarioResult:
    """Everything known about one scenario after it ran and was scored."""

    scenario_id: str
    scenario_hash: str
    category: str
    workload: str
    severity: str
    difficulty: str
    passed: bool
    failure_class: FailureClass | None
    evaluations: tuple[EvaluatorResult, ...]
    outcome: RunOutcome
    duration_ms: int
    started_at: datetime
    finished_at: datetime
    ablation: str = "full"
    notes: tuple[str, ...] = ()

    @property
    def is_harness_failure(self) -> bool:
        return bool(self.failure_class and self.failure_class.is_harness_failure)

    @property
    def unsafe(self) -> bool:
        """Did any safety metric fire? Reported by name, never aggregated away."""
        for evaluation in self.evaluations:
            value = evaluation.value("unsafe_incident")
            if value is not None and value > 0:
                return True
        return False

    @property
    def safety_notes(self) -> tuple[str, ...]:
        for evaluation in self.evaluations:
            if evaluation.evaluator == "safety" and evaluation.notes:
                return evaluation.notes
        return ()

    def metrics(self) -> dict[str, float | None]:
        """Flattened metric view. Later evaluators never shadow earlier names -
        ``register_deterministic_metrics`` guarantees the names are disjoint."""
        flat: dict[str, float | None] = {}
        for evaluation in self.evaluations:
            flat.update(evaluation.as_mapping())
        return flat

    def determinism_of(self, metric: str) -> Determinism:
        for evaluation in self.evaluations:
            if evaluation.metric(metric) is not None:
                return evaluation.determinism
        return Determinism.DETERMINISTIC

    def as_json(self) -> dict[str, Any]:
        return {
            "scenario_id": self.scenario_id,
            "scenario_hash": self.scenario_hash,
            "category": self.category,
            "workload": self.workload,
            "severity": self.severity,
            "difficulty": self.difficulty,
            "ablation": self.ablation,
            "passed": self.passed,
            "unsafe": self.unsafe,
            "failure_class": self.failure_class.value if self.failure_class else None,
            "harness_failure": self.is_harness_failure,
            "duration_ms": self.duration_ms,
            "started_at": self.started_at.isoformat(),
            "finished_at": self.finished_at.isoformat(),
            "metrics": self.metrics(),
            "evaluations": [e.as_json() for e in self.evaluations],
            "predicted": self.outcome.as_json(),
            "notes": list(self.notes),
        }


@dataclass(frozen=True, slots=True)
class CategoryBreakdown:
    """Per-category summary. A regression hidden inside one category is the
    failure mode PRD 9.3 exists to catch."""

    category: str
    total: int
    passed: int
    harness_failures: int
    unsafe: int

    @property
    def scored(self) -> int:
        return self.total - self.harness_failures

    @property
    def pass_rate(self) -> float | None:
        if self.scored <= 0:
            return None
        return self.passed / self.scored

    def as_json(self) -> dict[str, Any]:
        return {
            "category": self.category,
            "total": self.total,
            "scored": self.scored,
            "passed": self.passed,
            "pass_rate": self.pass_rate,
            "harness_failures": self.harness_failures,
            "unsafe": self.unsafe,
        }


@dataclass(frozen=True, slots=True)
class BenchmarkReport:
    """The whole run. Immutable once the harness finishes."""

    run_id: str
    suite: str
    ablation: str
    agent_version: str
    prompt_version: str
    policy_version: str
    model: str
    started_at: datetime
    finished_at: datetime
    results: tuple[ScenarioResult, ...]
    run_level: tuple[EvaluatorResult, ...] = ()
    reliability: tuple[ReliabilityBin, ...] = ()
    scenario_count: int = 0
    skipped: tuple[str, ...] = ()
    # Scenarios that were never run because the environment could not inject
    # their fault. Distinct from ``skipped`` (already recorded by an earlier
    # pass of the same run): those have results, these have none at all, and
    # collapsing the two would turn "not measured" into "measured elsewhere".
    uninjectable: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)

    # ---- partitions ---------------------------------------------------------

    @property
    def harness_failures(self) -> tuple[ScenarioResult, ...]:
        return tuple(r for r in self.results if r.is_harness_failure)

    @property
    def scored(self) -> tuple[ScenarioResult, ...]:
        """Results that say something about model quality."""
        return tuple(r for r in self.results if not r.is_harness_failure)

    @property
    def unsafe_scenarios(self) -> tuple[ScenarioResult, ...]:
        return tuple(r for r in self.scored if r.unsafe)

    @property
    def pass_rate(self) -> float | None:
        scored = self.scored
        if not scored:
            return None
        return sum(1 for r in scored if r.passed) / len(scored)

    # ---- aggregates ---------------------------------------------------------

    def metric_names(self) -> tuple[str, ...]:
        names: set[str] = set()
        for result in self.scored:
            names.update(result.metrics())
        for evaluation in self.run_level:
            names.update(evaluation.as_mapping())
        return tuple(sorted(names))

    def aggregate(self, name: str) -> MetricAggregate:
        """Mean over applicable, non-harness scenarios, with a bootstrap CI."""
        for evaluation in self.run_level:
            metric = evaluation.metric(name)
            if metric is not None:
                # Run-level metrics (calibration) are already computed over the
                # whole run; re-averaging them would be wrong.
                return MetricAggregate(
                    name=name,
                    mean=metric.value,
                    sample_size=metric.sample_size,
                    determinism=evaluation.determinism,
                    unit=metric.unit,
                )

        values: list[float] = []
        determinism = Determinism.DETERMINISTIC
        unit = "ratio"
        for result in self.scored:
            for evaluation in result.evaluations:
                metric = evaluation.metric(name)
                if metric is None:
                    continue
                determinism = evaluation.determinism
                unit = metric.unit
                if metric.value is not None:
                    values.append(metric.value)
        if not values:
            return MetricAggregate(name, None, 0, determinism=determinism, unit=unit)
        low, high = bootstrap_ci(values)
        return MetricAggregate(
            name=name,
            mean=round(sum(values) / len(values), 6),
            sample_size=len(values),
            ci_low=low,
            ci_high=high,
            determinism=determinism,
            unit=unit,
        )

    def aggregates(self) -> dict[str, MetricAggregate]:
        return {name: self.aggregate(name) for name in self.metric_names()}

    def by_category(self) -> tuple[CategoryBreakdown, ...]:
        buckets: dict[str, list[ScenarioResult]] = {}
        for result in self.results:
            buckets.setdefault(result.category, []).append(result)
        out: list[CategoryBreakdown] = []
        for category, rows in sorted(buckets.items()):
            harness = sum(1 for r in rows if r.is_harness_failure)
            out.append(
                CategoryBreakdown(
                    category=category,
                    total=len(rows),
                    passed=sum(1 for r in rows if r.passed and not r.is_harness_failure),
                    harness_failures=harness,
                    unsafe=sum(1 for r in rows if r.unsafe),
                )
            )
        return tuple(out)

    def failure_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for result in self.results:
            if result.failure_class is None:
                continue
            counts[result.failure_class.value] = counts.get(result.failure_class.value, 0) + 1
        return dict(sorted(counts.items()))

    def total_cost_usd(self) -> float:
        return round(sum(r.outcome.cost.total_cost_usd for r in self.results), 6)

    def total_tokens(self) -> int:
        return sum(r.outcome.cost.total_tokens for r in self.results)

    def summary(self) -> dict[str, Any]:
        """The compact record persisted on ``evaluation_runs.summary``."""
        return {
            "suite": self.suite,
            "ablation": self.ablation,
            "scenarios": len(self.results),
            "scored": len(self.scored),
            "passed": sum(1 for r in self.scored if r.passed),
            "pass_rate": self.pass_rate,
            "harness_failures": len(self.harness_failures),
            "uninjectable": len(self.uninjectable),
            "uninjectable_scenarios": list(self.uninjectable),
            "unsafe_scenarios": [r.scenario_id for r in self.unsafe_scenarios],
            "failure_classes": self.failure_counts(),
            "total_cost_usd": self.total_cost_usd(),
            "total_tokens": self.total_tokens(),
            "duration_s": round(
                (self.finished_at - self.started_at).total_seconds(), 3
            ),
        }


def flatten_metrics(results: Iterable[ScenarioResult]) -> Mapping[str, list[float]]:
    """Applicable values per metric across scenarios. Used by the comparators."""
    out: dict[str, list[float]] = {}
    for result in results:
        for name, value in result.metrics().items():
            if value is not None:
                out.setdefault(name, []).append(value)
    return out


__all__ = [
    "BOOTSTRAP_RESAMPLES",
    "BOOTSTRAP_SEED",
    "MIN_BOOTSTRAP_SAMPLES",
    "BenchmarkReport",
    "CategoryBreakdown",
    "MetricAggregate",
    "ScenarioResult",
    "bootstrap_ci",
    "flatten_metrics",
]
