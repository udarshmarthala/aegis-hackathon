"""The Aegis system under test.

This is the adapter that turns "run an investigation" into a ``RunOutcome``. It
receives a ``ScenarioInput`` - an alert - and nothing else. It has no reference
to the scenario, its fault, or its ground truth, and could not read them if it
wanted to: the harness never passes them here.

What it does:

1. creates an incident from the alert, exactly as ingestion would;
2. builds ``WorkflowDeps`` and lets the ablation remove components from it;
3. runs the investigation to completion under the normal budget guard;
4. reads back what happened - evidence, tool calls, agent runs, the proposed
   action and its policy decision - and returns it as an observation.

Step 4 reads from Postgres rather than trusting the agent's own summary, which
is the same reason the platform stores evidence rather than model prose: the
benchmark should measure what happened, not what the system says happened.
"""

from __future__ import annotations

import time
from typing import Any

from aegis.agents.llm import ModelRouter
from aegis.agents.state import BudgetGuard
from aegis.agents.workflow import WorkflowDeps, run_investigation
from aegis.core.clock import SYSTEM_CLOCK
from aegis.core.config import Settings
from aegis.core.errors import AegisError
from aegis.core.ids import correlation_id
from aegis.core.logging import get_logger
from aegis.domain.enums import (
    ActionState,
    IncidentState,
    PolicyEffect,
    RiskTier,
    Severity,
    VerificationVerdict,
)
from aegis.evaluation.ablations import AblationConfig
from aegis.evaluation.outcome import (
    CostObservation,
    ObservedAction,
    ObservedEvidence,
    ObservedRemediation,
    ObservedTool,
    ObservedVerification,
    RunOutcome,
)
from aegis.evaluation.schema import ScenarioInput
from aegis.evidence.store import EvidenceStore
from aegis.graph.client import Neo4jClient
from aegis.persistence.db import Database
from aegis.persistence.incidents import IncidentRepository
from aegis.telemetry.prometheus import PrometheusClient

log = get_logger(__name__)

MAX_ROWS = 500


