"""Evaluation: the ground-truth benchmark Aegis is measured against.

The contract of this package in one paragraph: ``schema`` defines scenarios and
the redacted ``ScenarioInput`` that is the *only* thing a system under test ever
receives; ``outcome`` defines what that system produced; ``evaluators`` score
one against the other, deterministically wherever the ground truth allows;
``harness`` runs the suite, separates harness failures from model-quality
failures and persists to Postgres; ``ablations`` names the configurations that
test whether each architectural component earns its place; ``report`` renders
the result and compares it against a baseline, with the unsafe-scenario list
always present.

Ground truth never reaches the system under test. That is enforced in
``schema.assert_sealed`` and re-checked by the benchmark's own tests.
"""

from aegis.evaluation.ablations import ABLATIONS, AblationConfig, ablation_names, get_ablation
from aegis.evaluation.evaluators import (
    AbstentionEvaluator,
    CalibrationEvaluator,
    CostEvaluator,
    CostModel,
    Determinism,
    EvaluatorResult,
    EvidenceEvaluator,
    JudgeEvaluator,
    LocalizationEvaluator,
    MetricValue,
    RemediationEvaluator,
    SafetyEvaluator,
    ToolEvaluator,
    deterministic_metrics,
)
from aegis.evaluation.harness import (
    BenchmarkHarness,
    HarnessConfig,
    NullEnvironment,
    PassCriteria,
    ScenarioEnvironment,
    SystemUnderTest,
)
from aegis.evaluation.outcome import (
    CostObservation,
    HarnessFailure,
    ObservedAction,
    ObservedEvidence,
    ObservedRemediation,
    ObservedTool,
    ObservedVerification,
    RunOutcome,
)
from aegis.evaluation.report import (
    Comparison,
    compare,
    compare_ablation,
    to_json,
    to_markdown,
    write_json,
    write_markdown,
)
from aegis.evaluation.results import BenchmarkReport, MetricAggregate, ScenarioResult
from aegis.evaluation.schema import (
    SCHEMA_VERSION,
    AlertSpec,
    ExpectedEvidence,
    FaultInjection,
    FaultMode,
    GroundTruth,
    GroundTruthLeak,
    Scenario,
    ScenarioCategory,
    ScenarioInput,
    ScenarioInvalid,
    Workload,
    load_scenario_file,
    load_scenarios,
)

__all__ = [
    "ABLATIONS",
    "SCHEMA_VERSION",
    "AblationConfig",
    "AbstentionEvaluator",
    "AlertSpec",
    "BenchmarkHarness",
    "BenchmarkReport",
    "CalibrationEvaluator",
    "Comparison",
    "CostEvaluator",
    "CostModel",
    "CostObservation",
    "Determinism",
    "EvaluatorResult",
    "EvidenceEvaluator",
    "ExpectedEvidence",
    "FaultInjection",
    "FaultMode",
    "GroundTruth",
    "GroundTruthLeak",
    "HarnessConfig",
    "HarnessFailure",
    "JudgeEvaluator",
    "LocalizationEvaluator",
    "MetricAggregate",
    "MetricValue",
    "NullEnvironment",
    "ObservedAction",
    "ObservedEvidence",
    "ObservedRemediation",
    "ObservedTool",
    "ObservedVerification",
    "PassCriteria",
    "RemediationEvaluator",
    "RunOutcome",
    "SafetyEvaluator",
    "Scenario",
    "ScenarioCategory",
    "ScenarioEnvironment",
    "ScenarioInput",
    "ScenarioInvalid",
    "ScenarioResult",
    "SystemUnderTest",
    "ToolEvaluator",
    "Workload",
    "ablation_names",
    "compare",
    "compare_ablation",
    "deterministic_metrics",
    "get_ablation",
    "load_scenario_file",
    "load_scenarios",
    "to_json",
    "to_markdown",
    "write_json",
    "write_markdown",
]
