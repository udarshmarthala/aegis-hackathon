"""Did Aegis point at the right part of the system?

Three separable questions, scored separately because they fail separately:

* **Blast radius** - which services were affected (set precision/recall/F1).
* **Origin** - which single service caused it (exact match).
* **Causality** - the dependency chain from origin to symptom, in order.

A system can get the blast radius perfect and the origin wrong; that is the
ordinary failure of correlation-based triage, and collapsing these into one
"localization score" would hide it.
"""

from __future__ import annotations

from dataclasses import dataclass

from aegis.domain.enums import FailureClass
from aegis.evaluation.evaluators.base import (
    Determinism,
    EvaluatorResult,
    MetricValue,
    precision_recall_f1,
    ratio,
    register_deterministic_metrics,
)
from aegis.evaluation.outcome import RunOutcome
from aegis.evaluation.schema import GroundTruth

METRICS = (
    "affected_service_precision",
    "affected_service_recall",
    "affected_service_f1",
    "root_cause_service_accuracy",
    "causal_path_exact_accuracy",
    "causal_path_edge_recall",
    "blast_radius_recall",
)
register_deterministic_metrics("localization", METRICS)

# Below this the localization is wrong enough to classify the scenario as a
# localization failure rather than a near miss.
F1_FAILURE_THRESHOLD = 0.5


def _edges(path: tuple[str, ...]) -> set[tuple[str, str]]:
    return {(path[i], path[i + 1]) for i in range(len(path) - 1)}


@dataclass(frozen=True, slots=True)
class LocalizationEvaluator:
    """Deterministic. Set arithmetic over service names, no model involved."""

    name: str = "localization"
    version: str = "1.0.0"
    determinism: Determinism = Determinism.DETERMINISTIC

    def evaluate(self, truth: GroundTruth, outcome: RunOutcome) -> EvaluatorResult:
        metrics: list[MetricValue] = []
        failures: list[FailureClass] = []
        notes: list[str] = []

        # A correct abstention has nothing to localize. Scoring it zero would
        # make honesty cost more than a confident wrong answer.
        scoreable = not (truth.should_abstain and outcome.abstained)
        if not scoreable:
            notes.append("correct abstention: localization not applicable")

        if truth.is_false_positive:
            notes.append("false-positive alert: no true affected services")

        precision, recall, f1 = (
            precision_recall_f1(outcome.affected_services, truth.affected_services)
            if scoreable
            else (None, None, None)
        )
        metrics.append(
            MetricValue(
                "affected_service_precision",
                precision,
                detail={
                    "predicted": sorted(set(outcome.affected_services)),
                    "expected": sorted(set(truth.affected_services)),
                },
            )
        )
        metrics.append(MetricValue("affected_service_recall", recall))
        metrics.append(MetricValue("affected_service_f1", f1))

        root_accuracy: float | None = None
        if scoreable and truth.root_cause_service is not None:
            root_accuracy = (
                1.0 if outcome.root_cause_service == truth.root_cause_service else 0.0
            )
            if root_accuracy == 0.0:
                failures.append(FailureClass.LOCALIZATION_FAILURE)
                notes.append(
                    f"root cause: expected {truth.root_cause_service!r}, "
                    f"got {outcome.root_cause_service!r}"
                )
        metrics.append(
            MetricValue(
                "root_cause_service_accuracy",
                root_accuracy,
                detail={
                    "expected": truth.root_cause_service,
                    "predicted": outcome.root_cause_service,
                },
            )
        )

        exact: float | None = None
        edge_recall: float | None = None
        if scoreable and truth.causal_dependency:
            exact = 1.0 if tuple(outcome.causal_path) == truth.causal_dependency else 0.0
            expected_edges = _edges(truth.causal_dependency)
            found = expected_edges & _edges(tuple(outcome.causal_path))
            edge_recall = ratio(len(found), len(expected_edges))
            if exact == 0.0 and (edge_recall or 0.0) < 1.0:
                failures.append(FailureClass.CAUSALITY_FAILURE)
                notes.append(
                    "causal path: expected "
                    + " -> ".join(truth.causal_dependency)
                    + "; got "
                    + (" -> ".join(outcome.causal_path) or "<none>")
                )
        metrics.append(MetricValue("causal_path_exact_accuracy", exact))
        metrics.append(MetricValue("causal_path_edge_recall", edge_recall))

        blast_recall: float | None = None
        if scoreable and truth.expected_blast_radius:
            _, blast_recall, _ = precision_recall_f1(
                outcome.affected_services, truth.expected_blast_radius
            )
        metrics.append(MetricValue("blast_radius_recall", blast_recall))

        if (
            scoreable
            and f1 is not None
            and f1 < F1_FAILURE_THRESHOLD
            and FailureClass.LOCALIZATION_FAILURE not in failures
        ):
            failures.append(FailureClass.LOCALIZATION_FAILURE)
            notes.append(f"affected-service F1 {f1:.2f} below {F1_FAILURE_THRESHOLD}")

        return EvaluatorResult(
            evaluator=self.name,
            version=self.version,
            determinism=self.determinism,
            metrics=tuple(metrics),
            failure_classes=tuple(failures),
            notes=tuple(notes),
        )


__all__ = ["METRICS", "LocalizationEvaluator"]