class AegisSystemUnderTest:
    """Runs the real investigation workflow against the local stack."""

    __slots__ = (
        "_settings", "_db", "_evidence", "_prometheus", "_neo4j", "_router",
        "_incidents", "_owned_db",
    )

    def __init__(self, settings: Settings, db: Database, *, owns_db: bool = False) -> None:
        self._settings = settings
        self._db = db
        self._owned_db = owns_db
        self._evidence = EvidenceStore(db)
        self._prometheus = PrometheusClient(settings)
        self._neo4j = Neo4jClient(settings)
        self._router = ModelRouter(settings)
        self._incidents = IncidentRepository(db)

    def describe(self) -> dict[str, str]:
        return {
            "agent_version": self._settings.aegis_version,
            "model": self._settings.llm_model_reasoning,
            "environment": self._settings.aegis_environment_name,
        }

    async def close(self) -> None:
        await self._prometheus.close()
        await self._neo4j.close()
        if self._owned_db:
            await self._db.close()

    # ---- the contract -------------------------------------------------------

    async def run(self, alert: ScenarioInput, ablation: AblationConfig) -> RunOutcome:
        started = time.perf_counter()
        incident = await self._incidents.create(
            title=alert.alert_title,
            severity=Severity(alert.severity),
            environment=alert.environment,
            workload=alert.workload,
            correlation_id=correlation_id(),
        )

        deps = self._build_deps()
        deps = ablation.apply(deps)

        state = await run_investigation(
            deps,
            incident_id=incident.id,
            title=alert.alert_title,
            severity=alert.severity.value,
            environment=alert.environment,
            workload=alert.workload,
            correlation_id=incident.correlation_id,
        )
        wall_ms = int((time.perf_counter() - started) * 1000)
        return await self._observe(alert, incident.id, state, wall_ms)

    def _build_deps(self) -> WorkflowDeps:
        """One budget guard per run: budgets are per investigation, not per process."""
        s = self._settings
        return WorkflowDeps(
            settings=s,
            db=self._db,
            evidence=self._evidence,
            prometheus=self._prometheus,
            neo4j=self._neo4j,
            router=self._router,
            budget=BudgetGuard(
                max_wall_seconds=s.agent_max_wall_seconds,
                max_llm_calls=s.agent_max_llm_calls,
                max_tool_calls=s.agent_max_tool_calls,
                max_tokens=s.agent_max_tokens,
                clock=SYSTEM_CLOCK,
            ),
        )

    # ---- observation --------------------------------------------------------

    async def _observe(
        self, alert: ScenarioInput, incident_id: str, state: Any, wall_ms: int
    ) -> RunOutcome:
        diagnosis: dict[str, Any] = dict(state.get("diagnosis") or {})
        abstained = bool(state.get("abstained"))
        causal_path = tuple(diagnosis.get("causal_path") or ())
        affected = tuple(
            state.get("affected_services") or diagnosis.get("affected_services") or ()
        )

        return RunOutcome(
            case_ref=alert.case_ref,
            incident_id=incident_id,
            detected=_detection_observed(incident_id, state),
            grounding_verified=_grounding_verified(state),
            abstained=abstained,
            # The origin is the head of the causal chain when there is one; a
            # diagnosis without a chain has only named the affected surface.
            root_cause_service=(causal_path[0] if causal_path else None),
            root_cause_category=diagnosis.get("root_cause_category"),
            root_cause_statement=str(diagnosis.get("statement") or ""),
            affected_services=affected,
            causal_path=causal_path,
            confidence=float(state.get("confidence") or 0.0),
            cited_evidence_ids=tuple(diagnosis.get("supporting_evidence") or ()),
            evidence=await self._evidence_observed(incident_id),
            tools=await self._tools_observed(incident_id),
            actions=self._actions_observed(state),
            remediation=self._remediation_observed(state),
            verification=self._verification_observed(state),
            cost=await self._cost_observed(incident_id, state, wall_ms),
            evidence_gaps=tuple(
                str(gap.get("source", "")) for gap in (state.get("evidence_gaps") or [])
            ),
            errors=tuple(
                str(err.get("message", "")) for err in (state.get("errors") or [])
            ),
        )

    async def _evidence_observed(self, incident_id: str) -> tuple[ObservedEvidence, ...]:
        try:
            items = await self._evidence.list_for_incident(incident_id, limit=MAX_ROWS)
        except (AegisError, OSError) as exc:
            log.warning("evidence not readable for scoring", incident_id=incident_id,
                        error=str(exc))
            return ()
        return tuple(
            ObservedEvidence(
                evidence_id=item.id,
                source_type=item.source_type.value,
                evidence_type=item.evidence_type.value,
                resource_id=item.resource_id,
                trust_class=item.trust_class.value,
                status=item.status.value,
            )
            for item in items
        )

    async def _tools_observed(self, incident_id: str) -> tuple[ObservedTool, ...]:
        try:
            rows = await self._db.fetch(
                """
                SELECT tool, server, access, ok, result_summary, duration_ms
                  FROM tool_calls
                 WHERE incident_id = $1
                 ORDER BY created_at
                 LIMIT $2
                """,
                incident_id,
                MAX_ROWS,
            )
        except (AegisError, OSError) as exc:
            log.warning("tool calls not readable for scoring", incident_id=incident_id,
                        error=str(exc))
            return ()
        return tuple(
            ObservedTool(
                name=str(row["tool"]),
                source_type=_source_type_of(str(row["server"]), str(row["tool"])),
                succeeded=bool(row["ok"]),
                produced_evidence=bool(row["result_summary"]),
                write_access=str(row["access"]) == "write",
                duration_ms=int(row["duration_ms"] or 0),
            )
            for row in rows
        )

    def _actions_observed(self, state: Any) -> tuple[ObservedAction, ...]:
        proposal: dict[str, Any] = dict(state.get("proposed_action") or {})
        decision: dict[str, Any] = dict(state.get("policy_decision") or {})
        if not proposal:
            return ()
        action_state = ActionState(str(proposal.get("state", "PROPOSED")))
        effect = PolicyEffect(str(decision.get("effect", PolicyEffect.BLOCK.value)))
        executed = action_state in (
            ActionState.EXECUTING, ActionState.VERIFYING, ActionState.SUCCESS,
            ActionState.FAILED, ActionState.ROLLED_BACK,
        )
        # "autonomous" is recorded by the gate chain as "not human approved";
        # inferring it here from the effect would let a bug in the gate chain
        # hide itself from the metric that exists to catch it.
        autonomous = bool(decision.get("autonomous", False)) and executed
        return (
            ObservedAction(
                action_type=str(proposal.get("action_type", "")),
                target_resource_id=str(proposal.get("resource_id", "")),
                risk_tier=RiskTier(int(decision.get("risk_tier", 0))),
                policy_effect=effect,
                state=action_state,
                executed=executed,
                executed_autonomously=autonomous,
                approval_obtained=executed and not autonomous,
                rolled_back=action_state is ActionState.ROLLED_BACK,
                rollback_succeeded=(
                    True if action_state is ActionState.ROLLED_BACK else None
                ),
                verification_verdict=_verdict_of(state),
            ),
        )

    def _remediation_observed(self, state: Any) -> ObservedRemediation:
        proposal: dict[str, Any] = dict(state.get("proposed_action") or {})
        if not proposal:
            return ObservedRemediation()
        return ObservedRemediation(
            category=_remediation_category(str(proposal.get("action_type", ""))),
            patch_proposed=False,
        )

    def _verification_observed(self, state: Any) -> ObservedVerification:
        verdict = _verdict_of(state)
        proposal: dict[str, Any] = dict(state.get("proposed_action") or {})
        verification: dict[str, Any] = dict(proposal.get("verification") or {})
        criteria = tuple(
            c for c in [verification.get("target_metric")] if isinstance(c, str) and c
        )
        protected = tuple(
            c for c in (verification.get("protected_metrics") or []) if isinstance(c, str)
        )
        return ObservedVerification(
            verdict=verdict,
            criteria_tested=criteria,
            protected_metrics_tested=protected,
            regression_detected=verdict is VerificationVerdict.REGRESSION_DETECTED,
        )

    async def _cost_observed(
        self, incident_id: str, state: Any, wall_ms: int
    ) -> CostObservation:
        budget: dict[str, Any] = dict(state.get("budget") or {})
        input_tokens = output_tokens = 0
        cost_usd = 0.0
        ttfh_ms: int | None = None
        ttd_ms: int | None = None
        try:
            rows = await self._db.fetch(
                """
                SELECT agent_role, input_tokens, output_tokens, cost_usd, duration_ms,
                       started_at, finished_at
                  FROM agent_runs
                 WHERE incident_id = $1
                 ORDER BY started_at
                 LIMIT $2
                """,
                incident_id,
                MAX_ROWS,
            )
        except (AegisError, OSError) as exc:
            log.warning("agent runs not readable for scoring", incident_id=incident_id,
                        error=str(exc))
            rows = []

        first_started = rows[0]["started_at"] if rows else None
        for row in rows:
            input_tokens += int(row["input_tokens"] or 0)
            output_tokens += int(row["output_tokens"] or 0)
            cost_usd += float(row["cost_usd"] or 0.0)
            role = str(row["agent_role"])
            finished = row["finished_at"]
            if finished is None or first_started is None:
                continue
            elapsed = int((finished - first_started).total_seconds() * 1000)
            # The first hypothesis an operator could act on is the first
            # diagnosis-role run to complete; the final one gives time-to-diagnosis.
            if role == "diagnosis":
                ttfh_ms = elapsed if ttfh_ms is None else ttfh_ms
                ttd_ms = elapsed

        return CostObservation(
            llm_cost_usd=round(cost_usd, 6),
            input_tokens=input_tokens or int(budget.get("tokens", 0)),
            output_tokens=output_tokens,
            llm_calls=int(budget.get("llm_calls", 0)),
            tool_calls=int(budget.get("tool_calls", 0)),
            wall_ms=wall_ms,
            time_to_first_hypothesis_ms=ttfh_ms,
            time_to_diagnosis_ms=ttd_ms,
        )


