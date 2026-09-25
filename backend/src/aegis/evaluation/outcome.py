"""What the system under test actually produced for one scenario.

Everything the evaluators score is captured here first, as plain frozen
dataclasses with no behaviour. Two reasons:

* **Evaluators stay pure.** They take (ground truth, outcome) and return
  numbers. No evaluator reaches into Postgres, Prometheus or an agent state
  dict, so every metric is unit-testable on a hand-built outcome.
* **The adapter boundary is explicit.** Anything that wants to be benchmarked -
  the Aegis workflow, a single-agent baseline, a deterministic baseline - only
  has to produce a ``RunOutcome``. The benchmark is not welded to LangGraph.

A ``RunOutcome`` records what was *observed*, including "nothing was observed
because the environment fell over": ``harness_failure`` carries that case and
is kept strictly apart from anything the model did (ESD section 32).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from aegis.core.errors import ValidationError
from aegis.domain.enums import (
    ActionState,
    FailureClass,
    PolicyEffect,
    RiskTier,
    VerificationVerdict,
)


@dataclass(frozen=True, slots=True)
class HarnessFailure:
    """The environment failed, not the model.

    Constructing one with a model-quality ``FailureClass`` raises: the whole
    value of this type is that a Prometheus outage can never be laundered into
    a localization failure.
    """

    failure_class: FailureClass
    message: str
    detail: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.failure_class.is_harness_failure:
            raise ValidationError(
                f"{self.failure_class.value} is a model-quality failure and cannot be "
                "recorded as a harness failure",
                context={"failure_class": self.failure_class.value},
            )


@dataclass(frozen=True, slots=True)
class ObservedEvidence:
    """One evidence item the investigation actually collected."""

    evidence_id: str
    source_type: str
    evidence_type: str
    resource_id: str | None = None
    trust_class: str = "TIER_D"
    status: str = "UNVALIDATED"

    @property
    def key(self) -> tuple[str, str, str | None]:
        return (self.source_type, self.evidence_type, self.resource_id)

    @property
    def is_gap(self) -> bool:
        return self.status == "SOURCE_UNAVAILABLE"


@dataclass(frozen=True, slots=True)
class ObservedTool:
    """One tool call, with enough shape to score selection and recovery."""

    name: str
    source_type: str
    succeeded: bool = True
    produced_evidence: bool = False
    write_access: bool = False
    duration_ms: int = 0


@dataclass(frozen=True, slots=True)
class ObservedAction:
    """One action Aegis proposed, and what the safety chain did with it.

    ``executed_autonomously`` and ``approval_obtained`` are separate fields on
    purpose: "it ran" and "a human said yes" are different facts, and the unsafe
    autonomy metric is exactly the case where the first is true and the second
    is not.
    """

    action_type: str
    target_resource_id: str = ""
    risk_tier: RiskTier = RiskTier.OBSERVE
    policy_effect: PolicyEffect = PolicyEffect.BLOCK
    state: ActionState = ActionState.PROPOSED
    executed: bool = False
    executed_autonomously: bool = False
    approval_obtained: bool = False
    rolled_back: bool = False
    rollback_succeeded: bool | None = None
    verification_verdict: VerificationVerdict | None = None


@dataclass(frozen=True, slots=True)
class ObservedRemediation:
    """The debugging and patch side of a run, where the scenario has one."""

    category: str | None = None
    patch_proposed: bool = False
    patch_applies: bool | None = None
    reproduction_succeeded: bool | None = None
    regression_suite_passed: bool | None = None
    staging_verified: bool | None = None
    production_verified: bool | None = None
    files_touched: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ObservedVerification:
    """What the deterministic verification engine concluded, if it ran."""

    verdict: VerificationVerdict | None = None
    criteria_tested: tuple[str, ...] = ()
    protected_metrics_tested: tuple[str, ...] = ()
    regression_detected: bool = False


@dataclass(frozen=True, slots=True)
class CostObservation:
    """Measured, never estimated by a model.

    ``llm_cost_usd`` comes from recorded token usage priced by the harness cost
    model; latency is wall time. Nothing here is self-reported by the agent.
    """

    llm_cost_usd: float = 0.0
    execution_cost_usd: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    llm_calls: int = 0
    tool_calls: int = 0
    wall_ms: int = 0
    time_to_first_hypothesis_ms: int | None = None
    time_to_diagnosis_ms: int | None = None
    mttr_ms: int | None = None

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    @property
    def total_cost_usd(self) -> float:
        return round(self.llm_cost_usd + self.execution_cost_usd, 6)


@dataclass(frozen=True, slots=True)
class RunOutcome:
    """One scenario's observed result. The sole input to every evaluator."""

    case_ref: str
    incident_id: str | None = None

    # diagnosis
    # Did Aegis treat the alert as a real incident? Three states, not two:
    # ``None`` means the run could not be observed well enough to say. An
    # adapter that cannot tell must report ``None``; hardcoding ``True`` makes
    # DETECTION_FAILURE unreachable and every detection number a tautology.
    detected: bool | None = None
    abstained: bool = False
    root_cause_service: str | None = None
    root_cause_category: str | None = None
    root_cause_statement: str = ""
    affected_services: tuple[str, ...] = ()
    causal_path: tuple[str, ...] = ()
    confidence: float = 0.0
    cited_evidence_ids: tuple[str, ...] = ()
    # Did the investigation-side grounding gate run for this diagnosis? ``False``
    # marks an arm where an ablation removed it, so an unverified result is never
    # read as a verified one. ``None`` means the question never arose.
    grounding_verified: bool | None = None

    # process
    evidence: tuple[ObservedEvidence, ...] = ()
    tools: tuple[ObservedTool, ...] = ()
    actions: tuple[ObservedAction, ...] = ()
    remediation: ObservedRemediation = ObservedRemediation()
    verification: ObservedVerification = ObservedVerification()
    cost: CostObservation = CostObservation()

    # environment
    evidence_gaps: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()
    harness_failure: HarnessFailure | None = None
    langsmith_run_id: str | None = None

    @property
    def is_harness_failure(self) -> bool:
        return self.harness_failure is not None

    @property
    def executed_actions(self) -> tuple[ObservedAction, ...]:
        return tuple(a for a in self.actions if a.executed)

    def as_json(self) -> dict[str, Any]:
        """Stored in ``benchmark_results.predicted`` for replay and audit."""
        return {
            "case_ref": self.case_ref,
            "incident_id": self.incident_id,
            "detected": self.detected,
            "abstained": self.abstained,
            "root_cause_service": self.root_cause_service,
            "root_cause_category": self.root_cause_category,
            "root_cause_statement": self.root_cause_statement[:2000],
            "affected_services": list(self.affected_services),
            "causal_path": list(self.causal_path),
            "confidence": self.confidence,
            "cited_evidence_ids": list(self.cited_evidence_ids),
            "grounding_verified": self.grounding_verified,
            "evidence": [
                {
                    "id": e.evidence_id,
                    "source_type": e.source_type,
                    "evidence_type": e.evidence_type,
                    "resource_id": e.resource_id,
                    "trust_class": e.trust_class,
                    "status": e.status,
                }
                for e in self.evidence
            ],
            "tools": [
                {
                    "name": t.name,
                    "source_type": t.source_type,
                    "ok": t.succeeded,
                    "produced_evidence": t.produced_evidence,
                    "write": t.write_access,
                }
                for t in self.tools
            ],
            "actions": [
                {
                    "action_type": a.action_type,
                    "risk_tier": int(a.risk_tier),
                    "policy_effect": a.policy_effect.value,
                    "state": a.state.value,
                    "executed": a.executed,
                    "autonomous": a.executed_autonomously,
                    "approved": a.approval_obtained,
                    "rolled_back": a.rolled_back,
                    "rollback_succeeded": a.rollback_succeeded,
                    "verification": (
                        a.verification_verdict.value if a.verification_verdict else None
                    ),
                }
                for a in self.actions
            ],
            "remediation": {
                "category": self.remediation.category,
                "patch_proposed": self.remediation.patch_proposed,
                "patch_applies": self.remediation.patch_applies,
                "reproduction_succeeded": self.remediation.reproduction_succeeded,
                "regression_suite_passed": self.remediation.regression_suite_passed,
                "staging_verified": self.remediation.staging_verified,
                "production_verified": self.remediation.production_verified,
                "files_touched": list(self.remediation.files_touched),
            },
            "verification": {
                "verdict": (
                    self.verification.verdict.value if self.verification.verdict else None
                ),
                "criteria_tested": list(self.verification.criteria_tested),
                "protected_metrics_tested": list(self.verification.protected_metrics_tested),
                "regression_detected": self.verification.regression_detected,
            },
            "cost": {
                "llm_usd": self.cost.llm_cost_usd,
                "execution_usd": self.cost.execution_cost_usd,
                "input_tokens": self.cost.input_tokens,
                "output_tokens": self.cost.output_tokens,
                "llm_calls": self.cost.llm_calls,
                "tool_calls": self.cost.tool_calls,
                "wall_ms": self.cost.wall_ms,
                "ttfh_ms": self.cost.time_to_first_hypothesis_ms,
                "ttd_ms": self.cost.time_to_diagnosis_ms,
                "mttr_ms": self.cost.mttr_ms,
            },
            "evidence_gaps": list(self.evidence_gaps),
            "errors": list(self.errors)[:20],
            "harness_failure": (
                {
                    "class": self.harness_failure.failure_class.value,
                    "message": self.harness_failure.message,
                }
                if self.harness_failure
                else None
            ),
            "langsmith_run_id": self.langsmith_run_id,
        }


__all__ = [
    "CostObservation",
    "HarnessFailure",
    "ObservedAction",
    "ObservedEvidence",
    "ObservedRemediation",
    "ObservedTool",
    "ObservedVerification",
    "RunOutcome",
]
