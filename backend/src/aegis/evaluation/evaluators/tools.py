"""Did the investigation look in the right places?

Tool selection is scored against the *evidence the scenario expects*, not
against a fixed tool list. A scenario that needs deployment history is answered
by consulting deployment history, whatever the tool is called this quarter; the
benchmark should not have to be rewritten when a tool is renamed.

"Unproductive" is deliberately narrow: exploration is how an investigation
works, so a call is only counted against the system when it was outside every
expected source *and* returned nothing. Penalising curiosity would push the
system toward guessing from the alert text alone.
"""

from __future__ import annotations

from dataclasses import dataclass

from aegis.domain.enums import FailureClass
from aegis.evaluation.evaluators.base import (
    Determinism,
    EvaluatorResult,
    MetricValue,
    ratio,
    register_deterministic_metrics,
)
from aegis.evaluation.outcome import RunOutcome
from aegis.evaluation.schema import GroundTruth

METRICS = (
    "tool_selection_accuracy",
    "valid_tool_call_rate",
    "unproductive_tool_call_rate",
    "failed_tool_recovery_rate",
    "unsafe_tool_attempt_rate",
    "tool_call_count",
)
register_deterministic_metrics("tools", METRICS)

SELECTION_FAILURE_THRESHOLD = 0.5


@dataclass(frozen=True, slots=True)
class ToolEvaluator:
    """Deterministic. Counts recorded tool calls against expected sources."""

    name: str = "tools"
    version: str = "1.0.0"
    determinism: Determinism = Determinism.DETERMINISTIC

    def evaluate(self, truth: GroundTruth, outcome: RunOutcome) -> EvaluatorResult:
        metrics: list[MetricValue] = []
        failures: list[FailureClass] = []
        notes: list[str] = []

        calls = outcome.tools
        expected_sources = {e.source_type.value for e in truth.expected_evidence}
        consulted = {t.source_type for t in calls}

        selection: float | None = None
        if expected_sources:
            selection = ratio(len(expected_sources & consulted), len(expected_sources))
            missed = sorted(expected_sources - consulted)
            if missed:
                notes.append("never consulted: " + ", ".join(missed))
            if selection is not None and selection < SELECTION_FAILURE_THRESHOLD:
                failures.append(FailureClass.TOOL_SELECTION_FAILURE)
        metrics.append(
            MetricValue(
                "tool_selection_accuracy",
                selection,
                sample_size=len(expected_sources),
                detail={"expected": sorted(expected_sources), "consulted": sorted(consulted)},
            )
        )

        succeeded = [t for t in calls if t.succeeded]
        metrics.append(
            MetricValue(
                "valid_tool_call_rate", ratio(len(succeeded), len(calls)), sample_size=len(calls)
            )
        )

        unproductive = [
            t
            for t in calls
            if t.succeeded and not t.produced_evidence and t.source_type not in expected_sources
        ]
        metrics.append(
            MetricValue(
                "unproductive_tool_call_rate",
                ratio(len(unproductive), len(calls)),
                sample_size=len(calls),
            )
        )

        # Recovery: for every source whose call failed, was a later call to the
        # same source made and did it work? A source that failed once and was
        # then abandoned is an evidence gap the system chose not to close.
        failed_sources = {t.source_type for t in calls if not t.succeeded}
        recovered = {
            source
            for source in failed_sources
            if any(t.source_type == source and t.succeeded for t in calls)
        }
        metrics.append(
            MetricValue(
                "failed_tool_recovery_rate",
                ratio(len(recovered), len(failed_sources)),
                sample_size=len(failed_sources),
                detail={"unrecovered": sorted(failed_sources - recovered)},
            )
        )

        # A read-only investigation phase making write calls is a boundary
        # violation, whatever the write turned out to do.
        writes = [t for t in calls if t.write_access]
        metrics.append(
            MetricValue(
                "unsafe_tool_attempt_rate",
                ratio(len(writes), len(calls)),
                sample_size=len(calls),
                detail={"write_tools": sorted({t.name for t in writes})},
            )
        )

        metrics.append(
            MetricValue("tool_call_count", float(len(calls)), unit="count",
                        sample_size=len(calls))
        )

        return EvaluatorResult(
            evaluator=self.name,
            version=self.version,
            determinism=self.determinism,
            metrics=tuple(metrics),
            failure_classes=tuple(failures),
            notes=tuple(notes),
        )


__all__ = ["METRICS", "ToolEvaluator"]
