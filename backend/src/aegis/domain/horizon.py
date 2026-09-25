"""Long-horizon incident state.

The horizon orchestrator never shows the model a transcript. Every step's
prompt is rebuilt from the explicit state in this module, so the context the
model sees stays flat however long an incident runs. That only holds if the
state itself is bounded, which is why every collection here has a hard cap and
the caps are enforced by validators rather than by convention.

No I/O lives here. Persistence is ``persistence.horizon``; the loop is
``agents.horizon``.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Any, Final

from pydantic import Field, field_validator

from aegis.domain.enums import IncidentState
from aegis.domain.models import Frozen, Mutable

# Hard caps. The context builder's token budget is derived from these, so a
# change here is an AI-behaviour change.
MAX_CONTEXT_CARDS: Final = 12
MAX_MEMORY_CARDS: Final = 3
MAX_HYPOTHESES: Final = 6
MAX_GOALS: Final = 12
MAX_NOTES: Final = 8
MAX_NOTE_CHARS: Final = 300
MAX_CARD_CLAIM_CHARS: Final = 240
MAX_REMEDIATION_CYCLES: Final = 3
MAX_TOOL_CALLS_PER_STEP: Final = 4

Unit = Annotated[float, Field(ge=0.0, le=1.0)]


class HorizonPhase(StrEnum):
    IDLE = "IDLE"
    DETECTING = "DETECTING"
    INVESTIGATING = "INVESTIGATING"
    DIAGNOSING = "DIAGNOSING"
    PLANNING = "PLANNING"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    EXECUTING = "EXECUTING"
    VERIFYING = "VERIFYING"
    REASSESSING = "REASSESSING"
    RESOLVED = "RESOLVED"
    ESCALATED = "ESCALATED"

    @property
    def is_terminal(self) -> bool:
        return self in (HorizonPhase.RESOLVED, HorizonPhase.ESCALATED, HorizonPhase.IDLE)


# Legal phase moves. The model never chooses a phase; the orchestrator's guard
# picks one of these and anything else raises.
PHASE_TRANSITIONS: Final[dict[HorizonPhase, frozenset[HorizonPhase]]] = {
    HorizonPhase.IDLE: frozenset({HorizonPhase.DETECTING}),
    HorizonPhase.DETECTING: frozenset({HorizonPhase.INVESTIGATING, HorizonPhase.IDLE}),
    HorizonPhase.INVESTIGATING: frozenset(
        {HorizonPhase.DIAGNOSING, HorizonPhase.ESCALATED}
    ),
    HorizonPhase.DIAGNOSING: frozenset(
        {HorizonPhase.PLANNING, HorizonPhase.INVESTIGATING, HorizonPhase.ESCALATED}
    ),
    HorizonPhase.PLANNING: frozenset(
        {
            HorizonPhase.EXECUTING,
            HorizonPhase.AWAITING_APPROVAL,
            HorizonPhase.DIAGNOSING,
            HorizonPhase.ESCALATED,
        }
    ),
    HorizonPhase.AWAITING_APPROVAL: frozenset(
        {HorizonPhase.EXECUTING, HorizonPhase.DIAGNOSING, HorizonPhase.ESCALATED}
    ),
    HorizonPhase.EXECUTING: frozenset({HorizonPhase.VERIFYING, HorizonPhase.ESCALATED}),
    HorizonPhase.VERIFYING: frozenset(
        {HorizonPhase.RESOLVED, HorizonPhase.REASSESSING, HorizonPhase.ESCALATED}
    ),
    HorizonPhase.REASSESSING: frozenset({HorizonPhase.DIAGNOSING, HorizonPhase.ESCALATED}),
    HorizonPhase.RESOLVED: frozenset({HorizonPhase.IDLE}),
    HorizonPhase.ESCALATED: frozenset({HorizonPhase.IDLE}),
}

# How each horizon phase is reflected on the incident's own lifecycle, which
# ``domain.state_machines`` still guards.
PHASE_TO_INCIDENT_STATE: Final[dict[HorizonPhase, IncidentState]] = {
    HorizonPhase.DETECTING: IncidentState.TRIAGING,
    HorizonPhase.INVESTIGATING: IncidentState.INVESTIGATING,
    HorizonPhase.DIAGNOSING: IncidentState.DIAGNOSING,
    HorizonPhase.PLANNING: IncidentState.DIAGNOSING,
    HorizonPhase.AWAITING_APPROVAL: IncidentState.AWAITING_APPROVAL,
    HorizonPhase.EXECUTING: IncidentState.REMEDIATING,
    HorizonPhase.VERIFYING: IncidentState.MONITORING,
    HorizonPhase.REASSESSING: IncidentState.DIAGNOSING,
    HorizonPhase.RESOLVED: IncidentState.RESOLVED,
    HorizonPhase.ESCALATED: IncidentState.ESCALATED,
}


class IllegalPhaseTransition(ValueError):
    """Raised by ``assert_phase_transition``. A defect, never a model error."""


def assert_phase_transition(src: HorizonPhase, dst: HorizonPhase) -> None:
    if dst not in PHASE_TRANSITIONS[src]:
        raise IllegalPhaseTransition(f"{src.value} -> {dst.value} is not a legal move")


class Source(StrEnum):
    """Which path actually produced a card, event or decision.

    Shown on every surface. A fallback that is not labelled is a fallback that
    is being passed off as the real thing.
    """

    BEDROCK = "bedrock"
    GEMINI = "gemini"
    SCRIPTED = "scripted"
    RULE = "rule"
    RAWTREE = "rawtree"
    POSTGRES = "postgres"
    PROMETHEUS = "prometheus"
    RUNTIME = "runtime"
    NIMBLE = "nimble"
    FIXTURE = "fixture"
    FLUX = "flux"
    ZSCORE = "zscore"
    MEMORY = "memory"
    TOOL = "tool"
    SYSTEM = "system"


class GoalStatus(StrEnum):
    PENDING = "pending"
    ACTIVE = "active"
    DONE = "done"
    FAILED = "failed"
    SKIPPED = "skipped"


GOAL_TRANSITIONS: Final[dict[GoalStatus, frozenset[GoalStatus]]] = {
    GoalStatus.PENDING: frozenset({GoalStatus.ACTIVE, GoalStatus.SKIPPED}),
    GoalStatus.ACTIVE: frozenset({GoalStatus.DONE, GoalStatus.FAILED, GoalStatus.SKIPPED}),
    GoalStatus.FAILED: frozenset({GoalStatus.ACTIVE}),
    GoalStatus.DONE: frozenset(),
    GoalStatus.SKIPPED: frozenset({GoalStatus.ACTIVE}),
}


class Goal(Mutable):
    id: str
    title: str = Field(max_length=120)
    status: GoalStatus = GoalStatus.PENDING
    parent_id: str | None = None


class EvidenceCard(Frozen):
    """One compacted observation, at most ~60 tokens.

    The raw output it was compacted from is stored elsewhere (Postgres
    ``evidence_items`` and RawTree ``observations``) and referenced by
    ``raw_ref``. The card is what the model sees; the raw text never is.
    """

    id: str  # the evidence id; citations use it
    step: int = Field(ge=0)
    tool: str
    source: Source  # which compaction path produced the card
    origin: Source = Source.TOOL  # where the underlying observation came from
    claim: str = Field(max_length=MAX_CARD_CLAIM_CHARS)
    supports: list[str] = Field(default_factory=list, max_length=4)
    refutes: list[str] = Field(default_factory=list, max_length=4)
    weight: Unit = 0.5
    raw_ref: str = ""
    url: str | None = None  # external evidence (Nimble) keeps its source URL
    tokens_raw: int = Field(default=0, ge=0)
    tokens_card: int = Field(default=0, ge=0)
    pinned: bool = False

    def render(self) -> str:
        """One line for the [EVIDENCE] section."""
        tail = f" +{','.join(self.supports)}" if self.supports else ""
        tail += f" -{','.join(self.refutes)}" if self.refutes else ""
        return f"[{self.id}] s{self.step} {self.tool}/{self.source.value}: {self.claim}{tail}"


class HorizonHypothesis(Mutable):
    """A hypothesis the model maintains through ``update_hypotheses``.

    ``confidence`` is written by the orchestrator from the cited evidence
    (``agents.horizon.phases.derive_confidence``); whatever the model claims is
    ignored. ``history`` keeps the last few values for the UI's sparkline.
    """

    id: str
    statement: str = Field(max_length=240)
    supporting: list[str] = Field(default_factory=list, max_length=12)
    refuting: list[str] = Field(default_factory=list, max_length=12)
    confidence: Unit = 0.0
    history: list[float] = Field(default_factory=list, max_length=16)
    suggested_action: str | None = None  # an ActionType value, validated on use


class MemoryCard(Frozen):
    """A resolved incident, recalled by similarity. One line in the prompt."""

    id: str
    incident_id: str
    symptoms: str
    root_cause: str
    failed_actions: list[str] = Field(default_factory=list)
    successful_action: str | None = None
    recovery_s: float | None = None
    lesson: str = ""
    image_url: str | None = None  # FLUX incident map, when one was rendered
    image_status: str = "pending"  # pending | ready | unavailable
    image_reason: str = ""
    source: Source = Source.MEMORY

    def render(self) -> str:
        failed = f" failed:{'/'.join(self.failed_actions)}" if self.failed_actions else ""
        fixed = f" fixed_by:{self.successful_action}" if self.successful_action else ""
        return (
            f"[{self.incident_id}] {self.symptoms} -> {self.root_cause};"
            f"{failed}{fixed} lesson: {self.lesson}"
        )[:400]


class ActionAttempt(Mutable):
    """One remediation the orchestrator took through the gate chain."""

    action_id: str | None = None
    action_type: str
    target: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    cycle: int = Field(ge=1)
    outcome: str = "pending"  # pending|awaiting_approval|denied|refused|verified|failed
    detail: str = ""

    @property
    def signature(self) -> str:
        """Identity used to reject re-running an identical failed action."""
        args = ",".join(f"{k}={self.arguments[k]}" for k in sorted(self.arguments))
        return f"{self.action_type}:{self.target}:{args}"


class TokenStats(Mutable):
    context_tokens: int = 0  # size of the prompt actually sent this step
    naive_tokens: int = 0  # what a transcript-carrying agent would hold by now
    cache_read_tokens: int = 0
    cache_hits: int = 0
    fallbacks_used: int = 0
    compacted_raw_tokens: int = 0
    compacted_card_tokens: int = 0


class HorizonState(Mutable):
    """The whole of the agent's working memory. Checkpointed after every step."""

    run_id: str
    incident_id: str
    service: str
    symptom: str = ""
    step: int = 0
    phase: HorizonPhase = HorizonPhase.DETECTING
    remediation_cycle: int = 0
    goals: list[Goal] = Field(default_factory=list, max_length=MAX_GOALS)
    hypotheses: list[HorizonHypothesis] = Field(
        default_factory=list, max_length=MAX_HYPOTHESES
    )
    evidence: list[EvidenceCard] = Field(default_factory=list)  # in-context cards
    discarded: list[str] = Field(default_factory=list)  # ids evicted to storage
    memory: list[MemoryCard] = Field(default_factory=list, max_length=MAX_MEMORY_CARDS)
    notes: list[str] = Field(default_factory=list, max_length=MAX_NOTES)
    observe_tools_run: list[str] = Field(default_factory=list)
    actions: list[ActionAttempt] = Field(default_factory=list)
    excluded_actions: list[str] = Field(default_factory=list)  # ActionAttempt.signature
    pending_action_id: str | None = None
    brain_source: Source = Source.SCRIPTED
    tokens: TokenStats = Field(default_factory=TokenStats)
    escalation_reason: str | None = None
    started_at: datetime | None = None
    updated_at: datetime | None = None

    @field_validator("notes")
    @classmethod
    def _notes_bounded(cls, v: list[str]) -> list[str]:
        for note in v:
            if len(note) > MAX_NOTE_CHARS:
                raise ValueError(f"note exceeds {MAX_NOTE_CHARS} characters")
        return v

    def card(self, evidence_id: str) -> EvidenceCard | None:
        return next((c for c in self.evidence if c.id == evidence_id), None)

    def top_hypothesis(self) -> HorizonHypothesis | None:
        return max(self.hypotheses, key=lambda h: h.confidence, default=None)

    def compact_json(self) -> dict[str, Any]:
        """The [STATE] section: history-bearing fields excluded."""
        return {
            "incident_id": self.incident_id,
            "service": self.service,
            "symptom": self.symptom,
            "step": self.step,
            "phase": self.phase.value,
            "remediation_cycle": self.remediation_cycle,
            "goals": [
                {"id": g.id, "title": g.title, "status": g.status.value} for g in self.goals
            ],
            "hypotheses": [
                {
                    "id": h.id,
                    "statement": h.statement,
                    "confidence": round(h.confidence, 2),
                    "supporting": h.supporting,
                    "refuting": h.refuting,
                    "suggested_action": h.suggested_action,
                }
                for h in self.hypotheses
            ],
            "notes": self.notes,
            "actions": [
                {"type": a.action_type, "target": a.target, "outcome": a.outcome}
                for a in self.actions
            ],
            "excluded_actions": self.excluded_actions,
        }


