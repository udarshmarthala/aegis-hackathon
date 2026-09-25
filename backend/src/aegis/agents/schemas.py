"""Structured output contracts for LLM calls.

Every model response is parsed into one of these. A free-text response is never
consumed directly, which is what stops prose from becoming control flow.

Each schema forces the model to expose its grounding: which evidence supports a
claim, which contradicts it, and what is still missing. A response that cannot
fill those fields is, correctly, a weak response.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class TriageOut(BaseModel):
    """Initial assessment from the alert alone."""

    restated_problem: str = Field(description="One sentence, factual, no speculation.")
    candidate_services: list[str] = Field(
        default_factory=list,
        description="Service names plausibly involved, most likely first. Empty if unclear.",
    )
    suggested_severity: str = Field(
        default="P3", description="One of P1, P2, P3, P4 based on stated impact only."
    )
    investigation_focus: list[str] = Field(
        default_factory=list,
        description="Ordered questions worth answering first.",
    )


class PredictionOut(BaseModel):
    statement: str = Field(description="An observable consequence if the hypothesis holds.")
    metric: str | None = Field(default=None, description="Metric that would show it.")
    resource_id: str | None = None
    direction: str | None = Field(default=None, description="increase, decrease or stable")


class HypothesisOut(BaseModel):
    label: str = Field(description="Short identifier such as H1.")
    statement: str = Field(description="A specific, falsifiable causal claim.")
    supporting_evidence: list[str] = Field(
        default_factory=list, description="Evidence ids that support this. Ids only."
    )
    contradicting_evidence: list[str] = Field(
        default_factory=list, description="Evidence ids that argue against it."
    )
    missing_evidence: list[str] = Field(
        default_factory=list, description="What would most reduce uncertainty."
    )
    predictions: list[PredictionOut] = Field(
        default_factory=list, description="At least one testable prediction."
    )
    affected_services: list[str] = Field(default_factory=list)


class HypothesisSetOut(BaseModel):
    """Competing hypotheses, not a single conclusion (UX spec 21)."""

    hypotheses: list[HypothesisOut] = Field(
        default_factory=list,
        description="Two to four competing explanations, strongest first.",
    )
    recommended_next_check: str = Field(
        default="", description="The single most informative next investigation step."
    )


class DiagnosisOut(BaseModel):
    """A conclusion or an explicit refusal to conclude."""

    abstain: bool = Field(
        description="True when evidence is insufficient. Abstaining is a valid answer."
    )
    statement: str = Field(description="The conclusion, or why one cannot be drawn.")
    root_cause_category: str | None = Field(
        default=None,
        description="Short stable category, e.g. connection_pool_exhaustion.",
    )
    selected_hypothesis_label: str | None = None
    supporting_evidence: list[str] = Field(
        default_factory=list, description="Evidence ids. Required unless abstaining."
    )
    causal_path: list[str] = Field(
        default_factory=list, description="Service names from symptom to origin."
    )
    affected_services: list[str] = Field(default_factory=list)
    contributing_factors: list[str] = Field(default_factory=list)
    rejected_alternatives: list[str] = Field(
        default_factory=list, description="Which hypotheses were ruled out and why."
    )
    missing_evidence: list[str] = Field(default_factory=list)
    uncertainty: str = Field(default="", description="What remains unknown.")


class RemediationOut(BaseModel):
    """A *proposal*. It carries no authority; policy decides separately."""

    recommend_action: bool
    action_type: str | None = Field(
        default=None, description="Must be an action type from the supplied registry."
    )
    target_resource_id: str | None = None
    reason: str = ""
    expected_metric: str | None = None
    expected_direction: str | None = None
    expected_threshold: float | None = None
    rollback_description: str | None = None
    blast_radius_services: list[str] = Field(default_factory=list)
    supporting_evidence: list[str] = Field(default_factory=list)


class PatchProposalOut(BaseModel):
    """A candidate code change. A *proposal*, exactly like RemediationOut.

    Nothing here is trusted: the diff is parsed and checked against the file
    list code localisation produced before anything executes it, the line counts
    and the hash are recomputed from the text, and the patch is never applied to
    an environment - only inside a disposable container.
    """

    propose_patch: bool = Field(
        description="False when the diagnosis does not identify a fixable code defect."
    )
    summary: str = Field(
        default="", description="One line: what the change does. Not why."
    )
    rationale: str = Field(
        default="",
        description="Why this change addresses the diagnosed cause, citing evidence ids.",
    )
    diff: str = Field(
        default="",
        description=(
            "A unified diff with --- / +++ headers and @@ hunk headers. "
            "Empty when propose_patch is false."
        ),
    )
    files_changed: list[str] = Field(
        default_factory=list,
        description="Paths the diff touches. Must all be candidate files.",
    )
    supporting_evidence: list[str] = Field(
        default_factory=list, description="Evidence ids supporting this change."
    )
    reason: str = Field(
        default="", description="When declining, what stopped you proposing a patch."
    )


class CommunicationOut(BaseModel):
    """One truth, several audiences (UX spec 41)."""

    engineering: str = Field(description="Precise, technical, cites services and metrics.")
    incident_commander: str = Field(description="Status, impact, next decision needed.")
    leadership: str = Field(description="Business impact, no jargon, no speculation.")
    customer: str = Field(default="", description="External wording. Conservative.")
