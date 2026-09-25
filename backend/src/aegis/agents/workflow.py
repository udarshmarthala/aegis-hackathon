"""The incident investigation workflow.

A LangGraph state machine, not a ReAct loop. Control flow is explicit so that
every branch is inspectable, every loop is bounded, and a human interrupt can
suspend and resume without re-running the investigation (ESD 9).

    triage -> investigate -> hypothesize -> diagnose
                   ^                           |
                   +------ (bounded loop) -----+
                                               |
                              plan_remediation -> finalize

The loop back to investigate fires only when the diagnosis abstains AND budget
remains AND the loop counter is under its limit. All three must hold, so the
workflow cannot spin.
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from aegis.agents.llm import (
    LLMUnavailable,
    ModelRouter,
    TaskClass,
    unavailable_reason,
)
from aegis.agents.prompts import (
    DIAGNOSIS_SYSTEM,
    HYPOTHESIS_SYSTEM,
    PROMPT_VERSION,
    REMEDIATION_SYSTEM,
    TRIAGE_SYSTEM,
)
from aegis.agents.schemas import DiagnosisOut, HypothesisSetOut, RemediationOut, TriageOut
from aegis.agents.state import BudgetGuard, IncidentState
from aegis.core.clock import SYSTEM_CLOCK
from aegis.core.config import Settings
from aegis.core.errors import AegisError, BudgetExhausted, SourceUnavailable
from aegis.core.ids import new_id
from aegis.core.logging import get_logger
from aegis.domain.enums import AgentRole, EvidenceType, SourceType
from aegis.domain.models import UntrustedText
from aegis.evidence.store import EvidenceStore
from aegis.graph.client import Neo4jClient
from aegis.mcp import INVESTIGATION_SCOPES, CallerIdentity, ToolContext, ToolResult
from aegis.persistence.db import Database
from aegis.telemetry.prometheus import PrometheusClient

log = get_logger(__name__)

# The window every investigation metric is read over. Fixed rather than
# model-chosen: a widening window is never a better investigation, and the
# number has to be stable for two runs of the same scenario to be comparable.
METRIC_WINDOW_S = 900

# Metrics collected for every suspected service, in this order. Names come from
# the closed catalogue in ``mcp.tools.telemetry``; there is no raw PromQL here
# and no way for an agent to add one.
INVESTIGATION_METRICS: tuple[str, ...] = ("error_rate", "latency_p99", "request_rate")

# The parallel enrichment branches between investigate and hypothesize. Named
# once because the graph builder and the single_agent ablation must agree on
# exactly which nodes the specialised-agent architecture adds.
ENRICHMENT_NODES: tuple[str, ...] = ("analyze_topology", "recall_memory", "analyze_changes")


@dataclass
class WorkflowDeps:
    """Everything the workflow needs, injected rather than imported.

    Every field below ``budget`` is optional on purpose. Aegis degrades rather
    than fails: an investigation with no graph, no retrieval corpus and no
    runtime adapter still collects telemetry, still reasons, and still records
    the missing capabilities as evidence gaps. Making these required would turn
    a missing integration into an outage of the control plane itself.
    """

    settings: Settings
    db: Database
    evidence: EvidenceStore
    prometheus: PrometheusClient
    neo4j: Neo4jClient
    router: ModelRouter
    budget: BudgetGuard
    redis: Any = None

    # ---- benchmark ablations ------------------------------------------------
    # Both default to the production shape. They exist so the evaluation suite
    # can remove a capability and measure what it was worth; nothing in the API,
    # the worker or the MCP layer sets them. They can only remove capability:
    # neither touches the policy engine, the gate chain or the execution-side
    # verification that decides whether a write is allowed to stand.
    #
    # single_agent collapses the parallel enrichment fan-out (topology, memory,
    # change analysis) so only investigate -> hypothesize -> diagnose runs.
    single_agent: bool = False
    # skip_evidence_verification removes the investigation-side grounding gate
    # in `diagnose`: the model's citations are taken at face value instead of
    # being validated before they can become a diagnosis. The harness still
    # validates citations independently when it scores, which is what makes the
    # ablation measurable rather than merely permissive.
    skip_evidence_verification: bool = False

    # knowledge
    graphrag: Any = None          # graph.GraphRAG
    topology: Any = None          # graph.GraphTraversal
    retriever: Any = None         # retrieval.HybridRetriever
    code: Any = None              # retrieval.CodeRetriever
    memory_recall: Any = None     # memory.IncidentMemoryRecall
    memory_store: Any = None      # memory.IncidentMemoryStore

    # change analysis
    github: Any = None            # integrations.GitHubClient

    # safety and action
    gate: Any = None              # execution.ActionGate
    executor: Any = None          # execution.ExecutionService
    ports: Any = None             # execution.ExecutionPorts
    sandbox: Any = None           # execution.SandboxRunner
    audit: Any = None             # persistence.AuditLog
    patches: Any = None           # persistence.PatchRepository

    # communications
    slack: Any = None             # integrations.SlackClient

    # The tool boundary. Optional like everything else, but its absence is not
    # a licence to call a client directly: a node with no invoker records an
    # evidence gap, because an unauthorised read is not a read.
    tools: Any = None             # mcp.ToolInvoker

    # Run-scoped handoff between plan_remediation and execute_remediation.
    # Deliberately NOT part of IncidentState: a ValidatedAction carries a live
    # lease and a live approval, neither of which survives a checkpoint. A
    # resumed run must re-gate rather than replay a permission granted before
    # the crash.
    pending_action: Any = None

    # Run-scoped handoff between localize_code and debug_remediation. A
    # CodeLocalization carries file candidates, symbol snippets and the whole
    # narrowing trail; putting it on IncidentState would write all of that into
    # every checkpoint, which is what "state holds references, not payloads"
    # exists to prevent. A resumed run localises again rather than replaying it.
    localization: Any = None      # retrieval.CodeLocalization
    # Whether localize_code ran in THIS process. Without it, a debugger that
    # finds no localisation cannot tell "we looked and the code is not in the
    # index" from "the worker was killed and the run resumed past the node that
    # looks" - and those point at opposite conclusions. A checkpointed resume
    # rebuilds WorkflowDeps from scratch, so the flag is False exactly when the
    # handoff was lost.
    localization_attempted: bool = False


async def _record_agent_run(
    deps: WorkflowDeps,
    incident_id: str,
    role: AgentRole,
    *,
    task: str,
    summary: str,
    evidence_ids: list[str],
    duration_ms: int,
    status: str = "done",
    meta: dict[str, Any] | None = None,
    error: str | None = None,
) -> None:
    """Persist agent activity for the timeline and the AI-run viewer.

    Never raises: losing an observability row must not fail an investigation.
    """
    try:
        await deps.db.execute(
            """
            INSERT INTO agent_runs
                (id, incident_id, agent_role, status, model, provider, prompt_version,
                 task, result_summary, evidence_ids, duration_ms, error, finished_at)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12, now())
            """,
            new_id("run"), incident_id, role.value, status,
            (meta or {}).get("model"), (meta or {}).get("provider"), PROMPT_VERSION,
            task, summary[:2000], evidence_ids, duration_ms, error,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("agent run not recorded", error=str(exc))


async def _publish(deps: WorkflowDeps, incident_id: str, event: dict[str, Any]) -> None:
    from aegis.api.routers.stream import publish

    await publish(deps.redis, incident_id, event)


def _evidence_digest(state: IncidentState, limit: int = 60) -> str:
    """Render evidence for a prompt: ids, summaries, trust and status.

    Bounded by count and by line length so a noisy incident cannot blow the
    context window (AIArchitecture 37).
    """
    lines: list[str] = []
    for item in (state.get("evidence_summaries") or [])[:limit]:
        status = item.get("status", "")
        marker = " [SOURCE UNAVAILABLE]" if status == "SOURCE_UNAVAILABLE" else ""
        lines.append(
            f"- {item['id']} ({item.get('trust_class','?')}, {item.get('source','?')})"
            f"{marker}: {str(item.get('summary',''))[:300]}"
        )
    if not lines:
        return "(no evidence collected)"
    return "\n".join(lines)


def _tool_context(
    deps: WorkflowDeps,
    state: IncidentState,
    *,
    node: str,
    scopes: frozenset[str] = INVESTIGATION_SCOPES,
) -> ToolContext:
    """Bound one node's tool calls to this run's incident, budget and deadline.

    The scope set defaults to ``INVESTIGATION_SCOPES`` - reads only. An
    investigating agent that could also execute is not an investigator, and
    there is no mutator on ``CallerIdentity`` for it to widen the set with
    mid-run (AIArchitecture 38).

    ``scopes`` is a parameter rather than a constant only so one node -
    ``debug_remediation`` - can be granted ``sandbox:run`` in addition. That
    scope executes code in a disposable, networkless container and nowhere else;
    no remediation scope is reachable from here by any node.

    The deadline is the budget's remaining wall clock, so a tool call can never
    outlive the run that authorised it even if the tool's own timeout is longer.
    """
    remaining = deps.budget.view().seconds_remaining
    return ToolContext(
        environment=state.get("environment", "local"),
        caller=CallerIdentity(
            subject=f"agent:{node}",
            actor_type="agent",
            scopes=scopes,
        ),
        budget=deps.budget,
        deadline=SYSTEM_CLOCK.now() + timedelta(seconds=remaining),
        correlation_id=state.get("correlation_id", ""),
        incident_id=state["incident_id"],
    )


def _unavailable_reason(result: ToolResult) -> str:
    """Why a tool could not answer, in one sentence an operator can act on."""
    if result.degraded_reason:
        return result.degraded_reason
    if result.error is not None:
        return f"{result.error.code}: {result.error.message}"
    return "the tool returned no answer and no reason"


def _summarise_evidence(item: Any, fallback_source: str) -> dict[str, Any]:
    """The compact evidence view the prompt digest and the UI both read."""
    return {
        "id": item.id,
        "summary": item.summary,
        "trust_class": item.trust_class.value,
        "source": item.source or fallback_source,
        "status": item.status.value,
    }


def _gaps_digest(state: IncidentState) -> str:
    gaps = state.get("evidence_gaps") or []
    if not gaps:
        return "(all evidence sources responded)"
    return "\n".join(f"- {g['source']}: {g['reason']}" for g in gaps)


# --------------------------------------------------------------------------- #
# nodes                                                                        #
# --------------------------------------------------------------------------- #


async def _resolve_services(deps: WorkflowDeps, candidates: list[str]) -> list[str]:
    """Map model-suggested service names onto services that actually exist.

    Exact match first, then a conservative substring match so "payment-service"
    still resolves to "payment". Anything that resolves to nothing is dropped:
    querying telemetry for a service that does not exist yields noise, not
    evidence, and would pollute the incident with meaningless citations.

    If telemetry cannot be reached, the hints are returned unchanged and the
    caller records the unavailability as an evidence gap.
    """
    if not candidates:
        return []
    try:
        known = await deps.prometheus.known_services()
    except SourceUnavailable:
        return candidates

    if not known:
        return []

    known_lower = {k.lower(): k for k in known}
    resolved: list[str] = []
    for raw in candidates:
        name = raw.strip().lower()
        if not name or len(name) > 120:
            continue
        match: str | None
        if name in known_lower:
            match = known_lower[name]
        else:
            match = next(
                (real for low, real in known_lower.items()
                 if low in name or name in low),
                None,
            )
        if match and match not in resolved:
            resolved.append(match)

    if not resolved:
        log.info("no model-suggested service resolved; falling back to discovery",
                 suggested=candidates[:5], known=known[:10])
        return []
    return resolved


def _first_service(state: IncidentState) -> str | None:
    """The service an action is aimed at, or None when none is known.

    None and "" are not interchangeable here: "" would be accepted as a real
    target resource and defeat the policy gate's scoping, whereas None makes
    the absence explicit and lets the gate fail closed.
    """
    for name in state.get("affected_services") or []:
        if name:
            return name
    return None




async def _resolve_service_ids(
    deps: WorkflowDeps, names: list[str]
) -> list[str]:
    """Map service names onto the identifiers the graph actually uses.

    Constructing ``environment:workload:name`` from configuration looks correct
    and is not. A graph can legitimately hold several nodes for one service
    name: one seeded by a bootstrap script, another discovered from telemetry
    under a different environment label. Only one of them carries the CALLS
    edges, and picking the other returns an empty neighbourhood - which the
    diagnosis then reports as "no downstream services" on a graph that plainly
    contains them.

    So the identifier is resolved by asking the graph, preferring whichever node
    for a given name has the most relationships. A node with edges is the one
    describing the topology; an isolated duplicate is a naming artefact.

    Falls back to the configured form when the graph cannot be reached, because
    a degraded lookup should still let the expansion try rather than abort.
    """
    wanted = [n.split(":")[-1].strip() for n in names if n and n.strip()]
    if not wanted:
        return []

    fallback = [
        f"{deps.settings.aegis_environment_name}:{deps.settings.workload_namespace}:{n}"
        for n in wanted
    ]
    # No None guard on deps.neo4j: WorkflowDeps declares it as a required
    # client, and the client itself converts every driver failure into
    # SourceUnavailable, which is handled below.
    try:
        rows = await deps.neo4j.run(
            """
            MATCH (s:Service) WHERE s.name IN $names
            OPTIONAL MATCH (s)-[r:CALLS]-()
            WITH s.name AS name, s.service_id AS service_id, count(r) AS degree
            RETURN name, service_id, degree ORDER BY degree DESC
            """,
            {"names": wanted},
        )
    except SourceUnavailable as exc:
        log.info(
            "graph unreachable while resolving service ids; using configured form",
            reason=str(exc),
        )
        return fallback

    best: dict[str, str] = {}
    for row in rows:
        name = str(row.get("name") or "")
        if name and name not in best:      # ordered by degree, so first wins
            best[name] = str(row.get("service_id") or "")

    resolved: list[str] = []
    for name, constructed in zip(wanted, fallback, strict=True):
        chosen = best.get(name) or constructed
        if chosen not in resolved:
            resolved.append(chosen)
    return resolved


def _topology_neighbours(state: IncidentState, limit: int = 6) -> list[str]:
    """Services adjacent to the incident in the operational graph.

    Returns an empty list on the first pass, when topology has not been expanded
    yet. That is deliberate: the first investigation samples what the alert
    named, and the bounded re-investigation loop widens to the neighbourhood
    once the graph has actually been consulted.

    Entries arrive as ``ScoredNode`` records, so their ``service_id`` is read as
    an attribute. An earlier version fell back to ``str(raw)`` for anything that
    was not a mapping, which stringified the whole dataclass and fed its repr
    onward as though it were a service name - a silent corruption that produced
    telemetry queries for a service that could not exist.
    """
    topology = state.get("topology") or {}
    out: list[str] = []

    def _add(value: object) -> None:
        if value is None:
            return
        if isinstance(value, Mapping):
            name = str(value.get("service_id") or value.get("name") or "")
        else:
            name = str(getattr(value, "service_id", "") or getattr(value, "name", ""))
        short = name.split(":")[-1].strip()
        if short and short not in out:
            out.append(short)

    for raw in list(topology.get("services") or []):
        _add(raw)

    for edge in list(topology.get("edges") or []):
        if isinstance(edge, Mapping):
            _add(edge.get("source"))
            _add(edge.get("target"))
        else:
            _add(getattr(edge, "source", None))
            _add(getattr(edge, "target", None))

    return out[:limit]


def make_nodes(deps: WorkflowDeps) -> dict[str, Any]:
    """Build node callables closed over the injected dependencies."""

    async def triage(state: IncidentState) -> dict[str, Any]:
        deps.budget.check("triage")
        started = time.perf_counter()
        incident_id = state["incident_id"]
        await _publish(deps, incident_id, {"type": "phase", "phase": "TRIAGING"})

        user = (
            f"Alert title: {state.get('title','')}\n"
            f"Severity: {state.get('severity','P3')}\n"
            f"Environment: {state.get('environment','')}\n"
            f"Workload: {state.get('workload','')}\n"
        )
        try:
            out, meta = await deps.router.structured(
                schema=TriageOut, system=TRIAGE_SYSTEM, user=user, task=TaskClass.FAST
            )
            deps.budget.charge_llm(input_tokens=len(user) // 4, output_tokens=200)
            candidates = out.candidate_services
            summary = out.restated_problem
        except LLMUnavailable as exc:
            # No model available is not a crash. Proceed on telemetry alone and
            # let the diagnosis step abstain if that is not enough.
            log.warning("triage degraded", reason=unavailable_reason(exc))
            candidates, summary, meta = [], state.get("title", ""), {}

        # A model naming a service is a HINT, not an identifier. Resolve every
        # candidate against services telemetry actually knows about; otherwise a
        # descriptive phrase ends up used as a PromQL label and Aegis queries for
        # a service that does not exist.
        candidates = await _resolve_services(deps, candidates)

        await _record_agent_run(
            deps, incident_id, AgentRole.TRIAGE,
            task="initial assessment", summary=summary,
            evidence_ids=[], duration_ms=int((time.perf_counter() - started) * 1000),
            meta=meta,
        )
        return {
            "phase": "TRIAGING",
            "candidate_services": candidates,
            "affected_services": candidates[:1],
        }

    async def _collect_metrics(
        state: IncidentState, incident_id: str, services: list[str]
    ) -> tuple[list[str], list[dict[str, Any]], list[dict[str, str]]]:
        """Metric evidence per service, gathered through the tool boundary.

        Every fetch goes through ``ToolInvoker`` rather than through the
        Prometheus client directly. That is what makes each read
        schema-validated, scope-checked, deadline-bounded, charged against the
        run's budget exactly once, and recorded as a ``tool_calls`` row - the
        rows the tool-use evaluation metrics are computed from. A direct client
        call does the same work and leaves no trace that it happened.

        A source failure records a gap and stops probing that source rather
        than retrying it for every remaining service.
        """
        ids: list[str] = []
        gaps: list[dict[str, str]] = []

        if deps.tools is None:
            # Fail closed. Without the boundary there is no authorised way to
            # read telemetry, and carrying on quietly would turn "we never
            # asked" into "nothing was wrong".
            reason = "the tool boundary is not configured; telemetry was not read"
            item = await deps.evidence.record_unavailable(
                incident_id=incident_id, source="prometheus",
                source_type=SourceType.METRICS, reason=reason,
            )
            return (
                [item.id],
                [_summarise_evidence(item, "prometheus")],
                [{"source": "prometheus", "reason": reason}],
            )

        context = _tool_context(deps, state, node="investigate")
        source_down = False

        for service in services:
            if source_down:
                break
            for metric in INVESTIGATION_METRICS:
                # The invoker charges the budget for this call. Charging here
                # as well would bill one read twice and silently halve the
                # budget an investigation actually gets.
                result = await deps.tools.invoke(
                    "query_metric_range",
                    {"service": service, "metric": metric, "window_s": METRIC_WINDOW_S},
                    context,
                )

                if result.degraded or not result.ok:
                    # "Could not look", never "found nothing". A degraded tool
                    # has already written the gap and cites it back; a refused
                    # call never reached a handler, so the gap is written here.
                    reason = _unavailable_reason(result)
                    if result.evidence_ids:
                        ids.extend(result.evidence_ids)
                    else:
                        item = await deps.evidence.record_unavailable(
                            incident_id=incident_id, source="prometheus",
                            source_type=SourceType.METRICS, reason=reason,
                        )
                        ids.append(item.id)
                    gaps.append({"source": "prometheus", "reason": reason})
                    source_down = True
                    break

                if result.found_nothing:
                    # Prometheus answered and holds no such series. That is a
                    # finding, and the hypothesis step needs it stated rather
                    # than merely absent from the evidence list.
                    item = await deps.evidence.record(
                        incident_id=incident_id,
                        source="prometheus",
                        source_type=SourceType.METRICS,
                        evidence_type=EvidenceType.METRIC_SERIES,
                        summary=f"{service} {metric}: no data in window",
                        structured_value={
                            "service": service, "metric": metric,
                            "latest": None, "peak": None, "points": 0,
                            "window_s": METRIC_WINDOW_S,
                        },
                        provenance_uri=f"promql:{metric}:{service}",
                        resource_id=service,
                    )
                    ids.append(item.id)
                    continue

                # The tool stored the observation and cited it back; re-storing
                # it here would double every metric in the evidence trail.
                ids.extend(result.evidence_ids)

        # One bulk read rather than N: the digest needs each item's trust class
        # and status, and those are decided by the store, not by the caller.
        stored = await deps.evidence.get_many(ids)
        summaries = [
            _summarise_evidence(stored[eid], "prometheus") for eid in ids if eid in stored
        ]
        return ids, summaries, gaps

    async def _collect_topology(
        incident_id: str, environment: str
    ) -> tuple[list[str], list[dict[str, Any]], list[dict[str, str]], dict[str, Any]]:
        """Topology from Neo4j. Soft: a gap here lowers confidence, nothing more.

        Still a direct client read rather than a tool call, because the tool
        catalogue exposes topology per service (blast radius, neighbourhood,
        expansion) and this is an environment-wide edge dump with no equivalent
        in it. Routing it through the nearest tool would silently answer a
        different question, so it charges the budget itself instead.
        """
        deps.budget.charge_tool()
        try:
            rows = await deps.neo4j.run(
                "MATCH (s:Service)-[:CALLS]->(t:Service) "
                "WHERE s.environment = $env "
                "RETURN s.name AS src, t.name AS dst LIMIT 200",
                {"env": environment},
            )
        except SourceUnavailable as exc:
            item = await deps.evidence.record_unavailable(
                incident_id=incident_id, source="neo4j",
                source_type=SourceType.GRAPH, reason=exc.message,
            )
            return (
                [item.id],
                [{"id": item.id, "summary": item.summary,
                  "trust_class": item.trust_class.value,
                  "source": "neo4j", "status": item.status.value}],
                [{"source": "neo4j", "reason": exc.message}],
                {},
            )

        topology = {"edges": rows}
        if not rows:
            return [], [], [], topology

        item = await deps.evidence.record(
            incident_id=incident_id, source="neo4j", source_type=SourceType.GRAPH,
            evidence_type=EvidenceType.TOPOLOGY_PATH,
            summary=f"service topology: {len(rows)} call edges",
            structured_value=topology, provenance_uri="cypher:service_calls",
        )
        return (
            [item.id],
            [{"id": item.id, "summary": item.summary,
              "trust_class": item.trust_class.value,
              "source": "neo4j", "status": item.status.value}],
            [],
            topology,
        )

    async def investigate(state: IncidentState) -> dict[str, Any]:
        """Gather evidence from every source, each isolated from the others."""
        deps.budget.check("investigate")
        started = time.perf_counter()
        incident_id = state["incident_id"]
        environment = state.get("environment", "local")
        await _publish(deps, incident_id, {"type": "phase", "phase": "INVESTIGATING"})

        services = list(state.get("candidate_services") or [])
        discovery_gap: list[str] = []
        discovery_summaries: list[dict[str, Any]] = []
        discovery_gaps: list[dict[str, str]] = []

        if not services:
            # Nothing named in the alert: ask telemetry what exists rather than
            # assuming a topology. If telemetry cannot be asked, that is an
            # evidence gap - not an empty world.
            try:
                services = await deps.prometheus.known_services()
            except SourceUnavailable as exc:
                item = await deps.evidence.record_unavailable(
                    incident_id=incident_id,
                    source="prometheus",
                    source_type=SourceType.METRICS,
                    reason=exc.message,
                )
                discovery_gap = [item.id]
                discovery_summaries = [{
                    "id": item.id, "summary": item.summary,
                    "trust_class": item.trust_class.value,
                    "source": "prometheus", "status": item.status.value,
                }]
                discovery_gaps = [{"source": "prometheus", "reason": exc.message}]
                services = []

        # Expand to the alerted service's graph neighbours before sampling.
        #
        # An alert names where a symptom surfaced, not where the fault is. A
        # checkout service returning 5xx because its payment dependency is
        # failing looks, from checkout's metrics alone, exactly like checkout
        # breaking on its own - which is precisely the ambiguity that forces an
        # honest abstention. Sampling the neighbours is what makes the two
        # distinguishable.
        #
        # Neighbours are appended after the alerted services, and the whole list
        # is still cut to the parallel-investigator budget, so this widens what
        # is examined without widening what it costs.
        neighbours = _topology_neighbours(state)
        if neighbours:
            for candidate in neighbours:
                if candidate not in services:
                    services.append(candidate)
            log.info(
                "expanded investigation to graph neighbours",
                incident_id=incident_id,
                alerted=len(state.get("candidate_services") or []),
                neighbours=len(neighbours),
            )

        services = services[: deps.settings.agent_max_parallel_investigators]

        m_ids, m_sum, m_gaps = await _collect_metrics(state, incident_id, services)
        t_ids, t_sum, t_gaps, topology = await _collect_topology(incident_id, environment)

        ids = discovery_gap + m_ids + t_ids
        summaries = discovery_summaries + m_sum + t_sum
        gaps = discovery_gaps + m_gaps + t_gaps

        await _record_agent_run(
            deps, incident_id, AgentRole.EVIDENCE_INVESTIGATOR,
            task=f"collect evidence for {len(services)} service(s)",
            summary=f"{len(ids)} evidence items, {len(gaps)} unavailable source(s)",
            evidence_ids=ids,
            duration_ms=int((time.perf_counter() - started) * 1000),
        )
        await _publish(deps, incident_id,
                       {"type": "evidence", "count": len(ids), "gaps": len(gaps)})

        return {
            "evidence_ids": ids,
            "evidence_summaries": summaries,
            "evidence_gaps": gaps,
            "topology": topology,
            "affected_services": services,
            "phase": "INVESTIGATING",
            # Incremented here rather than in the router so the bound holds no
            # matter which edge re-enters investigation.
            "loop_count": int(state.get("loop_count") or 0) + 1,
        }

    async def hypothesize(state: IncidentState) -> dict[str, Any]:
        """Produce competing explanations with falsifiable predictions."""
        deps.budget.check("hypothesize")
        started = time.perf_counter()
        incident_id = state["incident_id"]
        await _publish(deps, incident_id, {"type": "phase", "phase": "DIAGNOSING"})

        user = (
            f"Incident: {state.get('title','')}\n"
            f"Environment: {state.get('environment','')}\n"
            f"Services under suspicion: "
            f"{', '.join(state.get('affected_services') or []) or 'unknown'}\n\n"
            f"EVIDENCE:\n{_evidence_digest(state)}\n\n"
            f"UNAVAILABLE SOURCES:\n{_gaps_digest(state)}\n"
        )
        try:
            out, meta = await deps.router.structured(
                schema=HypothesisSetOut, system=HYPOTHESIS_SYSTEM,
                user=user, task=TaskClass.REASONING,
            )
            deps.budget.charge_llm(input_tokens=len(user) // 4, output_tokens=800)
        except LLMUnavailable as exc:
            log.warning("hypothesis generation unavailable", error=str(exc))
            await _record_agent_run(
                deps, incident_id, AgentRole.DIAGNOSIS,
                task="generate hypotheses", summary=unavailable_reason(exc),
                evidence_ids=[], status="failed", error=str(exc),
                duration_ms=int((time.perf_counter() - started) * 1000),
            )
            return {"hypotheses": [], "phase": "DIAGNOSING"}

        # Confidence is derived from the evidence, never taken from the model.
        from aegis.domain.models import Hypothesis, Prediction
        from aegis.evidence import confidence as conf

        stored = await deps.evidence.list_for_incident(incident_id)
        hypotheses: list[dict[str, Any]] = []

        for h in out.hypotheses:
            model = Hypothesis(
                id=h.label,
                statement=h.statement,
                supporting=h.supporting_evidence,
                contradicting=h.contradicting_evidence,
                missing=h.missing_evidence,
                predictions=[
                    Prediction(
                        statement=p.statement, metric=p.metric,
                        resource_id=p.resource_id,
                        direction=p.direction if p.direction in
                        ("increase", "decrease", "stable") else None,
                    )
                    for p in h.predictions
                ],
                affected_services=h.affected_services,
            )
            breakdown = conf.compute(model, stored)
            record = {
                "label": h.label,
                "statement": h.statement,
                "supporting": h.supporting_evidence,
                "contradicting": h.contradicting_evidence,
                "missing": h.missing_evidence,
                "predictions": [p.model_dump() for p in model.predictions],
                "affected_services": h.affected_services,
                "confidence": breakdown.value,
                "confidence_explain": breakdown.explain(),
            }
            hypotheses.append(record)

            try:
                hid = new_id("hyp")
                await deps.db.execute(
                    """
                    INSERT INTO hypotheses
                        (id, incident_id, label, statement, state, confidence,
                         supporting, contradicting, missing, predictions, affected_services)
                    VALUES ($1,$2,$3,$4,'PROPOSED',$5,$6,$7,$8,$9,$10)
                    ON CONFLICT (incident_id, label) DO UPDATE
                        SET statement = EXCLUDED.statement,
                            confidence = EXCLUDED.confidence,
                            supporting = EXCLUDED.supporting,
                            contradicting = EXCLUDED.contradicting,
                            missing = EXCLUDED.missing,
                            predictions = EXCLUDED.predictions,
                            updated_at = now()
                    """,
                    hid, incident_id, h.label, h.statement, breakdown.value,
                    h.supporting_evidence, h.contradicting_evidence, h.missing_evidence,
                    record["predictions"], h.affected_services,
                )
                # Confidence history powers the sparkline in the UI (UX spec 23).
                await deps.db.execute(
                    """
                    INSERT INTO hypothesis_confidence_history (hypothesis_id, confidence, reason)
                    SELECT id, $2, $3 FROM hypotheses
                    WHERE incident_id = $1 AND label = $4
                    """,
                    incident_id, breakdown.value, "derived from evidence", h.label,
                )
            except Exception as exc:  # noqa: BLE001
                log.warning("hypothesis not persisted", label=h.label, error=str(exc))

        hypotheses.sort(key=lambda x: x["confidence"], reverse=True)
        await _record_agent_run(
            deps, incident_id, AgentRole.DIAGNOSIS,
            task="generate competing hypotheses",
            summary=f"{len(hypotheses)} hypotheses; next check: {out.recommended_next_check}",
            evidence_ids=state.get("evidence_ids") or [],
            duration_ms=int((time.perf_counter() - started) * 1000), meta=meta,
        )
        await _publish(deps, incident_id,
                       {"type": "hypotheses", "count": len(hypotheses)})
        return {"hypotheses": hypotheses, "phase": "DIAGNOSING"}

    async def diagnose(state: IncidentState) -> dict[str, Any]:
        """Conclude, or abstain. Both are valid terminal states for this node."""
        deps.budget.check("diagnose")
        started = time.perf_counter()
        incident_id = state["incident_id"]

        hypotheses = state.get("hypotheses") or []
        if not hypotheses:
            return {
                "abstained": True,
                "confidence": 0.0,
                "diagnosis": {
                    "abstained": True,
                    "statement": "No hypotheses could be formed from available evidence.",
                    "missing_evidence": [g["source"] for g in state.get("evidence_gaps") or []],
                },
                "phase": "DIAGNOSING",
            }

        rendered = "\n".join(
            f"- {h['label']} (derived confidence {h['confidence']:.2f}): {h['statement']}\n"
            f"    supporting: {', '.join(h['supporting']) or 'none'}\n"
            f"    contradicting: {', '.join(h['contradicting']) or 'none'}\n"
            f"    missing: {', '.join(h['missing']) or 'none'}"
            for h in hypotheses
        )
        user = (
            f"Incident: {state.get('title','')}\n\n"
            f"HYPOTHESES:\n{rendered}\n\n"
            f"EVIDENCE:\n{_evidence_digest(state)}\n\n"
            f"UNAVAILABLE SOURCES:\n{_gaps_digest(state)}\n"
        )

        try:
            out, meta = await deps.router.structured(
                schema=DiagnosisOut, system=DIAGNOSIS_SYSTEM,
                user=user, task=TaskClass.REASONING,
            )
            deps.budget.charge_llm(input_tokens=len(user) // 4, output_tokens=600)
        except LLMUnavailable as exc:
            reason = unavailable_reason(exc)
            log.warning("diagnosis unavailable", reason=reason, error=str(exc))
            return {
                "abstained": True, "confidence": 0.0,
                "diagnosis": {"abstained": True,
                              "statement": f"Diagnosis unavailable: {reason}"},
                "phase": "DIAGNOSING",
            }

        # Grounding gate. An ungrounded conclusion is downgraded to an
        # abstention rather than shown to an operator (AIArchitecture 14).
        from aegis.evidence.validator import EvidenceValidator

        abstained = out.abstain
        report = None
        verification_state = "validated"
        if deps.skip_evidence_verification:
            # The no_verifier ablation, and only ever that: an operator-facing
            # run cannot reach this branch. Recorded on the state so the result
            # is legible as an unverified arm instead of quietly resembling a
            # verified one.
            verification_state = "skipped"
            log.warning("evidence verification disabled for this run",
                        incident_id=incident_id)
        elif not abstained:
            validator = EvidenceValidator(deps.evidence)
            report = await validator.validate_citations(incident_id, out.supporting_evidence)
            if not report.valid or report.tier_a_count == 0:
                log.warning(
                    "diagnosis rejected as ungrounded",
                    incident_id=incident_id, problems=report.problems,
                    tier_a=report.tier_a_count,
                )
                abstained = True

        selected = out.selected_hypothesis_label
        derived = next(
            (h["confidence"] for h in hypotheses if h["label"] == selected),
            hypotheses[0]["confidence"],
        )
        confidence = 0.0 if abstained else derived

        statement = (
            out.statement if not abstained or out.abstain
            else "Evidence does not sufficiently support a root cause yet."
        )
        # Only citations the validator actually resolved may be attributed to
        # the diagnosis; an abstention carries none. With the verifier ablated
        # there is nothing to resolve them against, so the model's own list is
        # carried through unchecked - which is precisely the behaviour the
        # ablation exists to put a number on.
        supporting: list[str]
        if abstained:
            supporting = []
        elif report is not None:
            supporting = list(report.resolved)
        elif deps.skip_evidence_verification:
            supporting = list(out.supporting_evidence)
        else:
            supporting = []

        diagnosis: dict[str, Any] = {
            "abstained": abstained,
            "statement": statement,
            "root_cause_category": None if abstained else out.root_cause_category,
            "confidence": confidence,
            "selected_hypothesis_label": None if abstained else selected,
            "supporting_evidence": supporting,
            "causal_path": [] if abstained else out.causal_path,
            "affected_services": out.affected_services,
            "contributing_factors": out.contributing_factors,
            "rejected_alternatives": out.rejected_alternatives,
            "missing_evidence": out.missing_evidence,
            "uncertainty": out.uncertainty,
        }

        try:
            await deps.db.execute(
                """
                INSERT INTO diagnoses
                    (id, incident_id, abstained, statement, root_cause_category,
                     confidence, selected_hypothesis_id, supporting_evidence,
                     causal_path, affected_services, contributing_factors,
                     rejected_alternatives, missing_evidence, uncertainty)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14)
                """,
                new_id("hyp"), incident_id, abstained, statement,
                diagnosis["root_cause_category"], confidence, selected,
                diagnosis["supporting_evidence"], diagnosis["causal_path"],
                out.affected_services, out.contributing_factors,
                out.rejected_alternatives, out.missing_evidence, out.uncertainty,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("diagnosis not persisted", error=str(exc))

        await _record_agent_run(
            deps, incident_id, AgentRole.VERIFIER if abstained else AgentRole.DIAGNOSIS,
            task="weigh hypotheses against evidence",
            summary=("abstained: " + statement) if abstained else statement,
            evidence_ids=supporting,
            duration_ms=int((time.perf_counter() - started) * 1000), meta=meta,
        )
        await _publish(deps, incident_id, {
            "type": "diagnosis", "abstained": abstained, "confidence": confidence,
        })
        return {
            "diagnosis": diagnosis,
            "abstained": abstained,
            "confidence": confidence,
            "selected_hypothesis_id": selected,
            "affected_services": out.affected_services or state.get("affected_services") or [],
            "phase": "DIAGNOSING",
            "evidence_verification": verification_state,
        }

    async def plan_remediation(state: IncidentState) -> dict[str, Any]:
        """Propose an action, then submit it to the deterministic policy engine.

        The model's output is only ever a *proposal*. Everything that decides
        whether it may run - risk tier, kill switches, evidence quality, blast
        radius, rollback, rate limit - is computed here from persisted facts,
        outside anything the model can influence.
        """
        deps.budget.check("plan_remediation")
        started = time.perf_counter()
        incident_id = state["incident_id"]

        if state.get("abstained"):
            return {"proposed_action": None, "policy_decision": None}

        from aegis.domain.enums import ActionType
        from aegis.policy.tiers import profile_for

        registry = "\n".join(
            f"- {a.value} (tier {int(profile_for(a).tier)}): {profile_for(a).description}"
            for a in ActionType
            if int(profile_for(a).tier) < 3  # tier 3 is never offered to a model
        )
        diagnosis = state.get("diagnosis") or {}
        user = (
            f"Diagnosis: {diagnosis.get('statement','')}\n"
            f"Root cause category: {diagnosis.get('root_cause_category')}\n"
            f"Affected services: {', '.join(state.get('affected_services') or [])}\n"
            f"Derived confidence: {state.get('confidence', 0.0):.2f}\n\n"
            f"ACTION REGISTRY (choose at most one):\n{registry}\n\n"
            f"EVIDENCE:\n{_evidence_digest(state)}\n"
        )

        try:
            out, meta = await deps.router.structured(
                schema=RemediationOut, system=REMEDIATION_SYSTEM,
                user=user, task=TaskClass.REASONING,
            )
            deps.budget.charge_llm(input_tokens=len(user) // 4, output_tokens=400)
        except LLMUnavailable as exc:
            log.warning("remediation planning unavailable", error=str(exc))
            return {"proposed_action": None, "policy_decision": None}

        if not out.recommend_action or not out.action_type:
            await _record_agent_run(
                deps, incident_id, AgentRole.REMEDIATION_PLANNER,
                task="consider remediation",
                summary=f"no action proposed: {out.reason or 'preconditions not met'}",
                evidence_ids=[], meta=meta,
                duration_ms=int((time.perf_counter() - started) * 1000),
            )
            return {"proposed_action": None, "policy_decision": None}

        # A model naming an unknown action type is a rejected proposal, never a
        # crash and never an improvised action.
        try:
            action_type = ActionType(out.action_type)
        except ValueError:
            log.warning("model proposed unknown action type", proposed=out.action_type)
            return {"proposed_action": None, "policy_decision": None}

        decision, proposal, validated = await evaluate_action_policy(
            deps,
            incident_id=incident_id,
            action_type=action_type,
            target_resource_id=out.target_resource_id or "",
            service_id=_first_service(state),
            environment=state.get("environment", "local"),
            severity=state.get("severity", "P3"),
            reason=out.reason,
            supporting_evidence=out.supporting_evidence,
            blast_radius_services=out.blast_radius_services,
            has_rollback=bool(out.rollback_description),
            rollback_description=out.rollback_description or "",
            expected_metric=out.expected_metric,
            expected_direction=out.expected_direction,
            expected_threshold=out.expected_threshold,
            confidence=float(state.get("confidence") or 0.0),
            abstained=bool(state.get("abstained")),
        )

        await _record_agent_run(
            deps, incident_id, AgentRole.REMEDIATION_PLANNER,
            task=f"propose {action_type.value}",
            summary=(
                f"policy={decision['effect']} tier={decision['risk_tier']} "
                f"rule={decision['matched_rule']}"
            ),
            evidence_ids=out.supporting_evidence, meta=meta,
            duration_ms=int((time.perf_counter() - started) * 1000),
        )
        await _publish(deps, incident_id, {
            "type": "action_proposed",
            "action_type": action_type.value,
            "effect": decision["effect"],
        })
        deps.pending_action = validated
        return {
            "proposed_action": proposal,
            "policy_decision": decision,
            "awaiting_approval": decision["effect"] == "REQUIRE_HUMAN",
        }

    async def finalize(state: IncidentState) -> dict[str, Any]:
        """Persist the final assessment and close out the run."""
        incident_id = state["incident_id"]
        diagnosis = state.get("diagnosis") or {}
        summary = diagnosis.get("statement", "Investigation completed.")

        try:
            await deps.db.execute(
                """
                UPDATE incidents
                   SET summary = $2, confidence = $3, affected_services = $4,
                       suspected_origin = $5, updated_at = now()
                 WHERE id = $1
                """,
                incident_id, summary[:4000], state.get("confidence"),
                state.get("affected_services") or [],
                _first_service(state),
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("final assessment not persisted", error=str(exc))

        await _publish(deps, incident_id, {
            "type": "finished",
            "abstained": bool(state.get("abstained")),
            "confidence": state.get("confidence"),
        })
        return {
            "finished": True,
            "budget": deps.budget.snapshot(),
            "phase": "AWAITING_APPROVAL" if state.get("awaiting_approval") else "MONITORING",
        }


    async def analyze_topology(state: IncidentState) -> dict[str, Any]:
        """Establish blast radius and causal paths from the operational graph.

        Neo4j is a soft dependency. When it cannot be reached the node records
        an evidence gap and returns - an investigation without topology has
        weaker blast-radius reasoning, not no investigation (ESD 34).
        """
        deps.budget.check("analyze_topology")
        started = time.perf_counter()
        incident_id = state["incident_id"]
        services = state.get("affected_services") or state.get("candidate_services") or []

        # Telemetry labels a workload by its bare name; the graph keys nodes by
        # the canonical ``environment:workload:name``. Seeding the expansion
        # with a bare name therefore matched nothing, the traversal returned no
        # neighbours, and the diagnosis concluded there were no downstream
        # services - on a graph that plainly contained the edges. An empty
        # blast radius that is actually an identifier mismatch is exactly the
        # kind of confident wrong answer this system exists to avoid.
        services = await _resolve_service_ids(deps, services)

        if deps.graphrag is None or not services:
            reason = (
                "no graph client is configured"
                if deps.graphrag is None
                else "no candidate service was identified to expand from"
            )
            return {
                "evidence_gaps": [{"source": "neo4j", "reason": reason}],
                "topology": {},
            }

        await _publish(deps, incident_id, {"type": "phase", "phase": "CORRELATING"})
        try:
            context = await deps.graphrag.expand(
                services, incident_id=incident_id, depth=2, budget=40
            )
            evidence_items = await deps.graphrag.to_evidence(
                deps.evidence, incident_id, context
            )
            deps.budget.charge_tool()
        except SourceUnavailable as exc:
            await deps.evidence.record_unavailable(
                incident_id=incident_id,
                source="neo4j",
                source_type=SourceType.GRAPH,
                reason=str(exc),
            )
            log.warning("topology unavailable", incident_id=incident_id, error=str(exc))
            return {
                "evidence_gaps": [{"source": "neo4j", "reason": str(exc)}],
                "topology": {},
            }

        ids = [getattr(e, "id", str(e)) for e in evidence_items]
        summaries = [
            {
                "id": getattr(e, "id", ""),
                "summary": getattr(e, "summary", ""),
                "source": "neo4j",
                "trust_class": "TIER_B",
                "status": "UNVALIDATED",
            }
            for e in evidence_items
        ]
        blast = getattr(context, "blast_radius", None)
        topology: dict[str, Any] = {
            "services": list(getattr(context, "services", []) or [])[:40],
            "edges": list(getattr(context, "edges", []) or [])[:80],
            "causal_paths": list(getattr(context, "causal_paths", []) or [])[:10],
            "blast_radius_size": getattr(blast, "size", 0) if blast else 0,
        }

        await _record_agent_run(
            deps, incident_id, AgentRole.TOPOLOGY_ANALYST,
            task="expand the service graph around the affected services",
            summary=(
                f"{len(topology['services'])} services, "
                f"{len(topology['causal_paths'])} causal paths"
            ),
            evidence_ids=ids,
            duration_ms=int((time.perf_counter() - started) * 1000),
        )
        return {
            "topology": topology,
            "evidence_ids": ids,
            "evidence_summaries": summaries,
        }

    async def recall_memory(state: IncidentState) -> dict[str, Any]:
        """Surface prior incidents that resemble this one.

        Routed through the tool boundary rather than calling the recall module
        directly. Three things follow from that, and each one was previously
        missing here:

        * The invoker writes a ``tool_calls`` row, which is what six evaluation
          metrics are computed from.
        * The tool already separates "memory answered and holds no precedent"
          from "memory could not be searched". This node reimplemented that
          distinction badly once already - reading a field name that does not
          exist, so every lookup silently returned nothing - and the fix is to
          stop reimplementing it.
        * Evidence and provenance are recorded once, by the tool, with the
          matching provenance URI.

        Only verified, approved incident knowledge is ever stored, so what comes
        back is history rather than speculation.
        """
        deps.budget.check("recall_memory")
        started = time.perf_counter()
        incident_id = state["incident_id"]
        symptom = state.get("title", "")

        if deps.tools is None or not symptom.strip():
            # No tool boundary, or nothing to search on. Neither is a precedent
            # of absence, so neither is reported as one.
            reason = (
                "the tool boundary is not configured"
                if deps.tools is None
                else "the incident has no title to search memory with"
            )
            item = await deps.evidence.record_unavailable(
                incident_id=incident_id,
                source="incident_memory",
                source_type=SourceType.MEMORY,
                reason=reason,
            )
            return {
                "history": [],
                "evidence_ids": [item.id],
                "evidence_gaps": [{"source": "incident_memory", "reason": reason}],
            }

        # The invoker charges the budget for this call; charging here as well
        # would bill one read twice.
        result = await deps.tools.invoke(
            "similar_incidents",
            {
                "symptom": symptom[:2000],
                "services": list(state.get("affected_services") or [])[:32],
                "limit": 5,
            },
            _tool_context(deps, state, node="recall_memory"),
        )

        ids = list(result.evidence_ids)
        gaps: list[dict[str, str]] = []

        if result.degraded or not result.ok:
            # A degraded recall is emphatically not "no prior incident matched".
            # The gap goes into state as well as into evidence, because
            # `_gaps_digest` is what puts it in front of the model - recording
            # it only as evidence would leave the prompt reading as though
            # memory had been searched and found nothing.
            reason = _unavailable_reason(result)
            if not ids:
                item = await deps.evidence.record_unavailable(
                    incident_id=incident_id,
                    source="incident_memory",
                    source_type=SourceType.MEMORY,
                    reason=reason,
                )
                ids.append(item.id)
            gaps.append({"source": "incident_memory", "reason": reason})

        matches = getattr(result.value, "matches", ()) if result.value else ()
        history = [
            {
                "memory_id": match.memory_id,
                "incident_id": match.incident_id,
                "title": match.title,
                "root_cause": match.root_cause,
                "cause_category": match.cause_category,
                "successful_fix": match.successful_fix,
                "match_type": match.match_type,
                "reason": match.reason,
                "shared_services": list(match.shared_services),
                "occurrences": match.occurrences,
                "confidence": round(float(match.confidence), 4),
            }
            for match in matches[:5]
        ]

        if history:
            summary = f"{len(history)} similar prior incident(s)"
        elif gaps:
            summary = f"incident memory unavailable: {gaps[0]['reason']}"
        else:
            summary = "no matching prior incident"

        await _record_agent_run(
            deps, incident_id, AgentRole.EVIDENCE_INVESTIGATOR,
            task="search incident memory for recurrences",
            summary=summary,
            evidence_ids=ids,
            duration_ms=int((time.perf_counter() - started) * 1000),
            status="failed" if gaps and not history else "done",
        )
        return {"history": history, "evidence_ids": ids, "evidence_gaps": gaps}

    async def analyze_changes(state: IncidentState) -> dict[str, Any]:
        """Correlate the incident window with recent deployments and commits.

        Change is the most common cause of an incident, so this runs even when
        no hypothesis has named a service yet. GitHub is a soft dependency: an
        unreachable or unconfigured client becomes an evidence gap, never a
        claim that nothing changed - "we could not look" and "nothing changed"
        point at opposite conclusions.
        """
        deps.budget.check("analyze_changes")
        started = time.perf_counter()
        incident_id = state["incident_id"]
        services = state.get("affected_services") or state.get("candidate_services") or []

        if deps.github is None or not getattr(deps.github, "configured", False):
            reason = "github is not configured"
            await deps.evidence.record_unavailable(
                incident_id=incident_id,
                source="github",
                source_type=SourceType.VCS,
                reason=reason,
            )
            return {"changes": [], "evidence_gaps": [{"source": "github", "reason": reason}]}

        since = SYSTEM_CLOCK.now() - timedelta(hours=24)
        owner = deps.settings.github_default_owner
        if not owner:
            reason = "no default github owner is configured"
            await deps.evidence.record_unavailable(
                incident_id=incident_id,
                source="github",
                source_type=SourceType.VCS,
                reason=reason,
            )
            return {"changes": [], "evidence_gaps": [{"source": "github", "reason": reason}]}

        changes: list[dict[str, Any]] = []
        ids: list[str] = []
        gaps: list[dict[str, str]] = []

        for service in services[: deps.settings.agent_max_parallel_investigators]:
            repo = service.split(":")[-1]
            try:
                commits = await deps.github.recent_commits(
                    owner, repo, since=since, until=SYSTEM_CLOCK.now(), limit=10
                )
                deps.budget.charge_tool()
            except SourceUnavailable as exc:
                gaps.append({"source": f"github:{repo}", "reason": str(exc)})
                await deps.evidence.record_unavailable(
                    incident_id=incident_id,
                    source=f"github:{repo}",
                    source_type=SourceType.VCS,
                    reason=str(exc),
                )
                continue

            for commit in commits[:10]:
                sha = getattr(commit, "sha", "")
                message = getattr(commit, "message", None)
                # A commit message is attacker-influenceable free text, so it is
                # stored as Tier-D untrusted content rather than as a summary.
                body = (
                    message.text
                    if isinstance(message, UntrustedText)
                    else str(message or "")
                )
                item = await deps.evidence.record(
                    incident_id=incident_id,
                    source=f"github:{owner}/{repo}",
                    source_type=SourceType.VCS,
                    evidence_type=EvidenceType.CODE_CHANGE,
                    summary=f"commit {sha[:8]} on {repo} within the incident window",
                    structured_value={
                        "repo": f"{owner}/{repo}",
                        "sha": sha,
                        "author": str(getattr(commit, "author", "") or ""),
                        "authored_at": str(getattr(commit, "authored_at", "") or ""),
                    },
                    content=body[:4000],
                    untrusted=True,
                    provenance_uri=f"github://{owner}/{repo}/commit/{sha}",
                    resource_id=service,
                )
                ids.append(item.id)
                changes.append(
                    {
                        "repo": f"{owner}/{repo}",
                        "sha": sha,
                        "service": service,
                        "evidence_id": item.id,
                    }
                )

        await _record_agent_run(
            deps, incident_id, AgentRole.CHANGE_ANALYST,
            task="correlate recent changes with the incident window",
            summary=(
                f"{len(changes)} commits in the last 24h across "
                f"{len(services)} services"
            ),
            evidence_ids=ids,
            duration_ms=int((time.perf_counter() - started) * 1000),
        )
        return {
            "changes": changes,
            "evidence_ids": ids,
            "evidence_gaps": gaps,
        }

    async def localize_code(state: IncidentState) -> dict[str, Any]:
        """Narrow from a service to the specific files and symbols in question.

        Runs only once a diagnosis exists and did not abstain. Localising code
        for a cause we have not established would burn budget searching for
        something we cannot describe.
        """
        deps.budget.check("localize_code")
        started = time.perf_counter()
        incident_id = state["incident_id"]
        diagnosis = state.get("diagnosis") or {}
        # Set before the guards: "this process decided not to localise" is still
        # a decision this process made, and the debugger must not mistake it for
        # a lost handoff.
        deps.localization_attempted = True

        if deps.code is None or state.get("abstained") or not diagnosis.get("statement"):
            return {}

        try:
            localization = await deps.code.localize(
                incident_id,
                state.get("affected_services") or [],
                diagnosis.get("statement", ""),
                since=SYSTEM_CLOCK.now() - timedelta(hours=24),
            )
            deps.budget.charge_tool()
        except SourceUnavailable as exc:
            await deps.evidence.record_unavailable(
                incident_id=incident_id,
                source="code-retrieval",
                source_type=SourceType.VCS,
                reason=str(exc),
            )
            return {"evidence_gaps": [{"source": "code-retrieval", "reason": str(exc)}]}

        ids = await deps.code.to_evidence(deps.evidence, incident_id, localization)
        candidates = getattr(localization, "files", []) or []
        # Handed to debug_remediation through deps, not through state: see
        # WorkflowDeps.localization for why the payload stays out of the
        # checkpoint.
        deps.localization = localization
        await _record_agent_run(
            deps, incident_id, AgentRole.DEBUGGER,
            task="localise the responsible code",
            summary=(
                f"{len(candidates)} candidate files"
                if candidates
                else "no code candidate could be localised"
            ),
            evidence_ids=ids,
            duration_ms=int((time.perf_counter() - started) * 1000),
        )
        return {"evidence_ids": ids}

    async def execute_remediation(state: IncidentState) -> dict[str, Any]:
        """Execute a validated action, then prove or disprove that it worked.

        Reached only when every gate said yes. If the action is waiting on a
        human, or no action was proposed, this node does nothing - the approval
        arrives through the API and resumes the work from there, which is why
        the graph does not block here.
        """
        incident_id = state["incident_id"]
        validated = deps.pending_action
        deps.pending_action = None  # never executable twice from one run

        if validated is None or deps.executor is None or deps.ports is None:
            return {}

        await _publish(deps, incident_id, {"type": "phase", "phase": "REMEDIATING"})
        try:
            report = await deps.executor.execute(validated, deps.ports)
        except AegisError as exc:
            log.error(
                "remediation could not be executed",
                incident_id=incident_id,
                error=str(exc),
                code=exc.code,
            )
            await _publish(deps, incident_id, {
                "type": "remediation_failed", "error": exc.code,
            })
            return {"errors": [{"node": "execute_remediation", "error": str(exc)}]}

        await _record_agent_run(
            deps, incident_id, AgentRole.VERIFIER,
            task=f"execute and verify {validated.action_type.value}",
            summary=(
                f"{report.final_state.value}"
                + (f" - {report.escalation_reason}" if report.escalated else "")
            ),
            evidence_ids=[],
            duration_ms=int(
                (report.finished_at - report.started_at).total_seconds() * 1000
            ),
            status="done" if report.succeeded else "failed",
        )
        await _publish(deps, incident_id, {
            "type": "remediation_result",
            "succeeded": report.succeeded,
            "state": report.final_state.value,
            "verdict": (
                report.verification.verdict.value if report.verification else None
            ),
            "rolled_back": report.rolled_back,
            "escalated": report.escalated,
        })
        return {
            "remediation": report.as_json(),
            # The verdict is lifted out of the report and onto the state because
            # "was this change proved to work" is a first-class fact about the
            # run: the evaluation harness, the replay path and the timeline all
            # read it here. Left inside the report blob it was invisible, and
            # every verification metric downstream read as "not measured".
            # None when the engine produced no verdict - never a default.
            "verification_verdict": (
                report.verification.verdict.value if report.verification else None
            ),
            "phase": "MONITORING" if report.succeeded else "ESCALATED",
        }

    async def learn(state: IncidentState) -> dict[str, Any]:
        """Persist what was confirmed, and only what was confirmed.

        The memory store refuses anything abstained or unverified. That refusal
        is the point: unverified speculation entering memory would contaminate
        every future retrieval, and a wrong prior is worse than no prior.
        """
        incident_id = state["incident_id"]
        remediation = state.get("remediation") or {}

        if deps.memory_store is None or state.get("abstained"):
            return {}
        if not remediation.get("succeeded"):
            log.info(
                "nothing verified to learn from",
                incident_id=incident_id,
                remediation_state=remediation.get("final_state"),
            )
            return {}

        try:
            await deps.memory_store.write(
                diagnosis=state.get("diagnosis") or {},
                verification=remediation,
                title=state.get("title", ""),
                symptoms=state.get("title", ""),
                successful_fix=remediation.get("performed", ""),
                approved_by="system:autonomous"
                if remediation.get("autonomous", True)
                else "human",
            )
        except AegisError as exc:
            # A refusal here is the contamination gate doing its job. It is
            # logged at INFO, not ERROR: nothing went wrong.
            log.info(
                "incident memory declined the write",
                incident_id=incident_id,
                reason=str(exc),
                code=exc.code,
            )
            return {}

        if deps.audit is not None:
            await deps.audit.record(
                event_type="memory.written",
                actor="system:learning",
                actor_type="system",
                incident_id=incident_id,
            )
        return {}

    # Imported here rather than at module scope: debugger.py imports this
    # module's helpers, and a module-level import in both directions would be a
    # cycle. The node is built exactly like the others, closed over deps.
    from aegis.agents.debugger import make_debug_remediation

    return {
        "triage": triage,
        "investigate": investigate,
        "analyze_topology": analyze_topology,
        "recall_memory": recall_memory,
        "analyze_changes": analyze_changes,
        "hypothesize": hypothesize,
        "diagnose": diagnose,
        "localize_code": localize_code,
        "debug_remediation": make_debug_remediation(deps),
        "plan_remediation": plan_remediation,
        "execute_remediation": execute_remediation,
        "learn": learn,
        "finalize": finalize,
    }


# --------------------------------------------------------------------------- #
# the safety boundary                                                          #
# --------------------------------------------------------------------------- #


async def evaluate_action_policy(
    deps: WorkflowDeps,
    *,
    incident_id: str,
    action_type: Any,
    target_resource_id: str,
    service_id: str | None,
    environment: str,
    severity: str,
    reason: str,
    supporting_evidence: list[str],
    blast_radius_services: list[str],
    has_rollback: bool,
    rollback_description: str,
    expected_metric: str | None,
    expected_direction: str | None,
    expected_threshold: float | None,
    confidence: float,
    abstained: bool,
    arguments: dict[str, Any] | None = None,
    correlation_id: str = "",
) -> tuple[dict[str, Any], dict[str, Any], Any]:
    """Turn a model suggestion into a gated proposal.

    The model's output reaches this function as loose values and leaves it as
    either a ``ValidatedAction`` - which an executor will accept - or a
    ``GateRejection`` explaining every gate that held it back. Nothing in
    between exists, and this function cannot produce a ValidatedAction itself:
    it delegates to ``ActionGate``, which owns the only constructor.

    Returns ``(decision_dict, proposal_dict, validated_or_none)``. The dicts are
    what the workflow state and the UI consume; the third value is the thing
    that can actually be executed, and it is ``None`` whenever a gate said no.
    """
    from aegis.core.ids import ACTION, new_id
    from aegis.domain.enums import MetricDirection, Severity
    from aegis.domain.models import (
        ActionProposal,
        BlastRadius,
        ExpectedEffect,
        ResourceRef,
        RollbackPlan,
        VerificationPlan,
    )
    from aegis.execution.validated import GateRejection, ValidatedAction

    if deps.gate is None:
        # No gate wired means no write path exists. Refusing loudly is correct:
        # silently returning "allowed" would be catastrophic, and silently
        # returning "blocked" would hide a misconfiguration.
        log.error("no action gate is configured; refusing to consider remediation")
        return (
            {
                "effect": "BLOCK",
                "risk_tier": 3,
                "matched_rule": "gate_unavailable",
                "reasons": ["the action gate is not configured in this process"],
                "gates": [],
                "policy_version": "n/a",
            },
            {},
            None,
        )

    direction = MetricDirection.DECREASE
    if expected_direction in {d.value for d in MetricDirection}:
        direction = MetricDirection(expected_direction)

    # A missing verification plan is not substituted with a default that would
    # pass. The schema gate rejects an unmeasurable action, which is the
    # intended outcome (ESD: an action whose success cannot be measured cannot
    # be proposed).
    verification = VerificationPlan(
        target_metric=expected_metric or "",
        direction=direction,
        threshold=expected_threshold if expected_threshold is not None else 0.0,
        protected_metrics=["latency_p99", "request_rate"],
    )
    rollback = (
        RollbackPlan(
            strategy="compensating_action",
            description=rollback_description or "operator-defined",
            automatic=False,
        )
        if has_rollback
        else None
    )
    resource_id = target_resource_id or service_id or ""
    proposal = ActionProposal(
        id=new_id(ACTION),
        incident_id=incident_id,
        action_type=action_type,
        target=ResourceRef(
            resource_type="instance" if target_resource_id else "service",
            resource_id=resource_id,
            environment=environment,
            service_id=service_id,
        ),
        reason=reason,
        supporting_evidence=supporting_evidence,
        expected_effect=ExpectedEffect(
            metric=expected_metric or "error_rate",
            direction=direction,
            threshold=expected_threshold if expected_threshold is not None else 0.0,
        ),
        blast_radius=BlastRadius(
            directly_affected=[service_id] if service_id else [],
            downstream=[s for s in blast_radius_services if s != service_id],
        ),
        rollback=rollback,
        verification=verification,
        arguments=arguments or {},
        # Deterministic: the same suggestion for the same incident and resource
        # collapses to one action rather than being proposed twice.
        idempotency_key=f"{incident_id}:{action_type.value}:{resource_id}",
        proposed_at=SYSTEM_CLOCK.now(),
    )

    severity_enum = (
        Severity(severity) if severity in Severity.__members__ else Severity.P3
    )
    outcome = await deps.gate.validate(
        proposal,
        severity=severity_enum,
        diagnosis_confidence=confidence,
        has_abstained_diagnosis=abstained,
        correlation_id=correlation_id,
        holder=f"worker:{incident_id}",
    )

    decision: dict[str, Any]
    if isinstance(outcome, ValidatedAction):
        decision = {
            "effect": outcome.decision.effect.value,
            "risk_tier": int(outcome.decision.risk_tier),
            "matched_rule": outcome.decision.matched_rule,
            "reasons": list(outcome.decision.reasons),
            "gates": [g.model_dump() for g in outcome.decision.gates],
            "policy_version": outcome.decision.policy_version,
            "autonomous": not outcome.was_human_approved,
        }
        action_id = outcome.action.id
        state_value = outcome.action.state.value
        validated: Any = outcome
    else:
        rejection: GateRejection = outcome
        decision = {
            "effect": rejection.effect.value,
            "risk_tier": int(rejection.risk_tier),
            "matched_rule": rejection.matched_rule,
            "reasons": list(rejection.reasons),
            "gates": [g.model_dump() for g in rejection.gates],
            "policy_version": "1.0.0",
            "needs_approval": rejection.needs_approval,
            "approval_id": rejection.approval_id,
        }
        action_id = rejection.action_id
        state_value = "HUMAN_REQUIRED" if rejection.needs_approval else "BLOCKED"
        validated = None

    return (
        decision,
        {
            "id": action_id,
            "action_type": action_type.value,
            "resource_id": resource_id,
            "service_id": service_id,
            "reason": reason,
            "arguments": dict(proposal.arguments),
            "blast_radius": proposal.blast_radius.model_dump(),
            "has_rollback": has_rollback,
            "verification": verification.model_dump(),
            "state": state_value,
        },
        validated,
    )


# --------------------------------------------------------------------------- #
# graph assembly                                                               #
# --------------------------------------------------------------------------- #


def _after_diagnose(state: IncidentState) -> str:
    """Bounded loop decision.

    Re-investigating requires ALL of: the diagnosis abstained, the loop counter
    is under its limit, and budget remains. Any one failing ends the loop, so
    the workflow provably terminates.
    """
    if not state.get("abstained"):
        return "plan_remediation"
    loops = int(state.get("loop_count") or 0)
    limit = int((state.get("budget") or {}).get("hypothesis_loop_limit", 0))
    if loops >= limit:
        return "finalize"
    return "investigate"


def build_workflow(deps: WorkflowDeps) -> Any:
    """Compile the investigation graph.

    A Postgres checkpointer is used when available so a worker killed mid-run
    resumes from its last completed node instead of restarting the
    investigation. Without it the graph still runs, just without resume.
    """
    from langgraph.graph import END, START, StateGraph

    nodes = make_nodes(deps)
    graph = StateGraph(IncidentState)

    for name, fn in nodes.items():
        # The single_agent ablation does not merely bypass the enrichment
        # branches, it never registers them: a node that is not in the compiled
        # graph cannot be reached by any edge, so the arm is structurally
        # incapable of the fan-out rather than relying on a runtime flag check.
        if deps.single_agent and name in ENRICHMENT_NODES:
            continue
        graph.add_node(name, fn)

    graph.add_edge(START, "triage")
    graph.add_edge("triage", "investigate")

    # Topology, memory and change analysis are independent of one another and
    # all depend only on what investigate found, so they fan out in parallel.
    # Their evidence lists merge through the reducer on IncidentState rather
    # than overwriting, so no branch can erase another's findings.
    if deps.single_agent:
        graph.add_edge("investigate", "hypothesize")
    else:
        for branch in ENRICHMENT_NODES:
            graph.add_edge("investigate", branch)
            graph.add_edge(branch, "hypothesize")

    graph.add_edge("hypothesize", "diagnose")

    def _count_loop(state: IncidentState) -> str:
        return _after_diagnose(state)

    graph.add_conditional_edges(
        "diagnose",
        _count_loop,
        {
            "investigate": "investigate",
            "plan_remediation": "localize_code",
            "finalize": "finalize",
        },
    )
    # Code localisation runs before planning so a remediation can reference the
    # specific change it is reacting to rather than the service in general, and
    # debugging sits between the two: a candidate patch is written, checked and
    # run in a container while the planner is still deciding whether any
    # environment action is warranted at all. Neither step can execute the
    # other's output - the patch never reaches an environment, and promoting one
    # is PROMOTE_PATCH, which has no executor.
    graph.add_edge("localize_code", "debug_remediation")
    graph.add_edge("debug_remediation", "plan_remediation")

    def _after_plan(state: IncidentState) -> str:
        """Execute, or stop and wait for a human.

        Waiting is not a loop: the graph ends, and an approval arriving through
        the API enqueues fresh work. Blocking a worker on a human decision would
        hold its budget and its resources for the whole approval TTL.
        """
        if state.get("awaiting_approval"):
            return "finalize"
        if not state.get("proposed_action"):
            return "finalize"
        return "execute_remediation"

    graph.add_conditional_edges(
        "plan_remediation",
        _after_plan,
        {"execute_remediation": "execute_remediation", "finalize": "finalize"},
    )
    graph.add_edge("execute_remediation", "learn")
    graph.add_edge("learn", "finalize")
    graph.add_edge("finalize", END)

    return graph.compile()


async def run_investigation(
    deps: WorkflowDeps,
    *,
    incident_id: str,
    title: str,
    severity: str,
    environment: str,
    workload: str,
    correlation_id: str,
) -> IncidentState:
    """Execute one investigation to completion.

    ``BudgetExhausted`` is caught and treated as a normal outcome: the partial
    state is preserved, the incident is marked escalated by the caller, and
    nothing is fabricated to fill the gap (ESD 33).
    """
    workflow = build_workflow(deps)
    initial: IncidentState = {
        "incident_id": incident_id,
        "correlation_id": correlation_id,
        "title": title,
        "severity": severity,
        "environment": environment,
        "workload": workload,
        "phase": "RECEIVED",
        "evidence_ids": [],
        "evidence_summaries": [],
        "evidence_gaps": [],
        "hypotheses": [],
        "errors": [],
        "loop_count": 0,
        "abstained": False,
        "confidence": 0.0,
        "budget": {"hypothesis_loop_limit": deps.settings.agent_hypothesis_loop_limit},
    }

    try:
        result = await workflow.ainvoke(
            initial,
            config={
                # Two supersteps per investigation loop, plus one for each node
                # on the linear tail. Raised from 12 to 14 when
                # debug_remediation joined the graph: a limit that no longer
                # covers the tail would abort a run at the last node rather than
                # at a real bound, and would look like a workflow bug.
                "recursion_limit": 2 * deps.settings.agent_hypothesis_loop_limit + 14,
                "configurable": {"thread_id": incident_id},
            },
        )
    except BudgetExhausted as exc:
        log.warning("investigation stopped on budget", incident_id=incident_id,
                    reason=exc.message)
        return {**initial, "finished": True, "abstained": True,
                "budget": deps.budget.snapshot(),
                "errors": [{"type": "budget", "message": exc.message}]}

    return result  # type: ignore[no-any-return]
