"""The scripted brain: the golden path with no network at all.

It calls the same tools a model would, through the same orchestrator, the same
guards and the same gate chain - only the choice of tool is fixed. It holds no
memory of its own: every decision is derived from ``HorizonState`` (the card ids
it cites are read from the state, never hard-coded), which is why a run resumed
after a crash continues exactly where it stopped.

Golden path (INC-043):

    observe metrics / instances / deployments / logs
    -> recall memory + hypotheses (pool exhaustion leads, bad deploy trails)
    -> propose restart_instance            (tier 1, policy allows)
    -> verification fails -> refute the restart hypothesis
       + search_known_issues + query_history(action_success_rate)
    -> hypotheses flip -> propose rollback_deployment to the prior version
       (tier 2, policy requires a human) -> approved -> verified -> resolved
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Final

from aegis.agents.horizon.phases import VERIFY_TOOL, is_gap
from aegis.agents.horizon.ports import BrainDecision, BrainRequest, ToolCall
from aegis.agents.horizon.tools_observe import (
    CONTAINER_LOGS,
    DEPLOYMENT_HISTORY,
    INSTANCE_STATUS,
    OBSERVE_TOOLS,
    PROPOSE_REMEDIATION,
    QUERY_HISTORY,
    QUERY_METRICS,
    RECALL_MEMORY,
    SEARCH_KNOWN_ISSUES,
)
from aegis.agents.horizon.tools_self_edit import UPDATE_HYPOTHESES
from aegis.domain.enums import ActionType
from aegis.domain.horizon import (
    EvidenceCard,
    HorizonHypothesis,
    HorizonPhase,
    HorizonState,
    Source,
)

H_POOL: Final = "h_pool"
H_DEPLOY: Final = "h_deploy"
POOL_STATEMENT: Final = (
    "{svc} connection pool is exhausted; restarting the instance releases the held connections"
)
DEPLOY_STATEMENT: Final = (
    "the {ver} deploy of {svc} introduced a connection leak; only rolling back removes it"
)
DEFAULT_ROLLBACK_TARGET: Final = "1.4.1"
_FIXTURE: Final = (
    Path(__file__).resolve().parents[4] / "tests" / "fixtures" / "nimble_known_issue.json"
)
_VERSION: Final = re.compile(r"\b\d+\.\d+\.\d+\b")
_DEP: Final = re.compile(r"dep=([A-Za-z0-9_.\-]+)@([A-Za-z0-9_.\-]+)")


def _call(state: HorizonState, i: int, tool: str, **arguments: Any) -> ToolCall:
    return ToolCall(id=f"s{state.step}-{i}", name=tool, arguments=arguments)


def _latest(state: HorizonState, tool: str) -> EvidenceCard | None:
    cards = [c for c in state.evidence if c.tool == tool and not is_gap(c)]
    return max(cards, key=lambda c: c.step, default=None)


def _ids(state: HorizonState, *tools: str) -> list[str]:
    out: list[str] = []
    for tool in tools:
        card = _latest(state, tool)
        if card is not None:
            out.append(card.id)
    return out


def _versions(card: EvidenceCard | None) -> tuple[str | None, str | None]:
    """(current, previous) parsed from a deployment card's claim."""
    if card is None:
        return None, None
    current_m = re.search(r"running (\S+)", card.claim)
    current = current_m.group(1) if current_m else None
    history = _VERSION.findall(card.claim.split("history", 1)[-1])
    previous = next((v for v in history if v != current), None)
    return current, previous


