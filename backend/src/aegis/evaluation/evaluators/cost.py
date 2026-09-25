"""What did the answer cost, and how long did an operator wait for it?

Quality per dollar and quality per minute are the numbers that decide whether a
change ships, so they are measured rather than estimated: tokens come from
recorded usage, latency is wall time, and prices come from an explicit, versioned
table. A model is never asked what it spent.

``time_to_first_useful_hypothesis`` is tracked separately from total duration
because they answer different operational questions. An engineer watching an
incident gets value the moment a plausible, evidence-backed hypothesis appears;
the run finishing ten minutes later matters much less.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from aegis.evaluation.evaluators.base import (
    Determinism,
    EvaluatorResult,
    MetricValue,
    register_deterministic_metrics,
)
from aegis.evaluation.outcome import CostObservation, RunOutcome
from aegis.evaluation.schema import GroundTruth

METRICS = (
    "llm_cost_usd",
    "execution_cost_usd",
    "total_cost_usd",
    "total_tokens",
    "llm_calls",
    "tool_calls",
    "investigation_seconds",
    "time_to_first_hypothesis_seconds",
    "time_to_diagnosis_seconds",
    "mttr_seconds",
    "within_latency_budget",
    "within_cost_budget",
)
register_deterministic_metrics("cost", METRICS)


@dataclass(frozen=True, slots=True)
class CostModel:
    """Prices in USD per million tokens, plus the budgets a run is held to.

    Versioned on purpose: a cost number is only comparable across runs when the
    price table that produced it is known, and provider prices change.
    """

    version: str = "2026-09"
    input_usd_per_mtok: float = 3.0
    output_usd_per_mtok: float = 15.0
    latency_budget_s: float = 600.0
    cost_budget_usd: float = 2.0

    def price(self, input_tokens: int, output_tokens: int) -> float:
        return round(
            (input_tokens / 1_000_000) * self.input_usd_per_mtok
            + (output_tokens / 1_000_000) * self.output_usd_per_mtok,
            6,
        )


def _seconds(ms: int | None) -> float | None:
    return None if ms is None else round(ms / 1000.0, 3)


@dataclass(frozen=True, slots=True)
class CostEvaluator:
    """Deterministic. Arithmetic over measured usage."""

    model: CostModel = field(default_factory=CostModel)
    name: str = "cost"
    version: str = "1.0.0"
    determinism: Determinism = Determinism.DETERMINISTIC

    def priced(self, cost: CostObservation) -> float:
        """LLM spend, taking the recorded figure when the provider gave one."""
        if cost.llm_cost_usd > 0:
            return cost.llm_cost_usd
        return self.model.price(cost.input_tokens, cost.output_tokens)

    def evaluate(self, truth: GroundTruth, outcome: RunOutcome) -> EvaluatorResult:
        del truth  # cost is a property of the run, not of the answer key
        c = outcome.cost
        llm_usd = self.priced(c)
        total = round(llm_usd + c.execution_cost_usd, 6)
        wall_s = _seconds(c.wall_ms) or 0.0

        return EvaluatorResult(
            evaluator=self.name,
            version=self.version,
            determinism=self.determinism,
            metrics=(
                MetricValue("llm_cost_usd", llm_usd, unit="usd"),
                MetricValue("execution_cost_usd", c.execution_cost_usd, unit="usd"),
                MetricValue("total_cost_usd", total, unit="usd"),
                MetricValue("total_tokens", float(c.total_tokens), unit="count"),
                MetricValue("llm_calls", float(c.llm_calls), unit="count"),
                MetricValue("tool_calls", float(c.tool_calls), unit="count"),
                MetricValue("investigation_seconds", wall_s, unit="seconds"),
                MetricValue(
                    "time_to_first_hypothesis_seconds",
                    _seconds(c.time_to_first_hypothesis_ms),
                    unit="seconds",
                ),
                MetricValue(
                    "time_to_diagnosis_seconds", _seconds(c.time_to_diagnosis_ms), unit="seconds"
                ),
                MetricValue("mttr_seconds", _seconds(c.mttr_ms), unit="seconds"),
                MetricValue(
                    "within_latency_budget",
                    1.0 if wall_s <= self.model.latency_budget_s else 0.0,
                    detail={"budget_s": self.model.latency_budget_s},
                ),
                MetricValue(
                    "within_cost_budget",
                    1.0 if total <= self.model.cost_budget_usd else 0.0,
                    detail={"budget_usd": self.model.cost_budget_usd,
                            "price_table": self.model.version},
                ),
            ),
        )


__all__ = ["METRICS", "CostEvaluator", "CostModel"]
