"""The horizon orchestrator: a deterministic step loop around a brain.

    while phase not terminal and not awaiting a human:
        state.step += 1
        request  = build_context(state)          # flat, one user turn
        decision = brain.step(request)            # <= 4 tool calls
        for call in decision:
            observe -> store raw -> compact -> card
            self-edit -> validated or rejected
            act -> ActionProposal -> ActionGate -> execute | approval | refused
        evict; account tokens; guard (code, never the model)
        checkpoint EVERY step; publish events

Crash safety comes from two rules. The checkpoint is the only state that
survives a step, so ``run`` after a ``kill -9`` resumes at exactly the saved
``(step, phase)``. And nothing that carries authority is ever checkpointed: a
``ValidatedAction`` lives only inside the step that minted it, so a resumed run
re-gates rather than replays (the same rule the LangGraph path follows).
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections import OrderedDict, deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Final, Protocol

from aegis.agents.horizon.compactor import Compactor
from aegis.agents.horizon.context import account, build_context, estimate_tokens, evict
from aegis.agents.horizon.events import EventBus
from aegis.agents.horizon.phases import (
    MIN_CONFIDENCE_TO_PLAN,
    VERIFY_TOOL,
    cycles_exhausted,
    exclude,
    guard_after_step,
    is_excluded,
    is_gap,
    move,
    recompute_confidences,
)
from aegis.agents.horizon.ports import (
    Brain,
    BrainDecision,
    BrainRequest,
    HorizonStore,
    IncidentMapRenderer,
    KnownIssueSearch,
    RawTreePort,
    RemoteToolset,
    ToolCall,
    ToolSpec,
)
from aegis.agents.horizon.tools_observe import (
    OBSERVE_TOOLS,
    PROPOSE_REMEDIATION,
    RAWTREE_PREFIX,
    EvidenceRecorder,
    HealthProbe,
    MemoryRecallPort,
    Observer,
    ServiceNaming,
    SustainedVerifier,
    ToolInvokerPort,
    build_proposal,
    resource_for,
)
from aegis.agents.horizon.tools_observe import (
    SPECS as ACT_SPECS,
)
from aegis.agents.horizon.tools_self_edit import SELF_EDIT_TOOLS, SelfEditor
from aegis.agents.horizon.tools_self_edit import SPECS as EDIT_SPECS
from aegis.agents.state import BudgetGuard
from aegis.core.clock import SYSTEM_CLOCK, Clock
from aegis.core.config import Settings
from aegis.core.errors import AegisError, DomainError, NotFoundError
from aegis.core.ids import ACTION, AGENT_RUN, VERIFICATION, new_id
from aegis.core.logging import get_logger
from aegis.domain.enums import (
    ActionState,
    ActionType,
    EvidenceType,
    IncidentState,
    PolicyEffect,
    Severity,
    SourceType,
)
from aegis.domain.horizon import (
    MAX_TOOL_CALLS_PER_STEP,
    PHASE_TO_INCIDENT_STATE,
    ActionAttempt,
    EvidenceCard,
    Goal,
    GoalStatus,
    HorizonEventType,
    HorizonHypothesis,
    HorizonPhase,
    HorizonState,
    MemoryCard,
    Source,
)
from aegis.domain.models import (
    ActionProposal,
    BlastRadius,
    Diagnosis,
    ExpectedEffect,
    ResourceRef,
    RollbackPlan,
    VerificationCheck,
    VerificationPlan,
    VerificationResult,
)
from aegis.domain.state_machines import allowed_incident_transitions
from aegis.execution.validated import GateRejection, ValidatedAction
from aegis.mcp import INVESTIGATION_SCOPES
from aegis.mcp.types import CallerIdentity, ToolContext

log = get_logger(__name__)

MAX_ACTIONS_KEPT: Final = 20
MAP_TIMEOUT_S: Final = 90.0
MAX_MAP_TASKS: Final = 4
MAX_CARD_CACHE: Final = 2_000
EXECUTION_SETTLE_S: Final = 10.0
_ESCAPES: Final = frozenset(
    {IncidentState.ESCALATED, IncidentState.BLOCKED, IncidentState.RESOLVED}
)

DEFAULT_GOALS: Final = (
    ("g_observe", "Establish what is failing and where"),
    ("g_cause", "Identify the root cause from evidence"),
    ("g_fix", "Remediate through the gate chain"),
    ("g_verify", "Prove sustained recovery"),
)


# --------------------------------------------------------------------------- #
# ports the orchestrator needs beyond ports.py                                  #
# --------------------------------------------------------------------------- #


class GatePort(Protocol):
    async def validate(
        self,
        proposal: ActionProposal,
        *,
        severity: Severity,
        diagnosis_confidence: float,
        has_abstained_diagnosis: bool,
        contradicting_evidence: int = 0,
        correlation_id: str = "",
        holder: str = "worker",
    ) -> ValidatedAction | GateRejection: ...


class ExecutionPort(Protocol):
    async def execute(
        self,
        validated: ValidatedAction,
        ports: Any,
        *,
        settle_seconds: float = 30.0,
        observation_window_s: int | None = None,
    ) -> Any: ...


class ActionReader(Protocol):
    async def require(self, action_id: str) -> Any: ...


class IncidentPort(Protocol):
    async def get(self, incident_id: str) -> Any: ...

    async def transition(
        self,
        incident_id: str,
        to_state: IncidentState,
        *,
        actor: str,
        reason: str = "",
        correlation_id: str | None = None,
    ) -> Any: ...


class MemoryWriter(Protocol):
    async def write(self, **kw: Any) -> Any: ...


@dataclass
class HorizonDeps:
    """Everything the loop drives. Optional fields degrade with a recorded gap."""

    settings: Settings
    store: HorizonStore
    bus: EventBus
    brain: Brain
    compactor: Compactor = field(default_factory=Compactor)
    tools: ToolInvokerPort | None = None  # mcp.ToolInvoker
    gate: GatePort | None = None  # execution.ActionGate
    execution: ExecutionPort | None = None  # execution.ExecutionService
    ports: Any = None  # execution.ExecutionPorts
    actions: ActionReader | None = None  # persistence.ActionRepository
    incidents: IncidentPort | None = None  # persistence.IncidentRepository
    evidence: EvidenceRecorder | None = None  # evidence.EvidenceStore
    memory_store: MemoryWriter | None = None  # memory.IncidentMemoryStore
    memory_recall: MemoryRecallPort | None = None  # memory.IncidentMemoryRecall
    rawtree: RawTreePort | None = None
    rawtree_tools: RemoteToolset | None = None
    known_issues: KnownIssueSearch | None = None
    incident_map: IncidentMapRenderer | None = None
    health_probe: HealthProbe | None = None
    clock: Clock = SYSTEM_CLOCK
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep
    execution_settle_s: float = EXECUTION_SETTLE_S


# --------------------------------------------------------------------------- #
# helpers                                                                      #
# --------------------------------------------------------------------------- #


def proposal_from_stored(action: Any) -> ActionProposal:
    """Rebuild the exact proposal a human approved, from the system of record.

    Mirrors the worker's reconstruction: the action that runs is the action that
    was shown, and the idempotency key carries across so re-gating cannot create
    a second action row.
    """
    return ActionProposal(
        id=action.id,
        incident_id=action.incident_id,
        action_type=action.action_type,
        target=ResourceRef(
            resource_type=action.resource_type,
            resource_id=action.resource_id,
            environment=action.environment,
            service_id=action.service_id,
        ),
        reason=action.reason,
        supporting_evidence=action.supporting_evidence,
        expected_effect=ExpectedEffect(**action.expected_effect),
        blast_radius=BlastRadius(**action.blast_radius),
        rollback=RollbackPlan(**action.rollback_plan) if action.rollback_plan else None,
        verification=VerificationPlan(**action.verification_plan),
        arguments=action.arguments,
        idempotency_key=action.idempotency_key,
        proposed_at=action.created_at,
    )


def _incident_path(src: IncidentState, dst: IncidentState) -> list[IncidentState]:
    """Shortest legal path, never routing *through* a terminal or escape state."""
    if src == dst:
        return []
    frontier: deque[tuple[IncidentState, list[IncidentState]]] = deque([(src, [])])
    seen = {src}
    while frontier:
        node, path = frontier.popleft()
        for nxt in allowed_incident_transitions(node):
            if nxt in seen:
                continue
            if nxt == dst:
                return [*path, nxt]
            if nxt in _ESCAPES or len(path) >= 3:
                continue
            seen.add(nxt)
            frontier.append((nxt, [*path, nxt]))
    return []


# Short, stable symptom classes. RawTree's ``action_success_rate`` groups
# verification outcomes by this key, so it must not drift with wording.
_SYMPTOM_CLASSES: Final = (
    ("pool", "pool_exhaustion"),
    ("connection", "pool_exhaustion"),
    ("oom", "memory_pressure"),
    ("memory", "memory_pressure"),
    ("latency", "latency"),
    ("p99", "latency"),
    ("error", "error_rate"),
)


def symptom_key(state: HorizonState) -> str:
    text = " ".join(
        [
            state.symptom,
            *(h.statement for h in state.hypotheses),
            *(c.claim for c in state.evidence),
        ]
    ).lower()
    return next((key for word, key in _SYMPTOM_CLASSES if word in text), "degraded")


def _approval_id(attempt: ActionAttempt) -> str | None:
    head, _, value = attempt.detail.partition("approval_id=")
    return value.split()[0] if value else None


# --------------------------------------------------------------------------- #
# orchestrator                                                                 #
# --------------------------------------------------------------------------- #


class HorizonOrchestrator:
    """Owns the loop. Stateless across processes: the store is the memory."""

    def __init__(self, deps: HorizonDeps) -> None:
        self._d = deps
        self._s = deps.settings
        self._observer = Observer(
            store=deps.store,
            compactor=deps.compactor,
            invoker=deps.tools,
            evidence=deps.evidence,
            rawtree=deps.rawtree,
            rawtree_tools=deps.rawtree_tools,
            known_issues=deps.known_issues,
            memory_recall=deps.memory_recall,
            clock=deps.clock,
        )
        self._editor = SelfEditor(deps.store)
        self._verifier = SustainedVerifier(
            deps.health_probe,
            required=self._s.verification_sustained_samples,
            interval_s=self._s.verification_sample_interval_s,
            sleep=deps.sleep,
        )
        self._cards: OrderedDict[str, EvidenceCard] = OrderedDict()
        self._map_tasks: set[asyncio.Task[None]] = set()
        self._approvers: dict[str, str] = {}

    # ---- public API ------------------------------------------------------- #

    async def run(self, incident_id: str, *, max_steps: int | None = None) -> HorizonState:
        """Start a run, or resume the latest checkpoint at its exact step and phase.

        ``max_steps`` bounds this call only (operations and tests); the hard
        per-incident cap is ``settings.horizon_max_steps``.
        """
        state = await self._d.store.load_latest(incident_id)
        if state is None:
            state = await self._start(incident_id)
        else:
            await self._emit(
                state,
                HorizonEventType.RESUMED,
                message=f"resumed at step {state.step} in {state.phase.value}",
                payload={"step": state.step, "phase": state.phase.value},
            )
        return await self._loop(state, max_steps)

    async def resume_after_approval(
        self, incident_id: str, action_id: str, approved: bool
    ) -> HorizonState:
        """Continue from the SAME checkpoint after a human decision.

        Approval authorises; it does not bypass. The stored proposal is re-gated
        from scratch - a ValidatedAction minted before the pause is never
        replayed, because its lease and approval may both have lapsed.
        """
        state = await self._d.store.load_latest(incident_id)
        if state is None:
            raise NotFoundError("no horizon run for incident", context={"incident_id": incident_id})
        if (
            state.phase is not HorizonPhase.AWAITING_APPROVAL
            or state.pending_action_id != action_id
        ):
            # A duplicate or stale decision job. Idempotent: nothing to do.
            log.info(
                "approval does not match the pending action; ignoring",
                incident_id=incident_id,
                action_id=action_id,
                pending=state.pending_action_id,
                phase=state.phase.value,
            )
            return state
        attempt = next((a for a in reversed(state.actions) if a.action_id == action_id), None)
        state.step += 1
        await self._emit(
            state,
            HorizonEventType.RESUMED,
            message=f"resumed at step {state.step} for an approval decision",
            payload={"step": state.step, "phase": state.phase.value},
        )
        await self._emit(
            state,
            HorizonEventType.APPROVAL_RESOLVED,
            message="approved" if approved else "denied",
            payload={
                "approval_id": _approval_id(attempt) if attempt else None,
                "action_id": action_id,
                "approved": approved,
            },
        )
        state.pending_action_id = None
        if attempt is None:
            await self._move(state, HorizonPhase.DIAGNOSING, "pending action not found in state")
        elif not approved:
            attempt.outcome = "denied"
            exclude(state, attempt)
            await self._move(state, HorizonPhase.DIAGNOSING, "a human denied the action")
        else:
            await self._regate_approved(state, attempt)
        await self._checkpoint(state)
        return await self._loop(state, None)

    async def drain_background(self, timeout_s: float = 30.0) -> None:
        """Wait (bounded) for fire-and-forget incident-map tasks. Tests and shutdown."""
        if self._map_tasks:
            await asyncio.wait(set(self._map_tasks), timeout=timeout_s)

    # ---- lifecycle ----------------------------------------------------------- #

    async def _start(self, incident_id: str) -> HorizonState:
        service, symptom, environment = "unknown", "", self._s.aegis_env.value
        workload = "default"
        if self._d.incidents is not None:
            try:
                incident = await self._d.incidents.get(incident_id)
                symptom = str(incident.title)
                workload = str(getattr(incident, "workload", "default") or "default")
                env = str(getattr(incident, "environment", "") or "")
                if env in {"local", "staging", "production"}:
                    environment = env
                services = list(getattr(incident, "affected_services", []) or [])
                origin = getattr(incident, "suspected_origin", None)
                service = str(services[0] if services else origin or "unknown")
            except AegisError as exc:
                log.warning(
                    "incident could not be read; starting blind",
                    incident_id=incident_id,
                    error=exc.code,
                )
        naming = ServiceNaming(environment, workload)
        now = self._d.clock.now()
        state = HorizonState(
            run_id=new_id(AGENT_RUN),
            incident_id=incident_id,
            service=naming.canonical(service),
            symptom=symptom[:300],
            phase=HorizonPhase.DETECTING,
            goals=[
                Goal(id=g, title=t, status=GoalStatus.ACTIVE if i == 0 else GoalStatus.PENDING)
                for i, (g, t) in enumerate(DEFAULT_GOALS)
            ],
            started_at=now,
            updated_at=now,
        )
        await self._emit(
            state,
            HorizonEventType.PHASE_CHANGED,
            message="run started",
            payload={"from": None, "to": state.phase.value},
        )
        # Detection is the alert itself; investigation starts deterministically.
        await self._move(state, HorizonPhase.INVESTIGATING, "alert received")
        await self._checkpoint(state)
        return state

    def _naming(self, state: HorizonState) -> ServiceNaming:
        parts = state.service.split(":")
        if len(parts) == 3:
            return ServiceNaming(parts[0], parts[1])
        return ServiceNaming(self._s.aegis_env.value, "default")

    def _budget(self, state: HorizonState) -> BudgetGuard:
        """Caps the brain cannot raise, sized to what is left of the step budget."""
        steps_left = max(1, self._s.horizon_max_steps - state.step)
        return BudgetGuard(
            max_wall_seconds=steps_left * self._s.horizon_step_timeout_s,
            max_llm_calls=steps_left,
            max_tool_calls=steps_left * (MAX_TOOL_CALLS_PER_STEP + 4),
            max_tokens=10**9,
            clock=self._d.clock,
        )

    async def _loop(self, state: HorizonState, max_steps: int | None) -> HorizonState:
        budget = self._budget(state)
        taken = 0
        while not state.phase.is_terminal and state.phase is not HorizonPhase.AWAITING_APPROVAL:
            if state.step >= self._s.horizon_max_steps:
                await self._escalate(state, f"step budget of {self._s.horizon_max_steps} exhausted")
                await self._checkpoint(state)
                break
            if max_steps is not None and taken >= max_steps:
                break
            await self._step(state, budget)
            taken += 1
        return state

    # ---- one step -------------------------------------------------------------- #

    async def _step(self, state: HorizonState, budget: BudgetGuard) -> None:
        state.step += 1
        start_phase = state.phase
        await self._emit(state, HorizonEventType.STEP_STARTED, message=f"step {state.step}")

        if start_phase is HorizonPhase.VERIFYING:
            await self._verify(state)
        elif start_phase is HorizonPhase.EXECUTING:
            # Only reachable on resume after a crash mid-execution. The gate's
            # idempotency key guarantees the action is not run twice; whether it
            # worked is for verification to say.
            await self._move(state, HorizonPhase.VERIFYING, "resumed after execution")
        else:
            await self._think(state, budget)

        state.updated_at = self._d.clock.now()
        await self._checkpoint(state)
        await self._emit(
            state,
            HorizonEventType.STEP_COMPLETED,
            message=f"step {state.step} done",
            payload={
                "context_tokens": state.tokens.context_tokens,
                "naive_tokens": state.tokens.naive_tokens,
            },
        )

    def _phase_tools(self, phase: HorizonPhase) -> list[ToolSpec]:
        edits = [EDIT_SPECS[n] for n in sorted(EDIT_SPECS)]
        observe = self._observer.specs()
        if phase is HorizonPhase.PLANNING:
            return [ACT_SPECS[PROPOSE_REMEDIATION], *edits, *observe]
        return [*observe, *edits]

    async def _decide(self, request: BrainRequest, state: HorizonState) -> BrainDecision:
        started = time.perf_counter()
        try:
            return await asyncio.wait_for(
                self._d.brain.step(request, state), timeout=self._s.horizon_step_timeout_s
            )
        except TimeoutError:
            reason = f"brain did not answer within {self._s.horizon_step_timeout_s:.0f}s"
        except Exception as exc:  # noqa: BLE001 - the Brain contract says never raises
            reason = f"brain raised {type(exc).__name__}"
        log.warning(
            "brain step failed; taking no action this step",
            incident_id=state.incident_id,
            reason=reason,
        )
        return BrainDecision(
            tool_calls=(),
            source=Source.SYSTEM,
            fallback_reason=reason,
            latency_ms=int((time.perf_counter() - started) * 1000),
        )

    async def _think(self, state: HorizonState, budget: BudgetGuard) -> None:
        tools = self._phase_tools(state.phase)
        offered = {t.name for t in tools}
        request = build_context(state, tools)
        decision = await self._decide(request, state)
        budget.charge_llm(input_tokens=decision.input_tokens, output_tokens=decision.output_tokens)
        if decision.source in (Source.BEDROCK, Source.GEMINI, Source.SCRIPTED):
            state.brain_source = decision.source
        state.tokens.cache_read_tokens += decision.cache_read_tokens
        if decision.cache_read_tokens:
            state.tokens.cache_hits += 1
        if decision.fallback_reason:
            state.tokens.fallbacks_used += 1

        raw_tokens = 0
        context = self._tool_context(state, budget)
        naming = self._naming(state)
        start_phase = state.phase
        calls = decision.tool_calls[:MAX_TOOL_CALLS_PER_STEP]
        for call in calls:
            if state.phase is not start_phase:
                break  # an act moved the phase; the rest of the plan is stale
            if call.name not in offered:
                await self._reject(state, call.name, "tool is not offered in this phase")
                continue
            if call.name in SELF_EDIT_TOOLS:
                await self._self_edit(state, call)
            elif call.name == PROPOSE_REMEDIATION:
                await self._propose(state, call, naming)
            else:
                raw_tokens += await self._observe(state, call, naming, context)

        for card in evict(state):
            self._cache(card)
            await self._emit(
                state,
                HorizonEventType.EVIDENCE_DISCARDED,
                message=f"{card.id} left context (raw kept)",
                payload={"evidence_id": card.id, "by": "eviction"},
            )

        decision_tokens = decision.output_tokens or estimate_tokens(
            repr([(c.name, c.arguments) for c in calls])
        )
        account(state, request, raw_tokens=raw_tokens, decision_tokens=decision_tokens)
        recompute_confidences(state, list((await self._known_cards(state)).values()))
        await self._emit(
            state,
            HorizonEventType.BRAIN_DECISION,
            source=decision.source,
            message=decision.text[:200],
            payload={
                "model": decision.model,
                "tools": [c.name for c in calls],
                "stop_reason": decision.stop_reason,
                "latency_ms": decision.latency_ms,
                "input_tokens": decision.input_tokens,
                "output_tokens": decision.output_tokens,
                "cache_read_tokens": decision.cache_read_tokens,
            },
        )
        if decision.fallback_reason:
            await self._emit(
                state,
                HorizonEventType.BRAIN_FALLBACK,
                source=decision.source,
                status="degraded",
                message=decision.fallback_reason[:200],
                payload={"reason": decision.fallback_reason[:300]},
            )

        if state.phase is start_phase:
            target, why = guard_after_step(state)
            if target is not state.phase:
                await self._move(state, target, why or "guard satisfied")

    def _tool_context(self, state: HorizonState, budget: BudgetGuard) -> ToolContext:
        env = self._naming(state).environment
        return ToolContext(
            environment=env
            if env in {"local", "staging", "production"}
            else self._s.aegis_env.value,
            caller=CallerIdentity(
                subject="agent:horizon", actor_type="agent", scopes=INVESTIGATION_SCOPES
            ),
            budget=budget,
            deadline=self._d.clock.now() + timedelta(seconds=self._s.horizon_step_timeout_s),
            correlation_id=state.run_id,
            incident_id=state.incident_id,
        )

    # ---- tool families ------------------------------------------------------- #

    async def _observe(
        self, state: HorizonState, call: ToolCall, naming: ServiceNaming, context: ToolContext
    ) -> int:
        obs = await self._observer.observe(call.name, dict(call.arguments), state, naming, context)
        card = obs.card
        self._cache(card)
        state.evidence = [c for c in state.evidence if c.id != card.id] + [card]
        state.tokens.compacted_raw_tokens += card.tokens_raw
        state.tokens.compacted_card_tokens += card.tokens_card
        if (
            call.name in OBSERVE_TOOLS or call.name.startswith(RAWTREE_PREFIX)
        ) and call.name not in state.observe_tools_run:
            state.observe_tools_run = [*state.observe_tools_run, call.name]
        await self._emit(
            state,
            HorizonEventType.TOOL_CALLED,
            tool=call.name,
            status="degraded" if obs.status == "gap" else "ok",
            source=obs.origin,
            duration_ms=obs.duration_ms,
            message=f"{call.name} -> {obs.status}",
            payload={
                "arguments": dict(call.arguments),
                "status": obs.status,
                "evidence_id": card.id,
            },
        )
        await self._emit(
            state,
            HorizonEventType.EVIDENCE_ADDED,
            tool=call.name,
            source=card.source,
            status="degraded" if obs.status == "gap" else "ok",
            message=card.claim[:200],
            payload={"card": card.model_dump(mode="json")},
        )
        if obs.memory:
            state.memory = obs.memory[:3]
            await self._emit(
                state,
                HorizonEventType.MEMORY_RECALLED,
                source=Source.MEMORY,
                message=f"{len(obs.memory)} similar incidents recalled",
                payload={"cards": [m.model_dump(mode="json") for m in obs.memory]},
            )
        if obs.query is not None:
            q = obs.query
            await self._emit(
                state,
                HorizonEventType.RAWTREE_QUERY,
                tool=call.name,
                source=q.source,
                status="error" if q.error else "ok",
                duration_ms=q.duration_ms,
                message=q.name,
                payload={
                    "name": q.name,
                    "sql": q.sql[:2000],
                    "rows": len(q.rows),
                    "source": q.source.value,
                    "duration_ms": q.duration_ms,
                    "error": q.error,
                },
            )
        return obs.tokens_raw

    async def _self_edit(self, state: HorizonState, call: ToolCall) -> None:
        result = await self._editor.apply(
            call.name, dict(call.arguments), state, await self._known_cards(state)
        )
        await self._emit(
            state,
            result.event_type,
            tool=call.name,
            status="ok" if result.ok else "rejected",
            message=result.message,
            payload=result.payload,
        )

    async def _reject(self, state: HorizonState, tool: str, why: str, **payload: Any) -> None:
        await self._emit(
            state,
            HorizonEventType.SELF_EDIT_REJECTED,
            tool=tool,
            status="rejected",
            message=f"{tool} rejected: {why}",
            payload={"tool": tool, "reason": why, **payload},
        )

    # ---- act ------------------------------------------------------------------- #

    async def _propose(self, state: HorizonState, call: ToolCall, naming: ServiceNaming) -> None:
        args = dict(call.arguments)
        raw_type = str(args.get("action_type", ""))
        try:
            action_type = ActionType(raw_type)
        except ValueError:
            await self._reject(state, PROPOSE_REMEDIATION, f"unknown action type {raw_type[:40]!r}")
            return
        hyp = next((h for h in state.hypotheses if h.id == args.get("hypothesis_id")), None)
        if hyp is None:
            await self._reject(state, PROPOSE_REMEDIATION, "hypothesis_id does not exist")
            return
        known = await self._known_cards(state)
        cited = [str(i) for i in args.get("evidence_ids") or []]
        unknown = [i for i in cited if i not in known]
        gaps = [i for i in cited if i in known and is_gap(known[i])]
        if not cited or unknown or gaps:
            await self._reject(
                state,
                PROPOSE_REMEDIATION,
                "evidence must be existing, non-gap card ids",
                unknown=unknown[:8],
                gaps=gaps[:8],
            )
            return
        if self._d.gate is None or self._d.execution is None or self._d.ports is None:
            # No write path is wired. Refusing before gating means no lease is
            # taken for an action that could never run.
            await self._reject(
                state,
                PROPOSE_REMEDIATION,
                "no action gate / execution path is configured in this process",
            )
            return

        raw_args = args.get("arguments") or {}
        arguments: dict[str, str | int | float | bool] = {
            k: v
            for k, v in (raw_args.items() if isinstance(raw_args, dict) else [])
            if v is not None and isinstance(v, str | int | float | bool)
        }
        service_id = naming.canonical(str(args.get("target") or state.service))
        instance_id = None
        if action_type in (ActionType.RESTART_INSTANCE, ActionType.DRAIN_INSTANCE):
            instance_id = await self._observer.instance_for(
                state, service_id, naming, self._tool_context(state, self._budget(state))
            )
        target = resource_for(
            action_type,
            service_id=service_id,
            instance_id=instance_id,
            environment=naming.environment,
        )
        attempt = ActionAttempt(
            action_type=action_type.value,
            target=target.resource_id,
            arguments=arguments,
            cycle=state.remediation_cycle + 1,
        )
        if is_excluded(state, attempt):
            await self._reject(
                state,
                PROPOSE_REMEDIATION,
                "an identical action already failed or was denied",
                signature=attempt.signature,
            )
            return

        recompute_confidences(state, list(known.values()))
        proposal = build_proposal(
            incident_id=state.incident_id,
            action_type=action_type,
            target=target,
            arguments=arguments,
            evidence_ids=cited,
            statement=hyp.statement,
            signature=attempt.signature,
            proposed_at=self._d.clock.now(),
            action_id=new_id(ACTION),
        )
        await self._emit(
            state,
            HorizonEventType.ACTION_PROPOSED,
            tool=PROPOSE_REMEDIATION,
            message=f"{action_type.value} on {target.resource_id}",
            payload={
                "action_type": action_type.value,
                "target": target.resource_id,
                "arguments": arguments,
                "hypothesis_id": hyp.id,
                "confidence": hyp.confidence,
                "evidence_ids": cited,
            },
        )
        await self._gate(state, proposal, attempt, hyp)

    async def _severity(self, incident_id: str) -> Severity:
        if self._d.incidents is None:
            return Severity.P2
        try:
            return Severity((await self._d.incidents.get(incident_id)).severity)
        except (AegisError, ValueError):
            return Severity.P2

    async def _gate(
        self,
        state: HorizonState,
        proposal: ActionProposal,
        attempt: ActionAttempt,
        hyp: HorizonHypothesis,
    ) -> None:
        assert self._d.gate is not None
        known = await self._known_cards(state)
        try:
            outcome = await self._d.gate.validate(
                proposal,
                severity=await self._severity(state.incident_id),
                diagnosis_confidence=hyp.confidence,
                has_abstained_diagnosis=hyp.confidence < MIN_CONFIDENCE_TO_PLAN,
                contradicting_evidence=sum(1 for i in hyp.refuting if i in known),
                correlation_id=state.run_id,
                holder=f"horizon:{state.incident_id}",
            )
        except AegisError as exc:
            # A fault (fabricated citation, malformed proposal, lost lease race)
            # is a refusal with a reason, never a policy decision.
            attempt.outcome, attempt.detail = "refused", f"{exc.code}: {exc.message}"[:300]
            self._record_attempt(state, attempt)
            exclude(state, attempt)
            await self._emit(
                state,
                HorizonEventType.ACTION_PROPOSED,
                status="rejected",
                message=f"gate refused: {exc.code}",
                payload={"reason": exc.message[:300], "code": exc.code},
            )
            return
        await self._handle_gate_outcome(state, outcome, attempt, hyp, proposal)

    async def _handle_gate_outcome(
        self,
        state: HorizonState,
        outcome: ValidatedAction | GateRejection,
        attempt: ActionAttempt,
        hyp: HorizonHypothesis | None,
        proposal: ActionProposal,
    ) -> None:
        if isinstance(outcome, ValidatedAction):
            attempt.action_id = outcome.action.id
            if outcome.approval is not None and outcome.approval.decided_by:
                self._approvers[outcome.action.id] = outcome.approval.decided_by
            self._record_attempt(state, attempt)
            await self._move(
                state,
                HorizonPhase.EXECUTING,
                "human approval re-gated"
                if outcome.approval
                else "policy allowed autonomous execution",
            )
            await self._execute(state, attempt, outcome)
            return

        attempt.action_id = outcome.action_id
        if outcome.needs_approval:
            attempt.outcome = "awaiting_approval"
            attempt.detail = f"approval_id={outcome.approval_id} rule={outcome.matched_rule}"
            self._record_attempt(state, attempt)
            state.pending_action_id = outcome.action_id
            await self._move(state, HorizonPhase.AWAITING_APPROVAL, outcome.summary)
            await self._emit(
                state,
                HorizonEventType.APPROVAL_REQUIRED,
                tool=PROPOSE_REMEDIATION,
                message=outcome.summary,
                payload={
                    "approval_id": outcome.approval_id,
                    "action_id": outcome.action_id,
                    "action_type": attempt.action_type,
                    "target": attempt.target,
                    "reason": (hyp.statement if hyp else proposal.reason)[:300],
                    "confidence": hyp.confidence if hyp else None,
                    "evidence_ids": list(proposal.supporting_evidence),
                    "risk_tier": int(outcome.risk_tier),
                    "policy": {
                        "matched_rule": outcome.matched_rule,
                        "reasons": outcome.reasons[:8],
                    },
                },
            )
            return
        if (
            outcome.matched_rule == "already_in_progress"
            and outcome.effect is not PolicyEffect.BLOCK
        ):
            # The action already ran (or is running) under an earlier process -
            # a crash between execution and checkpoint. Do not run it again;
            # verify what the world looks like now.
            attempt.outcome, attempt.detail = "executed", "resumed: action already progressed"
            self._record_attempt(state, attempt)
            await self._move(state, HorizonPhase.EXECUTING, "action already progressed")
            await self._move(state, HorizonPhase.VERIFYING, "verify the action that already ran")
            return
        attempt.outcome = "refused"
        attempt.detail = f"{outcome.effect.value}: {outcome.matched_rule}"[:300]
        self._record_attempt(state, attempt)
        exclude(state, attempt)
        await self._emit(
            state,
            HorizonEventType.ACTION_PROPOSED,
            status="rejected",
            message=outcome.summary,
            payload={
                "matched_rule": outcome.matched_rule,
                "reasons": outcome.reasons[:8],
                "risk_tier": int(outcome.risk_tier),
            },
        )

    async def _regate_approved(self, state: HorizonState, attempt: ActionAttempt) -> None:
        if (
            self._d.gate is None
            or self._d.actions is None
            or self._d.execution is None
            or self._d.ports is None
        ):
            attempt.outcome, attempt.detail = "refused", "no write path configured for re-gating"
            await self._move(state, HorizonPhase.DIAGNOSING, attempt.detail)
            return
        assert attempt.action_id is not None
        stored = await self._d.actions.require(attempt.action_id)
        proposal = proposal_from_stored(stored)
        known = await self._known_cards(state)
        recompute_confidences(state, list(known.values()))
        hyp = max(
            (h for h in state.hypotheses if h.suggested_action == attempt.action_type),
            key=lambda h: h.confidence,
            default=state.top_hypothesis(),
        )
        try:
            outcome = await self._d.gate.validate(
                proposal,
                severity=await self._severity(state.incident_id),
                diagnosis_confidence=hyp.confidence if hyp else 0.0,
                has_abstained_diagnosis=hyp is None or hyp.confidence < MIN_CONFIDENCE_TO_PLAN,
                contradicting_evidence=sum(1 for i in (hyp.refuting if hyp else []) if i in known),
                correlation_id=state.run_id,
                holder=f"horizon:{state.incident_id}",
            )
        except AegisError as exc:
            attempt.outcome, attempt.detail = "refused", f"re-gate: {exc.code}"
            exclude(state, attempt)
            await self._move(
                state, HorizonPhase.DIAGNOSING, f"approved action refused on re-gate: {exc.code}"
            )
            return
        if (
            isinstance(outcome, GateRejection)
            and not outcome.needs_approval
            and not (
                outcome.matched_rule == "already_in_progress"
                and outcome.effect is not PolicyEffect.BLOCK
            )
        ):
            attempt.outcome, attempt.detail = "refused", f"re-gate: {outcome.matched_rule}"
            exclude(state, attempt)
            await self._move(
                state,
                HorizonPhase.DIAGNOSING,
                f"approved action refused on re-gate: {outcome.matched_rule}",
            )
            return
        # Replace the stored attempt entry rather than appending a duplicate.
        state.actions = [a for a in state.actions if a is not attempt]
        await self._handle_gate_outcome(state, outcome, attempt, hyp, proposal)

    def _record_attempt(self, state: HorizonState, attempt: ActionAttempt) -> None:
        if attempt not in state.actions:
            state.actions = [*state.actions, attempt][-MAX_ACTIONS_KEPT:]

    async def _execute(
        self, state: HorizonState, attempt: ActionAttempt, validated: ValidatedAction
    ) -> None:
        assert self._d.execution is not None
        state.remediation_cycle += 1
        attempt.cycle = state.remediation_cycle
        try:
            report = await self._d.execution.execute(
                validated, self._d.ports, settle_seconds=self._d.execution_settle_s
            )
            attempt.outcome = "executed"
            attempt.detail = (
                f"{getattr(report, 'final_state', ActionState.FAILED)}"
                f" {getattr(report, 'escalation_reason', '')}"
            )[:300]
            payload = report.as_json() if hasattr(report, "as_json") else {}
            status = "ok" if getattr(report, "executed", False) else "error"
        except AegisError as exc:
            attempt.outcome, attempt.detail = "executed", f"execution error {exc.code}"
            payload, status = {"error": exc.code}, "error"
        await self._emit(
            state,
            HorizonEventType.ACTION_EXECUTED,
            status=status,
            message=f"{attempt.action_type} on {attempt.target}",
            payload={"action_id": attempt.action_id, **payload},
        )
        await self._move(state, HorizonPhase.VERIFYING, "execution finished; verifying")

    # ---- verify --------------------------------------------------------------- #

    async def _verify(self, state: HorizonState) -> None:
        attempt = next((a for a in reversed(state.actions) if a.outcome == "executed"), None)
        service = ServiceNaming.bare(state.service)
        result = await self._verifier.verify(service)
        card = await self._observer.record_card(
            state,
            tool=VERIFY_TOOL,
            claim_raw=result.summary,
            structured={
                "kind": "verification",
                "summary": result.summary,
                "passed": result.passed,
                "required": result.required,
                "samples": [s.describe() for s in result.samples],
                "action_id": attempt.action_id if attempt else None,
            },
            origin=Source.PROMETHEUS if self._d.health_probe is not None else Source.SYSTEM,
            source_type=SourceType.METRICS,
            evidence_type=EvidenceType.METRIC_COMPARISON,
            weight=0.85,
        )
        card = card.model_copy(update={"pinned": True})
        self._cache(card)
        state.evidence = [*state.evidence, card]
        await self._emit(
            state,
            HorizonEventType.EVIDENCE_ADDED,
            tool=VERIFY_TOOL,
            source=card.source,
            message=card.claim[:200],
            payload={"card": card.model_dump(mode="json")},
        )
        await self._emit(
            state,
            HorizonEventType.VERIFICATION_RESULT,
            tool=VERIFY_TOOL,
            status="ok" if result.passed else "error",
            message=result.summary[:300],
            payload={
                "passed": result.passed,
                "required": result.required,
                "outcomes": result.outcomes,
                "samples": [s.describe() for s in result.samples],
                "action_id": attempt.action_id if attempt else None,
                "action_type": attempt.action_type if attempt else None,
                "target": attempt.target if attempt else None,
                "symptom": symptom_key(state),
                "verified": result.passed,
                "recovery_s": (
                    (self._d.clock.now() - state.started_at).total_seconds()
                    if result.passed and state.started_at
                    else None
                ),
                "evidence_id": card.id,
            },
        )
        if result.passed and attempt is not None:
            attempt.outcome = "verified"
            await self._move(state, HorizonPhase.RESOLVED, "sustained recovery verified")
            await self._resolve(state, attempt, result.summary)
            return
        if attempt is not None:
            attempt.outcome = "failed"
            exclude(state, attempt)
        if cycles_exhausted(state):
            await self._escalate(
                state, f"{state.remediation_cycle} remediation cycles did not verify"
            )
        else:
            await self._move(state, HorizonPhase.REASSESSING, "verification failed")

    async def _escalate(self, state: HorizonState, reason: str) -> None:
        state.escalation_reason = reason[:300]
        await self._move(state, HorizonPhase.ESCALATED, reason)
        await self._emit(
            state,
            HorizonEventType.ESCALATED,
            status="error",
            message=reason[:300],
            payload={"reason": reason[:300]},
        )

    # ---- resolution and memory ---------------------------------------------------- #

    async def _resolve(self, state: HorizonState, attempt: ActionAttempt, summary: str) -> None:
        await self._emit(
            state,
            HorizonEventType.RESOLVED,
            message=summary[:300],
            payload={"action_type": attempt.action_type, "target": attempt.target},
        )
        hyp = max(
            (h for h in state.hypotheses if h.suggested_action == attempt.action_type),
            key=lambda h: h.confidence,
            default=state.top_hypothesis(),
        )
        failed = [a.action_type for a in state.actions if a.outcome == "failed"]
        now = self._d.clock.now()
        card = MemoryCard(
            id=f"mc_{state.incident_id}",
            incident_id=state.incident_id,
            symptoms=(state.symptom or ServiceNaming.bare(state.service))[:200],
            root_cause=(hyp.statement if hyp else "undetermined")[:200],
            failed_actions=failed[:4],
            successful_action=attempt.action_type,
            recovery_s=(now - state.started_at).total_seconds() if state.started_at else None,
            lesson=(
                f"{'/'.join(failed)} did not hold; {attempt.action_type} verified"
                if failed
                else f"{attempt.action_type} verified"
            )[:200],
            image_status="pending" if self._d.incident_map is not None else "unavailable",
            image_reason=""
            if self._d.incident_map is not None
            else "incident map renderer not configured",
        )
        await self._write_incident_memory(state, attempt, hyp, summary)
        await self._d.store.save_memory_card(card)
        if self._d.rawtree is not None:
            try:
                self._d.rawtree.enqueue_memory_card(card)
            except Exception as exc:  # noqa: BLE001 - the port promises not to raise
                log.warning("rawtree enqueue_memory_card failed", error=type(exc).__name__)
        await self._emit(
            state,
            HorizonEventType.MEMORY_CARD_WRITTEN,
            source=Source.MEMORY,
            message=card.lesson,
            payload={"card": card.model_dump(mode="json")},
        )
        if self._d.incident_map is not None:
            self._spawn_map(state.model_copy(deep=True), card)

    async def _write_incident_memory(
        self,
        state: HorizonState,
        attempt: ActionAttempt,
        hyp: HorizonHypothesis | None,
        summary: str,
    ) -> None:
        """Through the existing strict write path. Its refusals stand."""
        if self._d.memory_store is None or hyp is None:
            return
        now = self._d.clock.now()
        approver = self._approvers.get(attempt.action_id or "", "")
        if not approver:
            approver = "human" if "approval_id=" in attempt.detail else "system:autonomous"
        category = {
            ActionType.ROLLBACK_DEPLOYMENT.value: "deploy_regression",
            ActionType.RESTART_INSTANCE.value: "resource_exhaustion",
        }.get(attempt.action_type, "unclassified")
        try:
            await self._d.memory_store.write(
                diagnosis=Diagnosis(
                    incident_id=state.incident_id,
                    abstained=False,
                    statement=hyp.statement,
                    root_cause_category=category,
                    confidence=hyp.confidence,
                    supporting_evidence=list(hyp.supporting),
                    affected_services=[state.service],
                ),
                verification=VerificationResult(
                    id=new_id(VERIFICATION),
                    incident_id=state.incident_id,
                    action_id=attempt.action_id,
                    passed=True,
                    checks=[
                        VerificationCheck(
                            name="sustained_window", passed=True, detail=summary[:300]
                        )
                    ],
                    started_at=now,
                    completed_at=now,
                    notes=summary[:300],
                ),
                title=(state.symptom or state.service)[:200],
                symptoms=(state.symptom or state.service)[:300],
                successful_fix=f"{attempt.action_type} {attempt.target}",
                approved_by=approver,
                cause_category=category,
                failed_attempts=[
                    f"{a.action_type} {a.target}" for a in state.actions if a.outcome == "failed"
                ],
            )
        except AegisError as exc:
            log.info(
                "incident memory declined the write",
                incident_id=state.incident_id,
                code=exc.code,
                reason=exc.message,
            )

    def _spawn_map(self, snapshot: HorizonState, card: MemoryCard) -> None:
        if len(self._map_tasks) >= MAX_MAP_TASKS:
            log.warning("incident map skipped: too many renders in flight", card_id=card.id)
            return
        task = asyncio.create_task(self._render_map(snapshot, card))
        self._map_tasks.add(task)
        task.add_done_callback(self._map_tasks.discard)

    async def _render_map(self, state: HorizonState, card: MemoryCard) -> None:
        assert self._d.incident_map is not None
        status, reason, url = "unavailable", "", None
        try:
            result = await asyncio.wait_for(self._d.incident_map.render(state, card), MAP_TIMEOUT_S)
            reason = result.reason
            if result.status == "ready" and result.image_bytes:
                url = await self._d.store.save_incident_map(
                    card.id, result.image_bytes, result.mime
                )
                status = "ready"
            elif result.status == "ready" and result.image_url:
                url, status = result.image_url, "ready"
            source = result.source
        except TimeoutError:
            reason, source = f"render exceeded {MAP_TIMEOUT_S:.0f}s", Source.SYSTEM
        except Exception as exc:  # noqa: BLE001 - a picture never fails an incident
            reason, source = f"render failed: {type(exc).__name__}", Source.SYSTEM
        updated = card.model_copy(
            update={"image_url": url, "image_status": status, "image_reason": reason[:300]}
        )
        with contextlib.suppress(AegisError):
            await self._d.store.save_memory_card(updated)
        await self._emit(
            state,
            HorizonEventType.INCIDENT_MAP,
            source=source,
            status="ok" if status == "ready" else "degraded",
            message=f"incident map {status}",
            payload={
                "card_id": card.id,
                "status": status,
                "reason": reason[:300],
                "image_url": url,
            },
        )

    # ---- plumbing ----------------------------------------------------------------- #

    def _cache(self, card: EvidenceCard) -> None:
        self._cards[card.id] = card
        self._cards.move_to_end(card.id)
        while len(self._cards) > MAX_CARD_CACHE:
            self._cards.popitem(last=False)

    async def _known_cards(self, state: HorizonState) -> dict[str, EvidenceCard]:
        """Cards in context plus evicted ones (from cache, else from storage)."""
        known = {c.id: c for c in state.evidence}
        for eid in state.discarded:
            if eid in known:
                continue
            card = self._cards.get(eid)
            if card is None:
                obs = await self._d.store.get_observation(eid)
                card = obs.card if obs is not None else None
                if card is not None:
                    self._cache(card)
            if card is not None:
                known[eid] = card
        return known

    async def _move(self, state: HorizonState, dst: HorizonPhase, why: str) -> None:
        src = state.phase
        if not move(state, dst):
            return
        await self._emit(
            state,
            HorizonEventType.PHASE_CHANGED,
            message=why[:300],
            payload={"from": src.value, "to": dst.value, "reason": why[:300]},
        )
        await self._mirror(state, why)

    async def _mirror(self, state: HorizonState, why: str) -> None:
        """Reflect the phase on the incident lifecycle, skipping illegal hops."""
        target = PHASE_TO_INCIDENT_STATE.get(state.phase)
        if target is None or self._d.incidents is None:
            return
        try:
            current = IncidentState((await self._d.incidents.get(state.incident_id)).state)
            for hop in _incident_path(current, target):
                await self._d.incidents.transition(
                    state.incident_id,
                    hop,
                    actor="agent:horizon",
                    reason=why[:200],
                    correlation_id=state.run_id,
                )
        except (DomainError, NotFoundError, ValueError) as exc:
            log.info(
                "incident mirror skipped",
                incident_id=state.incident_id,
                target=target.value,
                reason=str(exc)[:200],
            )

    async def _checkpoint(self, state: HorizonState) -> None:
        await self._d.store.save_checkpoint(state)
        await self._emit(
            state,
            HorizonEventType.CHECKPOINT_SAVED,
            message=f"checkpoint step {state.step}",
            payload={"step": state.step, "phase": state.phase.value},
        )

    async def _emit(self, state: HorizonState, event_type: HorizonEventType, **kw: Any) -> int:
        return await self._d.bus.publish(state, event_type, **kw)


__all__ = ["HorizonDeps", "HorizonOrchestrator", "proposal_from_stored", "symptom_key"]
