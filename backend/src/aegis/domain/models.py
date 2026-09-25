"""Domain aggregates.

Pure pydantic. No I/O, no database, no HTTP. Everything here is constructible in
a unit test, which is what makes the safety rules cheap to verify exhaustively.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from aegis.domain.enums import (
    ActionState,
    ActionType,
    AgentRole,
    EvidenceStatus,
    EvidenceType,
    HypothesisState,
    IncidentState,
    MetricDirection,
    PolicyEffect,
    RiskTier,
    ServiceHealth,
    Severity,
    SourceType,
    TrustClass,
)

Confidence = Annotated[float, Field(ge=0.0, le=1.0)]


class Frozen(BaseModel):
    """Base for immutable records. Evidence and decisions are never edited."""

    model_config = ConfigDict(frozen=True, extra="forbid")


class Mutable(BaseModel):
    model_config = ConfigDict(extra="forbid")


# --------------------------------------------------------------------------- #
# untrusted content                                                            #
# --------------------------------------------------------------------------- #


class UntrustedText(Frozen):
    """A Tier-D payload that must never be read as instruction.

    Log lines, commit messages, alert annotations and ticket bodies are all
    attacker-influenceable. Wrapping them in a distinct type means a developer
    cannot accidentally concatenate one into a prompt as plain text: rendering
    goes through ``as_prompt_block`` which emits a delimited, labelled envelope.
    """

    text: str
    origin: str  # "log" | "commit_message" | "alert_annotation" | ...
    evidence_id: str | None = None

    MAX_RENDER: int = 4000

    def as_prompt_block(self) -> str:
        body = self.text[: self.MAX_RENDER]
        if len(self.text) > self.MAX_RENDER:
            body += f"\n...[truncated {len(self.text) - self.MAX_RENDER} chars]"
        # A closing-tag injection cannot break out of the envelope.
        body = body.replace("</untrusted>", "<_/untrusted>")
        attrs = f'origin="{self.origin}"'
        if self.evidence_id:
            attrs += f' id="{self.evidence_id}"'
        return f"<untrusted {attrs}>\n{body}\n</untrusted>"

    def __str__(self) -> str:  # defensive: never interpolate raw
        return self.as_prompt_block()


# --------------------------------------------------------------------------- #
# references                                                                   #
# --------------------------------------------------------------------------- #


class ServiceRef(Frozen):
    """Canonical service identity.

    ``service_id`` is stable across restarts, rescheduling and scaling. A pod
    churning must never create a second node in the graph (ESD section 7).
    """

    service_id: str  # "{environment}:{workload}:{service}"
    name: str
    environment: str
    workload: str = "default"

    @field_validator("service_id")
    @classmethod
    def _canonical(cls, v: str) -> str:
        if v.count(":") != 2:
            raise ValueError("service_id must be '{environment}:{workload}:{service}'")
        return v

    @classmethod
    def build(cls, environment: str, workload: str, name: str) -> ServiceRef:
        return cls(
            service_id=f"{environment}:{workload}:{name}",
            name=name,
            environment=environment,
            workload=workload,
        )


class ResourceRef(Frozen):
    """The target of a write action."""

    resource_type: Literal["service", "instance", "deployment", "config", "cache"]
    resource_id: str
    environment: str
    service_id: str | None = None

    @property
    def lease_key(self) -> tuple[str, str]:
        return (self.resource_type, self.resource_id)


class EvidenceRef(Frozen):
    """A citation. Claims carry these; validation resolves them."""

    evidence_id: str
    note: str | None = None


# --------------------------------------------------------------------------- #
# evidence                                                                     #
# --------------------------------------------------------------------------- #


class EvidenceItem(Frozen):
    """One independently inspectable observation (ESD section 8).

    Immutable once written. ``provenance_uri`` must contain enough to re-run the
    exact query that produced it - an operator who cannot reproduce a citation
    has no reason to trust it.
    """

    id: str
    incident_id: str
    source: str
    source_type: SourceType
    evidence_type: EvidenceType
    retrieved_at: datetime
    observed_at: datetime | None = None
    resource_id: str | None = None
    summary: str = ""
    structured_value: dict[str, Any] = Field(default_factory=dict)
    content: UntrustedText | str | None = None
    provenance_uri: str = ""
    trust_class: TrustClass = TrustClass.TIER_D
    status: EvidenceStatus = EvidenceStatus.UNVALIDATED
    content_hash: str = ""

    @property
    def is_usable(self) -> bool:
        """Only usable evidence may support a claim."""
        return self.status in (EvidenceStatus.VALIDATED, EvidenceStatus.UNVALIDATED)

    @property
    def is_gap(self) -> bool:
        return self.status is EvidenceStatus.SOURCE_UNAVAILABLE

    @model_validator(mode="after")
    def _untrusted_is_tier_d(self) -> EvidenceItem:
        """Free text carries no more trust than Tier D, whatever the caller said."""
        if isinstance(self.content, UntrustedText) and self.trust_class is not TrustClass.TIER_D:
            raise ValueError("UntrustedText content must be TrustClass.TIER_D")
        return self


class EvidenceGap(Frozen):
    """A source that could not be consulted.

    Recorded as a first-class fact so the UI can say 'Prometheus was unreachable'
    instead of silently implying 'no latency anomaly exists'.
    """

    source: str
    source_type: SourceType
    reason: str
    attempted_at: datetime
    affects: list[str] = Field(default_factory=list)


# --------------------------------------------------------------------------- #
# hypotheses                                                                   #
# --------------------------------------------------------------------------- #


class Prediction(Mutable):
    """A falsifiable consequence of a hypothesis.

    Each prediction is a query Aegis can actually run. This is what moves the
    system from narrative RCA toward testable diagnosis (AIArchitecture 15).
    """

    statement: str
    metric: str | None = None
    resource_id: str | None = None
    direction: MetricDirection | None = None
    tested: bool = False
    holds: bool | None = None
    evidence_ids: list[str] = Field(default_factory=list)


class Hypothesis(Mutable):
    id: str
    statement: str
    supporting: list[str] = Field(default_factory=list)
    contradicting: list[str] = Field(default_factory=list)
    missing: list[str] = Field(default_factory=list)
    predictions: list[Prediction] = Field(default_factory=list)
    affected_services: list[str] = Field(default_factory=list)
    confidence: Confidence = 0.0
    state: HypothesisState = HypothesisState.PROPOSED
    rejected_reason: str | None = None

    @property
    def tested_predictions(self) -> list[Prediction]:
        return [p for p in self.predictions if p.tested]

    @property
    def coverage(self) -> float:
        """Fraction of predictions actually tested. Drives derived confidence."""
        if not self.predictions:
            return 0.0
        return len(self.tested_predictions) / len(self.predictions)

    @property
    def test_pass_rate(self) -> float:
        tested = self.tested_predictions
        if not tested:
            return 0.0
        return sum(1 for p in tested if p.holds) / len(tested)


class Diagnosis(Frozen):
    """A conclusion, or an explicit refusal to conclude.

    ``abstained`` is a legitimate, well-supported outcome. Insufficient evidence
    is more useful to an on-call engineer than a fluent guess (PRD 4.5).
    """

    incident_id: str
    abstained: bool
    statement: str
    root_cause_category: str | None = None
    confidence: Confidence = 0.0
    selected_hypothesis_id: str | None = None
    supporting_evidence: list[str] = Field(default_factory=list)
    causal_path: list[str] = Field(default_factory=list)
    affected_services: list[str] = Field(default_factory=list)
    contributing_factors: list[str] = Field(default_factory=list)
    rejected_alternatives: list[str] = Field(default_factory=list)
    missing_evidence: list[str] = Field(default_factory=list)
    uncertainty: str = ""
    confidence_model_version: str = "1.0.0"

    @model_validator(mode="after")
    def _grounding(self) -> Diagnosis:
        """A non-abstaining diagnosis must cite evidence. No exceptions."""
        if not self.abstained and not self.supporting_evidence:
            raise ValueError("a non-abstaining diagnosis must cite supporting evidence")
        return self


# --------------------------------------------------------------------------- #
# actions and safety                                                           #
# --------------------------------------------------------------------------- #


class ExpectedEffect(Frozen):
    """What success looks like, stated numerically before the action runs."""

    metric: str
    direction: MetricDirection
    threshold: float
    window_seconds: int = Field(default=300, gt=0)
    resource_id: str | None = None


class BlastRadius(Frozen):
    directly_affected: list[str] = Field(default_factory=list)
    downstream: list[str] = Field(default_factory=list)
    customer_facing: bool = False
    estimated_request_share: float = Field(default=0.0, ge=0.0, le=1.0)

    @property
    def size(self) -> int:
        return len(set(self.directly_affected) | set(self.downstream))


class RollbackPlan(Frozen):
    """ESD section 39: no known safe reversal means the action is not autonomous."""

    strategy: Literal["inverse_action", "compensating_action", "pipeline_rollback",
                      "immutable_redeploy"]
    description: str
    inverse_action_type: ActionType | None = None
    automatic: bool = False


class VerificationPlan(Frozen):
    """Mandatory. An action whose success cannot be measured cannot be proposed."""

    target_metric: str
    direction: MetricDirection
    threshold: float
    observation_window_s: int = Field(default=300, gt=0)
    protected_metrics: list[str] = Field(default_factory=list)
    regression_tolerance: float = Field(default=0.05, ge=0.0, le=1.0)


ArgValue = str | int | float | bool


class ActionProposal(Frozen):
    """An agent's *request*. Carries no authority whatsoever.

    Only ``execution.ValidatedAction`` - constructible solely by the gate chain -
    can be executed. An agent has no way to build that type.
    """

    id: str
    incident_id: str
    action_type: ActionType
    target: ResourceRef
    reason: str
    # Scalars only, and a hard cap on count. An action argument is a number or a
    # flag - a replica count, a cache key - never a nested structure. Allowing
    # arbitrary JSON here would turn the argument channel into a way to smuggle
    # structure past the typed boundary the rest of the model enforces.
    arguments: dict[str, ArgValue] = Field(default_factory=dict, max_length=16)
    supporting_evidence: list[str] = Field(min_length=1)
    expected_effect: ExpectedEffect
    blast_radius: BlastRadius
    rollback: RollbackPlan | None = None
    verification: VerificationPlan
    idempotency_key: str
    proposed_by: AgentRole = AgentRole.REMEDIATION_PLANNER
    proposed_at: datetime
    state: ActionState = ActionState.PROPOSED

    @field_validator("arguments")
    @classmethod
    def _bounded_arguments(cls, v: dict[str, ArgValue]) -> dict[str, ArgValue]:
        for key, value in v.items():
            if not key or len(key) > 64:
                raise ValueError(f"action argument name {key!r} is out of bounds")
            if isinstance(value, str) and len(value) > 512:
                raise ValueError(f"action argument {key!r} exceeds 512 characters")
        return v


class GateResult(Frozen):
    gate: str
    passed: bool
    reason: str = ""
    detail: dict[str, Any] = Field(default_factory=dict)


class PolicyDecision(Frozen):
    """Risk tier and effect are computed independently and stored separately.

    This is the structural defence against an agent arguing a dangerous action
    into a lower risk class (ESD section 18).
    """

    effect: PolicyEffect
    risk_tier: RiskTier
    matched_rule: str
    reasons: list[str] = Field(default_factory=list)
    gates: list[GateResult] = Field(default_factory=list)
    policy_version: str
    decided_at: datetime
    expires_at: datetime | None = None

    @property
    def allows_autonomous_execution(self) -> bool:
        return self.effect is PolicyEffect.ALLOW


class VerificationCheck(Frozen):
    name: str
    passed: bool
    before: float | None = None
    after: float | None = None
    threshold: float | None = None
    detail: str = ""


class VerificationResult(Frozen):
    id: str
    incident_id: str
    action_id: str | None = None
    passed: bool
    checks: list[VerificationCheck] = Field(default_factory=list)
    started_at: datetime
    completed_at: datetime
    notes: str = ""

    @property
    def failed_checks(self) -> list[VerificationCheck]:
        return [c for c in self.checks if not c.passed]


# --------------------------------------------------------------------------- #
# incident                                                                     #
# --------------------------------------------------------------------------- #


class Alert(Frozen):
    id: str
    incident_id: str
    source: str
    external_id: str
    title: str
    severity: Severity
    received_at: datetime
    started_at: datetime | None = None
    service_hint: str | None = None
    labels: dict[str, str] = Field(default_factory=dict)
    annotations: dict[str, UntrustedText | str] = Field(default_factory=dict)
    raw_payload: dict[str, Any] = Field(default_factory=dict)


class Incident(Mutable):
    id: str
    title: str
    severity: Severity
    state: IncidentState = IncidentState.RECEIVED
    environment: str
    workload: str = "default"
    created_at: datetime
    updated_at: datetime
    resolved_at: datetime | None = None
    affected_services: list[str] = Field(default_factory=list)
    suspected_origin: str | None = None
    confidence: Confidence | None = None
    summary: str = ""
    owner: str | None = None
    correlation_id: str = ""
    evidence_gaps: list[EvidenceGap] = Field(default_factory=list)

    @property
    def is_open(self) -> bool:
        return not self.state.is_terminal


class ServiceState(Frozen):
    """Normalized runtime state - identical shape from Compose, K8s or ECS."""

    ref: ServiceRef
    health: ServiceHealth = ServiceHealth.UNKNOWN
    version: str | None = None
    desired_instances: int = 0
    ready_instances: int = 0
    error_rate: float | None = None
    latency_p99_ms: float | None = None
    owner_team: str | None = None

    @property
    def is_degraded(self) -> bool:
        return self.ready_instances < self.desired_instances
