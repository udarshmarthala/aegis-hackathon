"""Evaluators.

Deterministic ones own their metric names (registered at import); the judge is
the single, fenced exception. See ``base`` for the enforcement.
"""

from aegis.evaluation.evaluators.base import (
    Determinism,
    EvaluatorResult,
    MetricValue,
    assert_judge_allowed,
    deterministic_metrics,
    precision_recall_f1,
    register_deterministic_metrics,
)
from aegis.evaluation.evaluators.calibration import (
    AbstentionEvaluator,
    CalibrationEvaluator,
    ReliabilityBin,
    brier_score,
    calibration_error,
    outcome_is_correct,
    reliability_bins,
)
from aegis.evaluation.evaluators.cost import CostEvaluator, CostModel
from aegis.evaluation.evaluators.evidence import EvidenceEvaluator, validate_outcome
from aegis.evaluation.evaluators.judge import JudgeClient, JudgeEvaluator
from aegis.evaluation.evaluators.localization import LocalizationEvaluator
from aegis.evaluation.evaluators.remediation import RemediationEvaluator
from aegis.evaluation.evaluators.safety import SafetyEvaluator, is_unsafe_autonomy
from aegis.evaluation.evaluators.tools import ToolEvaluator

__all__ = [
    "AbstentionEvaluator",
    "CalibrationEvaluator",
    "CostEvaluator",
    "CostModel",
    "Determinism",
    "EvaluatorResult",
    "EvidenceEvaluator",
    "JudgeClient",
    "JudgeEvaluator",
    "LocalizationEvaluator",
    "MetricValue",
    "ReliabilityBin",
    "RemediationEvaluator",
    "SafetyEvaluator",
    "ToolEvaluator",
    "assert_judge_allowed",
    "brier_score",
    "calibration_error",
    "deterministic_metrics",
    "is_unsafe_autonomy",
    "outcome_is_correct",
    "precision_recall_f1",
    "register_deterministic_metrics",
    "reliability_bins",
    "validate_outcome",
]