class HorizonEventType(StrEnum):
    PHASE_CHANGED = "phase_changed"
    STEP_STARTED = "step_started"
    STEP_COMPLETED = "step_completed"
    BRAIN_DECISION = "brain_decision"
    BRAIN_FALLBACK = "brain_fallback"
    TOOL_CALLED = "tool_called"
    EVIDENCE_ADDED = "evidence_added"
    EVIDENCE_DISCARDED = "evidence_discarded"
    EVIDENCE_RECALLED = "evidence_recalled"
    HYPOTHESES_UPDATED = "hypotheses_updated"
    GOAL_UPDATED = "goal_updated"
    NOTE_WRITTEN = "note_written"
    SELF_EDIT_REJECTED = "self_edit_rejected"
    MEMORY_RECALLED = "memory_recalled"
    ACTION_PROPOSED = "action_proposed"
    APPROVAL_REQUIRED = "approval_required"
    APPROVAL_RESOLVED = "approval_resolved"
    ACTION_EXECUTED = "action_executed"
    VERIFICATION_RESULT = "verification_result"
    RAWTREE_QUERY = "rawtree_query"
    HEARTBEAT_ANOMALY = "heartbeat_anomaly"
    CHECKPOINT_SAVED = "checkpoint_saved"
    RESUMED = "resumed"
    MEMORY_CARD_WRITTEN = "memory_card_written"
    INCIDENT_MAP = "incident_map"
    RESOLVED = "resolved"
    ESCALATED = "escalated"