class ScriptedBrain:
    """Implements ``ports.Brain`` deterministically."""

    def __init__(self, *, component: str | None = None, version: str | None = None) -> None:
        self._component = component
        self._version = version

    def status(self) -> dict[str, Any]:
        return {"scripted": {"ready": True, "network": False}}

    async def step(self, request: BrainRequest, state: HorizonState) -> BrainDecision:
        allowed = {t.name for t in request.tools}
        calls = [c for c in self._plan(state) if c.name in allowed][: request.max_tool_calls]
        return BrainDecision(
            tool_calls=tuple(calls),
            source=Source.SCRIPTED,
            model="scripted",
            text=f"scripted policy at {state.phase.value}",
            stop_reason="tool_use" if calls else "end_turn",
        )

    # ---- policy -------------------------------------------------------------- #

    def _plan(self, state: HorizonState) -> list[ToolCall]:
        svc = state.service.rsplit(":", 1)[-1]
        phase = state.phase
        if phase in (HorizonPhase.INVESTIGATING, HorizonPhase.DETECTING):
            return self._investigate(state, svc)
        if phase is HorizonPhase.DIAGNOSING:
            return self._diagnose(state, svc)
        if phase is HorizonPhase.PLANNING:
            return self._planning(state, svc)
        if phase is HorizonPhase.REASSESSING:
            return self._reassess(state, svc)
        return []

    def _investigate(self, state: HorizonState, svc: str) -> list[ToolCall]:
        wanted = [
            (QUERY_METRICS, {"service": svc, "metric": "pool_saturation"}),
            (INSTANCE_STATUS, {"service": svc}),
            (DEPLOYMENT_HISTORY, {"service": svc}),
            (CONTAINER_LOGS, {"service": svc}),
        ]
        todo = [(n, a) for n, a in wanted if n not in state.observe_tools_run]
        if not todo:
            # Everything ran and the guard still holds us: widen the net.
            todo = [(QUERY_METRICS, {"service": svc, "metric": "latency_p99"})]
        return [_call(state, i, n, **a) for i, (n, a) in enumerate(todo)]

    def _hypotheses(self, state: HorizonState, svc: str) -> list[dict[str, Any]]:
        """Both hypotheses, citing whatever the state currently holds."""
        verify = _latest(state, VERIFY_TOOL)
        failed = verify if verify is not None and verify.claim.startswith("FAILED") else None
        history = _latest(state, QUERY_HISTORY)
        known = _latest(state, SEARCH_KNOWN_ISSUES)
        current, _ = _versions(_latest(state, DEPLOYMENT_HISTORY))

        pool_support = _ids(state, QUERY_METRICS, INSTANCE_STATUS, CONTAINER_LOGS)
        pool_refute = [c.id for c in (failed, history) if c is not None]
        deploy_support = _ids(state, DEPLOYMENT_HISTORY)
        deploy_support += [c.id for c in (failed, known, history) if c is not None]
        return [
            {
                "id": H_POOL,
                "statement": POOL_STATEMENT.format(svc=svc),
                "supporting": pool_support,
                "refuting": pool_refute,
                "suggested_action": ActionType.RESTART_INSTANCE.value,
            },
            {
                "id": H_DEPLOY,
                "statement": DEPLOY_STATEMENT.format(svc=svc, ver=current or "latest"),
                "supporting": deploy_support,
                "refuting": [],
                "suggested_action": ActionType.ROLLBACK_DEPLOYMENT.value,
            },
        ]

    def _diagnose(self, state: HorizonState, svc: str) -> list[ToolCall]:
        calls: list[ToolCall] = []
        if not state.memory and not _ids(state, RECALL_MEMORY):
            calls.append(_call(state, 0, RECALL_MEMORY, symptom=state.symptom or f"{svc} degraded"))
        hyps = self._hypotheses(state, svc)
        if all(h["supporting"] for h in hyps):
            calls.append(_call(state, 1, UPDATE_HYPOTHESES, hypotheses=hyps))
        else:
            calls.append(_call(state, 1, QUERY_METRICS, service=svc, metric="error_rate"))
        return calls

    def _reassess(self, state: HorizonState, svc: str) -> list[ToolCall]:
        calls = [_call(state, 0, UPDATE_HYPOTHESES, hypotheses=self._hypotheses(state, svc))]
        if SEARCH_KNOWN_ISSUES not in state.observe_tools_run:
            component, version = self._component_version(state)
            calls.append(_call(state, 1, SEARCH_KNOWN_ISSUES, component=component, version=version))
        if QUERY_HISTORY not in state.observe_tools_run:
            calls.append(_call(state, 2, QUERY_HISTORY, name="action_success_rate"))
        return calls

    def _planning(self, state: HorizonState, svc: str) -> list[ToolCall]:
        hyp: HorizonHypothesis | None = state.top_hypothesis()
        if hyp is None or hyp.suggested_action is None:
            return []
        action = ActionType(hyp.suggested_action)
        to_version: str | None = None
        if action is ActionType.ROLLBACK_DEPLOYMENT:
            _, previous = _versions(_latest(state, DEPLOYMENT_HISTORY))
            to_version = previous or DEFAULT_ROLLBACK_TARGET
        evidence = [
            i for i in hyp.supporting if (card := state.card(i)) is not None and not is_gap(card)
        ] or list(hyp.supporting)
        return [
            _call(
                state,
                0,
                PROPOSE_REMEDIATION,
                action_type=action.value,
                target=svc,
                arguments={"to_version": to_version, "replica_delta": None, "cache_key": None},
                hypothesis_id=hyp.id,
                evidence_ids=evidence,
            )
        ]

    # ---- helpers ------------------------------------------------------------- #

    def _component_version(self, state: HorizonState) -> tuple[str, str]:
        if self._component and self._version:
            return self._component, self._version
        for card in reversed(state.evidence):
            m = _DEP.search(card.claim)
            if m:
                return m.group(1), m.group(2)
        fixture = _read_fixture()
        if fixture is not None:
            return fixture
        current, _ = _versions(_latest(state, DEPLOYMENT_HISTORY))
        return "aegis-2.0-workload", current or "unknown"


def _read_fixture() -> tuple[str, str] | None:
    """The dependency named by the recorded known-issue fixture, if present."""
    try:
        data = json.loads(_FIXTURE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    component, version = data.get("component"), data.get("version")
    if isinstance(component, str) and isinstance(version, str) and component and version:
        return component, version
    return None


__all__ = ["OBSERVE_TOOLS", "ScriptedBrain"]