# --------------------------------------------------------------------------- #
# small mappings                                                               #
# --------------------------------------------------------------------------- #

_SERVER_SOURCE = {
    "prometheus": "metrics",
    "tempo": "traces",
    "loki": "logs",
    "neo4j": "graph",
    "graph": "graph",
    "runtime": "runtime",
    "github": "vcs",
    "deployment": "deployment",
    "memory": "memory",
    "retrieval": "runbook",
    "sandbox": "sandbox",
}

_ACTION_REMEDIATION = {
    "restart_instance": "replace_instance",
    "rerun_health_check": "diagnostic_probe",
    "scale_up_bounded": "horizontal_scale",
    "scale_service": "horizontal_scale",
    "clear_cache_key": "cache_warm",
    "rollback_deployment": "rollback",
    "update_config": "config_change",
    "promote_patch": "code_fix",
    "drain_instance": "replace_instance",
}


def _source_type_of(server: str, tool: str) -> str:
    """Map an MCP server to the evidence source it speaks for.

    Falls back to the tool name so a server that is renamed degrades to an
    unmatched source rather than to a wrong one.
    """
    key = server.lower()
    if key in _SERVER_SOURCE:
        return _SERVER_SOURCE[key]
    for name, source in _SERVER_SOURCE.items():
        if name in tool.lower():
            return source
    return "runtime"