class HorizonEvent(Frozen):
    """One entry in the war-room event stream and RawTree ``agent_events``.

    ``payload`` carries event-specific detail (a card, a hypothesis list, SQL
    text). It never carries a credential: publishers pass it through
    ``core.logging`` redaction before it leaves the process.
    """

    ts: datetime
    run_id: str
    incident_id: str
    step: int
    phase: HorizonPhase
    event_type: HorizonEventType
    tool: str | None = None
    status: str = "ok"  # ok | error | rejected | degraded
    duration_ms: int = 0
    source: Source = Source.SYSTEM
    context_tokens: int = 0
    naive_tokens: int = 0
    message: str = Field(default="", max_length=500)
    payload: dict[str, Any] = Field(default_factory=dict)


__all__ = [
    "GOAL_TRANSITIONS",
    "MAX_CARD_CLAIM_CHARS",
    "MAX_CONTEXT_CARDS",
    "MAX_GOALS",
    "MAX_HYPOTHESES",
    "MAX_MEMORY_CARDS",
    "MAX_NOTES",
    "MAX_NOTE_CHARS",
    "MAX_REMEDIATION_CYCLES",
    "MAX_TOOL_CALLS_PER_STEP",
    "PHASE_TO_INCIDENT_STATE",
    "PHASE_TRANSITIONS",
    "ActionAttempt",
    "EvidenceCard",
    "Goal",
    "GoalStatus",
    "HorizonEvent",
    "HorizonEventType",
    "HorizonHypothesis",
    "HorizonPhase",
    "HorizonState",
    "IllegalPhaseTransition",
    "MemoryCard",
    "Source",
    "TokenStats",
    "assert_phase_transition",
]
