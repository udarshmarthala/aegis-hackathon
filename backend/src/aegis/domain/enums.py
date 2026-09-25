"""Closed vocabularies.

Every one of these is a closed set on purpose. A string where an enum belongs is
how an LLM eventually invents an action type, a risk tier or a severity that no
policy rule covers.
"""

from __future__ import annotations

from enum import IntEnum, StrEnum


class Severity(StrEnum):
    P1 = "P1"
    P2 = "P2"
    P3 = "P3"
    P4 = "P4"

    @property
    def rank(self) -> int:
        return {"P1": 1, "P2": 2, "P3": 3, "P4": 4}[self.value]


class IncidentState(StrEnum):
    RECEIVED = "RECEIVED"
    TRIAGING = "TRIAGING"
    INVESTIGATING = "INVESTIGATING"
    DIAGNOSING = "DIAGNOSING"
    DEBUGGING = "DEBUGGING"
    VERIFYING = "VERIFYING"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    REMEDIATING = "REMEDIATING"
    MONITORING = "MONITORING"
    RESOLVED = "RESOLVED"
    ESCALATED = "ESCALATED"
    BLOCKED = "BLOCKED"

    @property
    def is_terminal(self) -> bool:
        return self in (IncidentState.RESOLVED,)

    @property
    def is_active(self) -> bool:
        return self not in (IncidentState.RESOLVED, IncidentState.BLOCKED)


class ActionState(StrEnum):
    PROPOSED = "PROPOSED"
    POLICY_CHECKED = "POLICY_CHECKED"
    BLOCKED = "BLOCKED"
    HUMAN_REQUIRED = "HUMAN_REQUIRED"
    APPROVED = "APPROVED"
    EXECUTING = "EXECUTING"
    VERIFYING = "VERIFYING"
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    ROLLED_BACK = "ROLLED_BACK"
    EXPIRED = "EXPIRED"

    @property
    def is_terminal(self) -> bool:
        return self in (
            ActionState.SUCCESS, ActionState.FAILED,
            ActionState.ROLLED_BACK, ActionState.BLOCKED, ActionState.EXPIRED,
        )


class RiskTier(IntEnum):
    """PRD FR-12. Tier is a property of the action type, never of the argument."""

    OBSERVE = 0
    LOW = 1       # allowlisted, bounded, idempotent, reversible
    APPROVAL = 2  # human decision required
    HUMAN_ONLY = 3  # no autonomous code path exists at all


class PolicyEffect(StrEnum):
    ALLOW = "ALLOW"
    REQUIRE_HUMAN = "REQUIRE_HUMAN"
    BLOCK = "BLOCK"


class ActionType(StrEnum):
    """The complete set of write actions Aegis can even represent.

    Tier 3 members are declared so policy can name and block them explicitly,
    but ``execution.registry`` deliberately registers no executor for them -
    there is nothing to call even if a decision were somehow wrong.
    """

    # tier 1
    RESTART_INSTANCE = "restart_instance"
    RERUN_HEALTH_CHECK = "rerun_health_check"
    SCALE_UP_BOUNDED = "scale_up_bounded"
    CLEAR_CACHE_KEY = "clear_cache_key"
    # tier 2
    ROLLBACK_DEPLOYMENT = "rollback_deployment"
    SCALE_SERVICE = "scale_service"
    UPDATE_CONFIG = "update_config"
    PROMOTE_PATCH = "promote_patch"
    DRAIN_INSTANCE = "drain_instance"
    # tier 3 - representable, never executable
    DELETE_DATA = "delete_data"
    ROTATE_SECRET = "rotate_secret"  # noqa: S105 - action name, not a credential
    RUN_MIGRATION = "run_migration"
    MODIFY_SECURITY_POLICY = "modify_security_policy"


class TrustClass(StrEnum):
    """AIArchitecture section 13. Carried end to end, never inferred by a model."""

    TIER_A = "TIER_A"  # direct machine observation: metrics, spans, exit codes
    TIER_B = "TIER_B"  # structured metadata: deploy records, commit metadata
    TIER_C = "TIER_C"  # human-authored: runbooks, postmortems
    TIER_D = "TIER_D"  # untrusted free text: logs, commit messages, user input

    @property
    def weight(self) -> float:
        return {"TIER_A": 1.0, "TIER_B": 0.8, "TIER_C": 0.5, "TIER_D": 0.2}[self.value]


class SourceType(StrEnum):
    METRICS = "metrics"
    TRACES = "traces"
    LOGS = "logs"
    GRAPH = "graph"
    RUNTIME = "runtime"
    VCS = "vcs"
    DEPLOYMENT = "deployment"
    MEMORY = "memory"
    RUNBOOK = "runbook"
    SANDBOX = "sandbox"


class EvidenceType(StrEnum):
    METRIC_SERIES = "metric_series"
    METRIC_COMPARISON = "metric_comparison"
    TRACE_SPAN = "trace_span"
    TRACE_PATTERN = "trace_pattern"
    LOG_MATCH = "log_match"
    LOG_ANOMALY = "log_anomaly"
    TOPOLOGY_PATH = "topology_path"
    BLAST_RADIUS = "blast_radius"
    DEPLOYMENT_EVENT = "deployment_event"
    CODE_CHANGE = "code_change"
    CODE_SNIPPET = "code_snippet"
    INSTANCE_STATE = "instance_state"
    HISTORICAL_INCIDENT = "historical_incident"
    TEST_RESULT = "test_result"
    REPRODUCTION = "reproduction"
    EVIDENCE_GAP = "evidence_gap"