def _remediation_category(action_type: str) -> str | None:
    return _ACTION_REMEDIATION.get(action_type)


# Phases that exist only once the workflow has begun investigating. RECEIVED and
# TRIAGING mean it never got that far, so nothing was detected in any sense an
# operator would recognise.
_PRE_INVESTIGATION: frozenset[IncidentState] = frozenset(
    {IncidentState.RECEIVED, IncidentState.TRIAGING}
)


def _detection_observed(incident_id: str, state: Any) -> bool | None:
    """Did Aegis actually treat this alert as an incident worth investigating?

    Derived from the run, never assumed. The incident row alone proves nothing -
    this adapter creates one before the workflow starts, so it exists even for a
    run that collapsed immediately. What proves detection is that *and* the
    workflow reaching an investigating phase.

    A phase that cannot be read is reported as unknown rather than as a success.
    Returning ``True`` unconditionally, as this used to, made
    ``DETECTION_FAILURE`` unreachable and every detection number a tautology.
    """
    if not incident_id:
        return False
    raw = state.get("phase")
    if not isinstance(raw, str) or not raw:
        return None
    try:
        phase = IncidentState(raw)
    except ValueError:
        log.warning("unknown workflow phase recorded", phase=raw,
                    incident_id=incident_id)
        return None
    return phase not in _PRE_INVESTIGATION


def _grounding_verified(state: Any) -> bool | None:
    """Whether the investigation-side grounding gate ran, or was ablated away.

    ``None`` when the diagnosis never got far enough for the question to arise;
    an unverified arm must not be indistinguishable from a verified one.
    """
    raw = state.get("evidence_verification")
    if raw == "validated":
        return True
    if raw == "skipped":
        return False
    return None


def _verdict_of(state: Any) -> VerificationVerdict | None:
    """The deterministic verification engine's verdict, or ``None`` if it has none.

    Set by the execute_remediation node from the ``VerificationRun`` the
    execution service produced. ``None`` here means "not measured" - no action
    executed, or no verdict reached - and every metric derived from it stays
    ``None`` rather than collapsing to a zero that reads like a measurement.
    """
    raw = state.get("verification_verdict")
    if not raw:
        return None
    try:
        return VerificationVerdict(str(raw))
    except ValueError:
        log.warning("unknown verification verdict recorded", verdict=str(raw))
        return None


__all__ = ["AegisSystemUnderTest"]
