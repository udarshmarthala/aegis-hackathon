"""Workflow state and budget enforcement.

Two rules shape this module:

1. **State holds references, not payloads.** Evidence bodies live in Postgres;
   the graph state carries ids and short summaries. Otherwise every checkpoint
   would grow with the investigation and eventually exceed what can be written.

2. **Agents cannot widen their own limits.** ``BudgetGuard`` is owned by the
   orchestrator. Nodes receive a read-only ``BudgetView``, so there is no
   reachable method that increases a cap (AIArchitecture 38).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Annotated, Any, TypedDict

from aegis.core.clock import SYSTEM_CLOCK, Clock
from aegis.core.errors import BudgetExhausted
from aegis.core.logging import get_logger

log = get_logger(__name__)


def merge_lists(left: list[Any], right: list[Any]) -> list[Any]:
    """Reducer for parallel nodes.

    Fan-out branches each append their findings; LangGraph merges with this
    rather than last-write-wins, so no investigator can erase another's results.
    """
    return [*left, *right]


class IncidentState(TypedDict, total=False):
    """The orchestrator's working memory."""

    incident_id: str
    correlation_id: str
    environment: str
    workload: str
    severity: str
    phase: str
    title: str

    affected_services: list[str]
    candidate_services: list[str]

    # References only - never raw evidence bodies.
    evidence_ids: Annotated[list[str], merge_lists]
    evidence_summaries: Annotated[list[dict[str, Any]], merge_lists]
    evidence_gaps: Annotated[list[dict[str, str]], merge_lists]

    topology: dict[str, Any]
    changes: list[dict[str, Any]]
    history: list[dict[str, Any]]

    hypotheses: list[dict[str, Any]]
    selected_hypothesis_id: str | None
    confidence: float
    diagnosis: dict[str, Any] | None
    abstained: bool

    loop_count: int
    remediation: dict[str, Any] | None
    proposed_action: dict[str, Any] | None
    policy_decision: dict[str, Any] | None
    awaiting_approval: bool
    # The deterministic verification engine's verdict for the executed action.
    # Absent means no action was executed; None means one was and the engine
    # reached no verdict. Those are different facts and readers must be able to
    # tell them apart, so neither is ever written as a success.
    verification_verdict: str | None
    # "validated" once the investigation-side grounding gate has run, "skipped"
    # when a benchmark ablation removed it. Absent when diagnosis never got far
    # enough for the question to arise.
    evidence_verification: str

    errors: Annotated[list[dict[str, str]], merge_lists]
    budget: dict[str, Any]
    finished: bool


@dataclass(frozen=True, slots=True)
class BudgetView:
    """What a node is allowed to see. No mutators, by construction."""

    llm_calls_remaining: int
    tool_calls_remaining: int
    tokens_remaining: int
    seconds_remaining: float

    @property
    def exhausted(self) -> bool:
        return (
            self.llm_calls_remaining <= 0
            or self.tool_calls_remaining <= 0
            or self.tokens_remaining <= 0
            or self.seconds_remaining <= 0
        )


@dataclass
class BudgetGuard:
    """Central enforcement. Owned by the orchestrator, never handed to a node."""

    max_wall_seconds: float
    max_llm_calls: int
    max_tool_calls: int
    max_tokens: int
    clock: Clock = field(default=SYSTEM_CLOCK)

    _started: float = field(default=0.0, init=False)
    _llm_calls: int = field(default=0, init=False)
    _tool_calls: int = field(default=0, init=False)
    _tokens: int = field(default=0, init=False)

    def __post_init__(self) -> None:
        self._started = self.clock.monotonic()

    @property
    def elapsed(self) -> float:
        return self.clock.monotonic() - self._started

    def view(self) -> BudgetView:
        return BudgetView(
            llm_calls_remaining=max(0, self.max_llm_calls - self._llm_calls),
            tool_calls_remaining=max(0, self.max_tool_calls - self._tool_calls),
            tokens_remaining=max(0, self.max_tokens - self._tokens),
            seconds_remaining=max(0.0, self.max_wall_seconds - self.elapsed),
        )

    def check(self, what: str) -> None:
        """Called on entry to every node. Raises when any dimension is spent."""
        v = self.view()
        if not v.exhausted:
            return
        reason = (
            "wall clock" if v.seconds_remaining <= 0
            else "llm calls" if v.llm_calls_remaining <= 0
            else "tool calls" if v.tool_calls_remaining <= 0
            else "tokens"
        )
        log.warning("budget exhausted", node=what, dimension=reason,
                    elapsed_s=round(self.elapsed, 1))
        raise BudgetExhausted(
            f"budget exhausted ({reason}) before {what}",
            context={"dimension": reason, "node": what},
        )

    def charge_llm(self, *, input_tokens: int = 0, output_tokens: int = 0) -> None:
        self._llm_calls += 1
        self._tokens += input_tokens + output_tokens

    def charge_tool(self) -> None:
        self._tool_calls += 1

    def snapshot(self) -> dict[str, Any]:
        """Persisted with the incident so budget use is auditable per run."""
        return {
            "llm_calls": self._llm_calls,
            "tool_calls": self._tool_calls,
            "tokens": self._tokens,
            "elapsed_s": round(self.elapsed, 2),
            "max_wall_seconds": self.max_wall_seconds,
            "max_llm_calls": self.max_llm_calls,
            "max_tool_calls": self.max_tool_calls,
            "max_tokens": self.max_tokens,
        }