class EvidenceStatus(StrEnum):
    """``SOURCE_UNAVAILABLE`` is the whole point of this enum.

    "We looked and found nothing" and "we could not look" lead to opposite
    operational conclusions, so they can never share a representation (PRD 13).
    """

    UNVALIDATED = "UNVALIDATED"
    VALIDATED = "VALIDATED"
    REFUTED = "REFUTED"
    SOURCE_UNAVAILABLE = "SOURCE_UNAVAILABLE"


class HypothesisState(StrEnum):
    PROPOSED = "PROPOSED"
    TESTING = "TESTING"
    SUPPORTED = "SUPPORTED"
    REJECTED = "REJECTED"
    INCONCLUSIVE = "INCONCLUSIVE"


class MetricDirection(StrEnum):
    DECREASE = "decrease"
    INCREASE = "increase"
    STABLE = "stable"


class FailureClass(StrEnum):
    """ESD section 32. Environment failures never count as model-quality failures."""

    DETECTION_FAILURE = "DETECTION_FAILURE"
    LOCALIZATION_FAILURE = "LOCALIZATION_FAILURE"
    EVIDENCE_FAILURE = "EVIDENCE_FAILURE"
    GROUNDING_FAILURE = "GROUNDING_FAILURE"
    CAUSALITY_FAILURE = "CAUSALITY_FAILURE"
    TOOL_SELECTION_FAILURE = "TOOL_SELECTION_FAILURE"
    RETRIEVAL_FAILURE = "RETRIEVAL_FAILURE"
    CODE_LOCALIZATION_FAILURE = "CODE_LOCALIZATION_FAILURE"
    PATCH_FAILURE = "PATCH_FAILURE"
    VERIFICATION_FAILURE = "VERIFICATION_FAILURE"
    POLICY_FAILURE = "POLICY_FAILURE"
    EXECUTION_FAILURE = "EXECUTION_FAILURE"
    OBSERVABILITY_FAILURE = "OBSERVABILITY_FAILURE"
    TIMEOUT = "TIMEOUT"
    PROVIDER_FAILURE = "PROVIDER_FAILURE"
    ENVIRONMENT_FAILURE = "ENVIRONMENT_FAILURE"

    @property
    def is_harness_failure(self) -> bool:
        return self in (
            FailureClass.ENVIRONMENT_FAILURE,
            FailureClass.PROVIDER_FAILURE,
            FailureClass.OBSERVABILITY_FAILURE,
        )


class AgentRole(StrEnum):
    ORCHESTRATOR = "orchestrator"
    TRIAGE = "triage"
    EVIDENCE_INVESTIGATOR = "evidence_investigator"
    TOPOLOGY_ANALYST = "topology_analyst"
    CHANGE_ANALYST = "change_analyst"
    DIAGNOSIS = "diagnosis"
    DEBUGGER = "debugger"
    VERIFIER = "verifier"
    REMEDIATION_PLANNER = "remediation_planner"
    COMMUNICATION = "communication"


class ServiceHealth(StrEnum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    CRITICAL = "critical"
    UNKNOWN = "unknown"


class ClaimOutcome(StrEnum):
    """The result of testing one verification claim.

    ``UNAVAILABLE`` exists for the same reason ``EvidenceStatus`` has
    ``SOURCE_UNAVAILABLE``: a metric we could not read is not a metric that came
    back healthy. Collapsing the two would let a Prometheus outage read as a
    successful remediation, which is the single most dangerous failure mode a
    verification system can have.
    """

    PASS = "PASS"  # noqa: S105 - a verification outcome, not a credential
    FAIL = "FAIL"
    INCONCLUSIVE = "INCONCLUSIVE"   # measured, but the signal does not decide it
    UNAVAILABLE = "UNAVAILABLE"     # could not be measured at all


class VerificationVerdict(StrEnum):
    """The overall conclusion of a verification run.

    ``VERIFIED`` requires positive evidence that the incident condition is gone.
    It is never inferred from the absence of a failure signal (PRD 13).
    """

    VERIFIED = "VERIFIED"
    PARTIALLY_VERIFIED = "PARTIALLY_VERIFIED"
    INCONCLUSIVE = "INCONCLUSIVE"
    FAILED = "FAILED"
    REGRESSION_DETECTED = "REGRESSION_DETECTED"

    @property
    def is_success(self) -> bool:
        """Only a full verification counts as success.

        Partial verification deliberately does not: it means some claims could
        not be confirmed, and treating that as a pass is how an unverified
        change reaches production wearing a green badge.
        """
        return self is VerificationVerdict.VERIFIED

    @property
    def requires_rollback(self) -> bool:
        return self in (
            VerificationVerdict.FAILED,
            VerificationVerdict.REGRESSION_DETECTED,
        )


class VerificationTestKind(StrEnum):
    """How a claim was tested. Deterministic kinds are preferred everywhere."""

    METRIC_THRESHOLD = "metric_threshold"
    METRIC_DELTA = "metric_delta"
    PROTECTED_METRIC = "protected_metric"
    HEALTH_PROBE = "health_probe"
    INSTANCE_READY = "instance_ready"
    REPRODUCTION = "reproduction"
    REGRESSION_SUITE = "regression_suite"
    TRACE_ERROR_RATE = "trace_error_rate"
    LOG_PATTERN_ABSENT = "log_pattern_absent"
    DEPENDENCY_HEALTH = "dependency_health"
