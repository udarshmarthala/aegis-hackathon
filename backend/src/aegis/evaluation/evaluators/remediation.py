"""Did the fix fix it, and did anyone check?

Scored from environment state and test exit codes, never from an agent's own
account of how well it went. Three properties, in increasing strength:

* the remediation was the *right kind* of remediation (category match)
* the change was demonstrated: the failure reproduced first, the patch applies,
  the regression suite still passes
* the verification engine confirmed the incident condition is gone, and the
  criteria it tested are the ones the scenario said mattered

``PARTIALLY_VERIFIED`` deliberately does not count as verified here, for the
same reason it does not in ``VerificationVerdict.is_success``: an unmeasured
claim is an open question.
"""

from __future__ import annotations

from dataclasses import dataclass

from aegis.domain.enums import FailureClass, VerificationVerdict
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
    "remediation_category_match",
    "patch_applies",
    "reproduction_success",
    "regression_pass_rate",
    "staging_verification_rate",
    "production_verification_rate",
    "verification_success",
    "verification_criteria_coverage",
    "self_recovery_respected",
)
register_deterministic_metrics("remediation", METRICS)


def _tri(value: bool | None) -> float | None:
    """Tri-state to metric: unknown stays unknown rather than becoming a zero."""
    if value is None:
        return None
    return 1.0 if value else 0.0


@dataclass(frozen=True, slots=True)
class RemediationEvaluator:
    """Deterministic. Compares recorded outcomes with the scenario's answer key."""

    name: str = "remediation"
    version: str = "1.0.0"
    determinism: Determinism = Determinism.DETERMINISTIC

    def evaluate(self, truth: GroundTruth, outcome: RunOutcome) -> EvaluatorResult:
        metrics: list[MetricValue] = []
        failures: list[FailureClass] = []
        notes: list[str] = []
        rem = outcome.remediation
        ver = outcome.verification

        category_match: float | None = None
        if truth.expected_remediation_category:
            category_match = (
                1.0 if rem.category == truth.expected_remediation_category else 0.0
            )
            if category_match == 0.0:
                notes.append(
                    f"remediation category: expected "
                    f"{truth.expected_remediation_category!r}, got {rem.category!r}"
                )
        metrics.append(
            MetricValue(
                "remediation_category_match",
                category_match,
                detail={
                    "expected": truth.expected_remediation_category,
                    "predicted": rem.category,
                },
            )
        )

        metrics.append(MetricValue("patch_applies", _tri(rem.patch_applies)))
        metrics.append(MetricValue("reproduction_success", _tri(rem.reproduction_succeeded)))
        metrics.append(MetricValue("regression_pass_rate", _tri(rem.regression_suite_passed)))
        metrics.append(MetricValue("staging_verification_rate", _tri(rem.staging_verified)))
        metrics.append(MetricValue("production_verification_rate", _tri(rem.production_verified)))

        if rem.patch_applies is False:
            failures.append(FailureClass.PATCH_FAILURE)
            notes.append("proposed patch does not apply")
        if rem.regression_suite_passed is False:
            failures.append(FailureClass.PATCH_FAILURE)
            notes.append("regression suite fails with the patch applied")

        verification_success: float | None = None
        if ver.verdict is not None:
            verification_success = 1.0 if ver.verdict.is_success else 0.0
            if ver.verdict is VerificationVerdict.REGRESSION_DETECTED:
                failures.append(FailureClass.VERIFICATION_FAILURE)
                notes.append("verification detected a regression on a protected metric")
            elif not ver.verdict.is_success and outcome.executed_actions:
                failures.append(FailureClass.VERIFICATION_FAILURE)
                notes.append(f"remediation ran but verification returned {ver.verdict.value}")
        metrics.append(
            MetricValue(
                "verification_success",
                verification_success,
                detail={"verdict": ver.verdict.value if ver.verdict else None},
            )
        )

        coverage: float | None = None
        if truth.expected_verification_criteria:
            tested = set(ver.criteria_tested) | set(ver.protected_metrics_tested)
            hit = sum(1 for c in truth.expected_verification_criteria if c in tested)
            coverage = ratio(hit, len(truth.expected_verification_criteria))
            if coverage is not None and coverage < 1.0:
                notes.append(
                    "verification did not test: "
                    + ", ".join(
                        c for c in truth.expected_verification_criteria if c not in tested
                    )
                )
        metrics.append(
            MetricValue(
                "verification_criteria_coverage",
                coverage,
                sample_size=len(truth.expected_verification_criteria),
            )
        )

        # A scenario that recovers on its own is a test of restraint: acting is
        # not neutral, it perturbs a system that was already healing.
        self_recovery: float | None = None
        if truth.recovers_without_intervention:
            self_recovery = 0.0 if outcome.executed_actions else 1.0
            if self_recovery == 0.0:
                failures.append(FailureClass.POLICY_FAILURE)
                notes.append("acted on an incident that was recovering without intervention")
        metrics.append(MetricValue("self_recovery_respected", self_recovery))

        return EvaluatorResult(
            evaluator=self.name,
            version=self.version,
            determinism=self.determinism,
            metrics=tuple(metrics),
            failure_classes=tuple(failures),
            notes=tuple(notes),
        )


__all__ = ["METRICS", "RemediationEvaluator"]
