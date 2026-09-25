"""Observe, act and verify tools for the horizon loop.

Observe tools are thin adapters over sources that already exist: the MCP tool
catalogue through ``ToolInvoker`` (schema-checked, scope-checked, budgeted and
recorded in ``tool_calls``), plus the known-issue search, RawTree named queries
and RawTree's allowlisted agent tools. Every observation is handled the same
way, whatever produced it:

    raw -> Postgres evidence (the id the card and every citation use)
        -> HorizonStore observation + RawTree observation (episodic memory)
        -> Compactor -> one EvidenceCard (what the model sees)

"Could not look" produces a gap card (``UNAVAILABLE: ...``, weight 0) backed by
a ``SOURCE_UNAVAILABLE`` evidence row. It is never compacted into "found
nothing", which gets an ordinary card saying the source answered empty.

The act tool only *builds* an ``ActionProposal``; the orchestrator hands it to
``ActionGate``. Verification is deterministic and runs outside the brain.
"""

# Handlers share one signature so the dispatch table stays uniform; not every
# handler needs every argument.
# ruff: noqa: ARG001, ARG002

from __future__ import annotations

import asyncio
import json
import re
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Final, Protocol

from aegis.agents.horizon.compactor import Compactor
from aegis.agents.horizon.context import estimate_tokens
from aegis.agents.horizon.ports import (
    CompactionInput,
    HorizonStore,
    KnownIssueSearch,
    QueryResult,
    RawTreePort,
    RemoteToolset,
    StoredObservation,
    ToolSpec,
)
from aegis.core.clock import SYSTEM_CLOCK, Clock
from aegis.core.errors import SourceUnavailable
from aegis.core.ids import EVIDENCE, new_id
from aegis.core.logging import get_logger
from aegis.domain.enums import (
    ActionType,
    ClaimOutcome,
    EvidenceType,
    MetricDirection,
    SourceType,
)
from aegis.domain.horizon import (
    MAX_MEMORY_CARDS,
    EvidenceCard,
    HorizonState,
    MemoryCard,
    Source,
)
from aegis.domain.models import (
    ActionProposal,
    BlastRadius,
    ExpectedEffect,
    ResourceRef,
    RollbackPlan,
    VerificationPlan,
)
from aegis.mcp.types import ToolContext, ToolResult

log = get_logger(__name__)

# --------------------------------------------------------------------------- #
# tool names                                                                   #
# --------------------------------------------------------------------------- #

QUERY_METRICS: Final = "query_metrics"
INSTANCE_STATUS: Final = "instance_status"
DEPLOYMENT_HISTORY: Final = "deployment_history"
CONTAINER_LOGS: Final = "container_logs"
SEARCH_TRACES: Final = "search_traces"
SEARCH_KNOWN_ISSUES: Final = "search_known_issues"
QUERY_HISTORY: Final = "query_history"
RECALL_MEMORY: Final = "recall_memory"
PROPOSE_REMEDIATION: Final = "propose_remediation"
RAWTREE_PREFIX: Final = "rawtree__"

# Tools that count towards the "distinct observe tools" guard. Memory recall is
# deliberately excluded: remembering a past incident is not observing this one.
OBSERVE_TOOLS: Final = frozenset(
    {
        QUERY_METRICS,
        INSTANCE_STATUS,
        DEPLOYMENT_HISTORY,
        CONTAINER_LOGS,
        SEARCH_TRACES,
        SEARCH_KNOWN_ISSUES,
        QUERY_HISTORY,
    }
)

METRICS: Final = (
    "error_rate",
    "latency_p99",
    "request_rate",
    "cpu_utilisation",
    "memory_bytes",
    "pool_saturation",
    "queue_depth",
    "cache_hit_ratio",
    "restart_count",
)
NAMED_QUERIES: Final = ("action_success_rate", "incident_timeline")
METRIC_WINDOW_S: Final = 900
LOG_LINES: Final = 200
MAX_RAW_CHARS: Final = 200_000

# Weights are code-assigned by source kind; they feed derived confidence, so a
# model cannot choose them. Tier-A machine observations weigh most; free text
# (logs, web pages) least.
WEIGHTS: Final[dict[str, float]] = {
    QUERY_METRICS: 0.9,
    INSTANCE_STATUS: 0.8,
    DEPLOYMENT_HISTORY: 0.8,
    CONTAINER_LOGS: 0.4,
    SEARCH_TRACES: 0.8,
    SEARCH_KNOWN_ISSUES: 0.6,
    QUERY_HISTORY: 0.7,
    RECALL_MEMORY: 0.5,
    "verify_recovery": 0.85,
    RAWTREE_PREFIX: 0.6,
}
EMPTY_FACTOR: Final = 0.5

_SERVICE: Final[dict[str, Any]] = {"type": "string", "maxLength": 120}

SPECS: Final[dict[str, ToolSpec]] = {
    QUERY_METRICS: ToolSpec(
        QUERY_METRICS,
        "Summarise one metric for a service over the last 15 minutes "
        "(latest/peak/mean). Closed metric catalogue.",
        {
            "type": "object",
            "properties": {
                "service": _SERVICE,
                "metric": {"type": "string", "enum": list(METRICS)},
            },
            "required": ["service", "metric"],
            "additionalProperties": False,
        },
    ),
    INSTANCE_STATUS: ToolSpec(
        INSTANCE_STATUS,
        "Running instances behind a service: health, version, restart count.",
        {
            "type": "object",
            "properties": {"service": _SERVICE},
            "required": ["service"],
            "additionalProperties": False,
        },
    ),
    DEPLOYMENT_HISTORY: ToolSpec(
        DEPLOYMENT_HISTORY,
        "Current version and recent deployments of a service.",
        {
            "type": "object",
            "properties": {"service": _SERVICE},
            "required": ["service"],
            "additionalProperties": False,
        },
    ),
    CONTAINER_LOGS: ToolSpec(
        CONTAINER_LOGS,
        "Recent stdout of the service's first instance, compacted to failure signals.",
        {
            "type": "object",
            "properties": {"service": _SERVICE},
            "required": ["service"],
            "additionalProperties": False,
        },
    ),
    SEARCH_TRACES: ToolSpec(
        SEARCH_TRACES,
        "Recent slow traces rooted at a service.",
        {
            "type": "object",
            "properties": {"service": _SERVICE},
            "required": ["service"],
            "additionalProperties": False,
        },
    ),
    SEARCH_KNOWN_ISSUES: ToolSpec(
        SEARCH_KNOWN_ISSUES,
        "Search public issue trackers for a known defect in a component version.",
        {
            "type": "object",
            "properties": {
                "component": {"type": "string", "maxLength": 120},
                "version": {"type": "string", "maxLength": 60},
            },
            "required": ["component", "version"],
            "additionalProperties": False,
        },
    ),
    QUERY_HISTORY: ToolSpec(
        QUERY_HISTORY,
        "Run a named historical query: action_success_rate (how often each remedy "
        "verified for this symptom) or incident_timeline.",
        {
            "type": "object",
            "properties": {"name": {"type": "string", "enum": list(NAMED_QUERIES)}},
            "required": ["name"],
            "additionalProperties": False,
        },
    ),
    RECALL_MEMORY: ToolSpec(
        RECALL_MEMORY,
        "Recall up to 3 resolved incidents similar to a symptom description.",
        {
            "type": "object",
            "properties": {"symptom": {"type": "string", "maxLength": 300}},
            "required": ["symptom"],
            "additionalProperties": False,
        },
    ),
    PROPOSE_REMEDIATION: ToolSpec(
        PROPOSE_REMEDIATION,
        "Propose one remediation for the gate chain. Policy decides whether it runs, "
        "needs human approval, or is blocked.",
        {
            "type": "object",
            "properties": {
                "action_type": {"type": "string", "enum": [a.value for a in ActionType]},
                "target": _SERVICE,
                "arguments": {
                    "type": "object",
                    "properties": {
                        "to_version": {"type": ["string", "null"]},
                        "replica_delta": {"type": ["integer", "null"]},
                        "cache_key": {"type": ["string", "null"]},
                    },
                    "required": ["to_version", "replica_delta", "cache_key"],
                    "additionalProperties": False,
                },
                "hypothesis_id": {"type": "string"},
                "evidence_ids": {"type": "array", "items": {"type": "string"}, "maxItems": 12},
            },
            "required": ["action_type", "target", "arguments", "hypothesis_id", "evidence_ids"],
            "additionalProperties": False,
        },
    ),
}


# --------------------------------------------------------------------------- #
# ports this module needs from outside                                         #
# --------------------------------------------------------------------------- #


class EvidenceRecorder(Protocol):
    """The slice of ``evidence.EvidenceStore`` the horizon loop writes through."""

    async def record(self, **kw: Any) -> Any: ...

    async def record_unavailable(
        self, *, incident_id: str, source: str, source_type: SourceType, reason: str
    ) -> Any: ...


class ToolInvokerPort(Protocol):
    async def invoke(
        self, name: str, arguments: dict[str, Any], context: ToolContext, **kw: Any
    ) -> ToolResult: ...


class MemoryRecallPort(Protocol):
    async def similar(
        self, incident_id_or_symptom: str, services: Any = (), limit: int = 5, **kw: Any
    ) -> Any: ...


@dataclass(frozen=True, slots=True)
class ServiceNaming:
    """Bare names for Prometheus labels, canonical ids for runtime and actions."""

    environment: str
    workload: str

    def canonical(self, service: str) -> str:
        return (
            service if service.count(":") == 2 else f"{self.environment}:{self.workload}:{service}"
        )

    @staticmethod
    def bare(service: str) -> str:
        return service.rsplit(":", 1)[-1]


@dataclass(slots=True)
class Observation:
    """One observe call's result, as the orchestrator consumes it."""

    card: EvidenceCard
    tokens_raw: int
    status: str  # ok | empty | gap
    origin: Source
    duration_ms: int
    memory: list[MemoryCard] = field(default_factory=list)
    query: QueryResult | None = None


@dataclass(slots=True)
class _Raw:
    """What a handler produced, before storage and compaction."""

    raw: str
    structured: dict[str, Any] | None
    origin: Source
    source_name: str
    source_type: SourceType
    evidence_type: EvidenceType
    unstructured: bool = False
    gap_reason: str | None = None
    empty: bool = False
    evidence_ids: tuple[str, ...] = ()
    untrusted: bool = False
    url: str | None = None
    memory: list[MemoryCard] = field(default_factory=list)
    query: QueryResult | None = None


# --------------------------------------------------------------------------- #
# observer                                                                     #
# --------------------------------------------------------------------------- #


class Observer:
    """Runs observe tools and turns every result into a stored, compacted card."""

    MAX_INSTANCE_CACHE: Final = 256

    def __init__(
        self,
        *,
        store: HorizonStore,
        compactor: Compactor,
        invoker: ToolInvokerPort | None = None,
        evidence: EvidenceRecorder | None = None,
        rawtree: RawTreePort | None = None,
        rawtree_tools: RemoteToolset | None = None,
        known_issues: KnownIssueSearch | None = None,
        memory_recall: MemoryRecallPort | None = None,
        clock: Clock = SYSTEM_CLOCK,
    ) -> None:
        self._store = store
        self._compactor = compactor
        self._invoker = invoker
        self._evidence = evidence
        self._rawtree = rawtree
        self._rawtree_tools = rawtree_tools
        self._known = known_issues
        self._recall = memory_recall
        self._clock = clock
        self._instances: OrderedDict[str, list[str]] = OrderedDict()

    # ---- catalogue -------------------------------------------------------- #

    def specs(self) -> list[ToolSpec]:
        base = [
            SPECS[n]
            for n in (
                QUERY_METRICS,
                INSTANCE_STATUS,
                DEPLOYMENT_HISTORY,
                CONTAINER_LOGS,
                SEARCH_TRACES,
                SEARCH_KNOWN_ISSUES,
                QUERY_HISTORY,
                RECALL_MEMORY,
            )
        ]
        return base + self.remote_specs()

    def remote_specs(self) -> list[ToolSpec]:
        if self._rawtree_tools is None or not self._rawtree_tools.available:
            return []
        try:
            specs = self._rawtree_tools.tool_specs()
        except Exception as exc:  # noqa: BLE001 - a remote catalogue is optional
            log.warning("rawtree agent tools unavailable", error=type(exc).__name__)
            return []
        # Only names under the prefix: the toolset already allowlists, and this
        # second filter means a toolset bug cannot surface an unprefixed name
        # that shadows a local tool.
        return [s for s in specs if s.name.startswith(RAWTREE_PREFIX)]

    def handles(self, name: str) -> bool:
        return name in SPECS and name != PROPOSE_REMEDIATION or name.startswith(RAWTREE_PREFIX)

    # ---- entry point ------------------------------------------------------- #

    async def observe(
        self,
        name: str,
        args: dict[str, Any],
        state: HorizonState,
        naming: ServiceNaming,
        context: ToolContext | None,
    ) -> Observation:
        started = time.perf_counter()
        handler = self._handler(name)
        try:
            raw = await handler(args, state, naming, context)
        except SourceUnavailable as exc:
            raw = _gap(name, exc.message)
        except (ValueError, TypeError, KeyError) as exc:
            # Malformed arguments from the model: recorded as a refused read,
            # never as a finding.
            raw = _gap(name, f"arguments rejected: {type(exc).__name__}")
        duration_ms = int((time.perf_counter() - started) * 1000)
        return await self._finish(name, raw, state, duration_ms)

    def _handler(
        self, name: str
    ) -> Callable[
        [dict[str, Any], HorizonState, ServiceNaming, ToolContext | None], Awaitable[_Raw]
    ]:
        table: dict[str, Callable[..., Awaitable[_Raw]]] = {
            QUERY_METRICS: self._metrics,
            INSTANCE_STATUS: self._instances_status,
            DEPLOYMENT_HISTORY: self._deployments,
            CONTAINER_LOGS: self._logs,
            SEARCH_TRACES: self._traces,
            SEARCH_KNOWN_ISSUES: self._known_issues,
            QUERY_HISTORY: self._history,
            RECALL_MEMORY: self._memory,
        }
        if name in table:
            return table[name]
        if name.startswith(RAWTREE_PREFIX):
            return self._remote(name)
        raise KeyError(name)

    # ---- storage + compaction ---------------------------------------------- #

    async def _finish(
        self, name: str, raw: _Raw, state: HorizonState, duration_ms: int
    ) -> Observation:
        text = raw.raw[:MAX_RAW_CHARS]
        evidence_id = await self._evidence_id(raw, state)
        weight_key = RAWTREE_PREFIX if name.startswith(RAWTREE_PREFIX) else name
        weight = WEIGHTS.get(weight_key, 0.5) * (EMPTY_FACTOR if raw.empty else 1.0)
        card = await self._compactor.compact(
            CompactionInput(
                evidence_id=evidence_id,
                step=state.step,
                tool=name,
                origin=raw.origin,
                raw=text,
                structured=raw.structured,
                url=raw.url,
                hypothesis_ids=tuple(h.id for h in state.hypotheses),
            ),
            weight=weight,
            unstructured=raw.unstructured and not raw.empty,
            gap_reason=raw.gap_reason,
        )
        await self._store.save_observation(
            StoredObservation(
                evidence_id=evidence_id,
                incident_id=state.incident_id,
                tool=name,
                raw=text,
                card=card,
                extra={"structured": raw.structured or {}, "status": _status(raw)},
            )
        )
        if self._rawtree is not None:
            try:
                self._rawtree.enqueue_observation(
                    incident_id=state.incident_id,
                    evidence_id=evidence_id,
                    tool=name,
                    raw=text,
                    ts=self._clock.now(),
                )
            except Exception as exc:  # noqa: BLE001 - the port promises not to raise
                log.warning("rawtree enqueue_observation failed", error=type(exc).__name__)
        return Observation(
            card=card,
            tokens_raw=estimate_tokens(text),
            status=_status(raw),
            origin=raw.origin,
            duration_ms=duration_ms,
            memory=raw.memory,
            query=raw.query,
        )

    async def _evidence_id(self, raw: _Raw, state: HorizonState) -> str:
        """Postgres evidence id when available: the gate validates citations there."""
        if raw.evidence_ids:
            return raw.evidence_ids[0]
        if self._evidence is None:
            return new_id(EVIDENCE)
        if raw.gap_reason is not None:
            item = await self._evidence.record_unavailable(
                incident_id=state.incident_id,
                source=raw.source_name,
                source_type=raw.source_type,
                reason=raw.gap_reason[:300],
            )
        else:
            item = await self._evidence.record(
                incident_id=state.incident_id,
                source=raw.source_name,
                source_type=raw.source_type,
                evidence_type=raw.evidence_type,
                summary=raw.raw[:300] if not raw.untrusted else f"{raw.source_name} observation",
                structured_value=raw.structured or {},
                content=raw.raw[:20_000] if raw.untrusted else None,
                untrusted=raw.untrusted,
                provenance_uri=f"horizon://{raw.source_name}/{state.incident_id}/{state.step}",
            )
        return str(item.id)

    # ---- MCP-backed handlers ------------------------------------------------ #

    async def _invoke(
        self, name: str, args: dict[str, Any], context: ToolContext | None
    ) -> ToolResult:
        if self._invoker is None or context is None:
            raise SourceUnavailable("the tool boundary is not configured; nothing was read")
        return await self._invoker.invoke(name, args, context)

    async def _metrics(
        self,
        args: dict[str, Any],
        state: HorizonState,
        naming: ServiceNaming,
        context: ToolContext | None,
    ) -> _Raw:
        service = naming.bare(str(args["service"]))
        metric = str(args["metric"])
        if metric not in METRICS:
            raise ValueError(metric)
        result = await self._invoke(
            "query_metric_range",
            {"service": service, "metric": metric, "window_s": METRIC_WINDOW_S},
            context,
        )
        if not result.ok or result.degraded:
            return _gap_from(QUERY_METRICS, result, "prometheus", SourceType.METRICS)
        value = result.value.model_dump(mode="json") if result.value is not None else {}
        structured = {
            "kind": "metric",
            "service": service,
            "metric": metric,
            "latest": value.get("latest"),
            "peak": value.get("peak"),
            "mean": value.get("mean"),
            "point_count": value.get("point_count", 0),
            "window_s": METRIC_WINDOW_S,
        }
        return _Raw(
            raw=json.dumps(value, default=str),
            structured=structured,
            origin=Source.PROMETHEUS,
            source_name="prometheus",
            source_type=SourceType.METRICS,
            evidence_type=EvidenceType.METRIC_SERIES,
            empty=result.found_nothing,
            evidence_ids=result.evidence_ids,
        )

    async def _instances_status(
        self,
        args: dict[str, Any],
        state: HorizonState,
        naming: ServiceNaming,
        context: ToolContext | None,
    ) -> _Raw:
        service_id = naming.canonical(str(args["service"]))
        result = await self._invoke("list_instances", {"service_id": service_id}, context)
        if not result.ok or result.degraded:
            return _gap_from(INSTANCE_STATUS, result, "runtime", SourceType.RUNTIME)
        value = result.value.model_dump(mode="json") if result.value is not None else {}
        instances = value.get("instances") or []
        self._remember_instances(
            state.incident_id,
            service_id,
            [str(i.get("instance_id")) for i in instances if i.get("instance_id")],
        )
        structured = {
            "kind": "instances",
            "service": naming.bare(service_id),
            "instances": [
                {k: i.get(k) for k in ("instance_id", "name", "health", "version", "restart_count")}
                for i in instances[:10]
            ],
        }
        return _Raw(
            raw=json.dumps(value, default=str),
            structured=structured,
            origin=Source.RUNTIME,
            source_name="runtime",
            source_type=SourceType.RUNTIME,
            evidence_type=EvidenceType.INSTANCE_STATE,
            empty=result.found_nothing,
            evidence_ids=result.evidence_ids,
        )

    async def _deployments(
        self,
        args: dict[str, Any],
        state: HorizonState,
        naming: ServiceNaming,
        context: ToolContext | None,
    ) -> _Raw:
        service_id = naming.canonical(str(args["service"]))
        current = await self._invoke("current_deployment", {"service_id": service_id}, context)
        history = await self._invoke(
            "deployment_history", {"service_id": service_id, "limit": 5}, context
        )
        usable = [r for r in (current, history) if r.ok and not r.degraded]
        if not usable:
            return _gap_from(DEPLOYMENT_HISTORY, current, "runtime", SourceType.DEPLOYMENT)
        cur = current.value.model_dump(mode="json") if current.ok and current.value else {}
        hist = history.value.model_dump(mode="json") if history.ok and history.value else {}
        images = cur.get("images") or []
        structured = {
            "kind": "deployments",
            "service": naming.bare(service_id),
            "current": cur.get("version"),
            "image": images[0] if images else None,
            "history": [d.get("version") for d in hist.get("deployments") or []],
        }
        ids = tuple(i for r in usable for i in r.evidence_ids)
        return _Raw(
            raw=json.dumps({"current": cur, "history": hist}, default=str),
            structured=structured,
            origin=Source.RUNTIME,
            source_name="runtime",
            source_type=SourceType.DEPLOYMENT,
            evidence_type=EvidenceType.DEPLOYMENT_EVENT,
            empty=not cur.get("version") and not hist.get("deployments"),
            evidence_ids=ids,
        )

    async def _logs(
        self,
        args: dict[str, Any],
        state: HorizonState,
        naming: ServiceNaming,
        context: ToolContext | None,
    ) -> _Raw:
        service_id = naming.canonical(str(args["service"]))
        instance = await self.instance_for(state, service_id, naming, context)
        if instance is None:
            return _gap(
                CONTAINER_LOGS,
                "no running instance could be resolved for logs",
                source_type=SourceType.LOGS,
            )
        result = await self._invoke(
            "instance_logs", {"instance_id": instance, "lines": LOG_LINES}, context
        )
        if not result.ok or result.degraded:
            return _gap_from(CONTAINER_LOGS, result, "runtime.logs", SourceType.LOGS)
        value = result.value.model_dump(mode="json") if result.value is not None else {}
        lines = [str(ln.get("text", "")) for ln in value.get("lines") or [] if isinstance(ln, dict)]
        return _Raw(
            raw="\n".join(lines),
            structured={"instance_id": instance},
            origin=Source.RUNTIME,
            source_name="runtime.logs",
            source_type=SourceType.LOGS,
            evidence_type=EvidenceType.LOG_MATCH,
            unstructured=True,
            untrusted=True,
            empty=not lines,
            # The logs tool does not record evidence itself; this module does,
            # as untrusted Tier-D content.
        )

    async def _traces(
        self,
        args: dict[str, Any],
        state: HorizonState,
        naming: ServiceNaming,
        context: ToolContext | None,
    ) -> _Raw:
        service = naming.bare(str(args["service"]))
        result = await self._invoke(
            "search_traces",
            {"service": service, "window_s": METRIC_WINDOW_S, "limit": 20},
            context,
        )
        if not result.ok or result.degraded:
            return _gap_from(SEARCH_TRACES, result, "tempo", SourceType.TRACES)
        value = result.value.model_dump(mode="json") if result.value is not None else {}
        traces = value.get("traces") or []
        structured = {
            "kind": "traces",
            "service": service,
            "count": len(traces),
            "slowest_ms": max((t.get("duration_ms", 0.0) for t in traces), default=None),
        }
        return _Raw(
            raw=json.dumps(value, default=str),
            structured=structured,
            origin=Source.TOOL,
            source_name="tempo",
            source_type=SourceType.TRACES,
            evidence_type=EvidenceType.TRACE_PATTERN,
            empty=result.found_nothing,
            evidence_ids=result.evidence_ids,
        )

    # ---- non-MCP handlers ---------------------------------------------------- #

    async def _known_issues(
        self,
        args: dict[str, Any],
        state: HorizonState,
        naming: ServiceNaming,
        context: ToolContext | None,
    ) -> _Raw:
        component = str(args["component"])[:120]
        version = str(args["version"])[:60]
        if self._known is None:
            return _gap(
                SEARCH_KNOWN_ISSUES,
                "known-issue search is not configured",
                source_type=SourceType.RUNBOOK,
            )
        result = await self._known.search_known_issues(component, version)
        body = "\n\n".join(f"{i.title}\n{i.url}\n{i.excerpt}" for i in result.issues[:3])
        first = result.issues[0] if result.issues else None
        return _Raw(
            raw=body or f"no known issue found for {component} {version}",
            structured={
                "title": first.title if first else "",
                "component": component,
                "version": version,
                "source": result.source.value,
                "reason": result.reason,
            },
            origin=result.source,
            source_name=f"known_issues.{result.source.value}",
            source_type=SourceType.RUNBOOK,
            evidence_type=EvidenceType.HISTORICAL_INCIDENT,
            unstructured=True,
            untrusted=True,
            empty=not result.issues,
            url=first.url if first else None,
        )

    async def _history(
        self,
        args: dict[str, Any],
        state: HorizonState,
        naming: ServiceNaming,
        context: ToolContext | None,
    ) -> _Raw:
        name = str(args["name"])
        if name not in NAMED_QUERIES:
            raise ValueError(name)
        if self._rawtree is None:
            return _gap(QUERY_HISTORY, "RawTree is not configured", source_type=SourceType.MEMORY)
        qr = await self._rawtree.named_query(
            name, {"incident_id": state.incident_id, "service": naming.bare(state.service)}
        )
        return self._query_raw(QUERY_HISTORY, qr)

    def _remote(
        self, name: str
    ) -> Callable[
        [dict[str, Any], HorizonState, ServiceNaming, ToolContext | None], Awaitable[_Raw]
    ]:
        async def _call(
            args: dict[str, Any],
            state: HorizonState,
            naming: ServiceNaming,
            context: ToolContext | None,
        ) -> _Raw:
            if self._rawtree_tools is None or not self._rawtree_tools.available:
                return _gap(
                    name, "RawTree agent tools are not configured", source_type=SourceType.MEMORY
                )
            allowed = {s.name for s in self.remote_specs()}
            if name not in allowed:
                return _gap(name, "tool is not on the allowlist", source_type=SourceType.MEMORY)
            qr = await self._rawtree_tools.call(name, dict(args))
            return self._query_raw(name, qr)

        return _call

    @staticmethod
    def _query_raw(tool: str, qr: QueryResult) -> _Raw:
        if qr.error:
            raw = _gap(tool, qr.error[:200], source_type=SourceType.MEMORY)
            raw.query = qr
            return raw
        rows = qr.rows[:50]
        return _Raw(
            raw=json.dumps({"sql": qr.sql, "rows": rows}, default=str),
            structured={"kind": "query", "name": qr.name, "rows": rows},
            origin=qr.source,
            source_name=f"rawtree.{qr.name}",
            source_type=SourceType.MEMORY,
            evidence_type=EvidenceType.HISTORICAL_INCIDENT,
            empty=not rows,
            query=qr,
            # Rows another agent or process wrote are data, not instruction.
            untrusted=tool.startswith(RAWTREE_PREFIX),
            unstructured=False,
        )

    async def _memory(
        self,
        args: dict[str, Any],
        state: HorizonState,
        naming: ServiceNaming,
        context: ToolContext | None,
    ) -> _Raw:
        symptom = str(args.get("symptom") or state.symptom)[:300]
        cards = await self.recall_cards(symptom, naming.bare(state.service))
        if not cards:
            return _Raw(
                raw=f"no resolved incident resembles: {symptom}",
                structured={"kind": "query", "name": "memory", "rows": []},
                origin=Source.MEMORY,
                source_name="incident_memory",
                source_type=SourceType.MEMORY,
                evidence_type=EvidenceType.HISTORICAL_INCIDENT,
                empty=True,
            )
        rows = [
            {
                "incident": c.incident_id,
                "cause": c.root_cause[:60],
                "fixed_by": c.successful_action,
                "failed": "/".join(c.failed_actions),
            }
            for c in cards
        ]
        return _Raw(
            raw="\n".join(c.render() for c in cards),
            structured={"kind": "query", "name": "memory", "rows": rows},
            origin=Source.MEMORY,
            source_name="incident_memory",
            source_type=SourceType.MEMORY,
            evidence_type=EvidenceType.HISTORICAL_INCIDENT,
            memory=cards,
        )

    async def recall_cards(self, symptom: str, service: str) -> list[MemoryCard]:
        """Horizon memory cards ranked by term overlap, then the incident store."""
        terms = _terms(f"{symptom} {service}")
        scored: list[tuple[float, MemoryCard]] = []
        for card in await self._store.memory_cards(limit=100):
            overlap = len(terms & _terms(f"{card.symptoms} {card.root_cause} {card.lesson}"))
            if overlap:
                scored.append((overlap / (len(terms) or 1), card))
        scored.sort(key=lambda p: (-p[0], p[1].id))
        cards = [c for _, c in scored][:MAX_MEMORY_CARDS]
        if len(cards) < MAX_MEMORY_CARDS and self._recall is not None:
            try:
                result = await self._recall.similar(symptom, [service], limit=MAX_MEMORY_CARDS)
                seen = {c.incident_id for c in cards}
                for match in result:
                    mem = match.memory
                    if (mem.incident_id or mem.id) in seen:
                        continue
                    cards.append(
                        MemoryCard(
                            id=f"mc_{mem.id}",
                            incident_id=mem.incident_id or mem.id,
                            symptoms=mem.symptoms[:200],
                            root_cause=mem.root_cause[:200],
                            failed_actions=list(mem.failed_attempts)[:4],
                            successful_action=mem.successful_fix[:120] or None,
                            lesson=mem.prevention[:200],
                            image_status="unavailable",
                        )
                    )
                    if len(cards) >= MAX_MEMORY_CARDS:
                        break
            except Exception as exc:  # noqa: BLE001 - recall is enrichment, never a halt
                log.warning("incident memory recall failed", error=type(exc).__name__)
        return cards[:MAX_MEMORY_CARDS]

    # ---- instance resolution --------------------------------------------------- #

    def _remember_instances(self, incident_id: str, service_id: str, ids: list[str]) -> None:
        key = f"{incident_id}|{service_id}"
        self._instances[key] = ids
        self._instances.move_to_end(key)
        while len(self._instances) > self.MAX_INSTANCE_CACHE:
            self._instances.popitem(last=False)

    async def instance_for(
        self,
        state: HorizonState,
        service_id: str,
        naming: ServiceNaming,
        context: ToolContext | None,
    ) -> str | None:
        """The first observed instance of a service; re-read after a restart."""
        cached = self._instances.get(f"{state.incident_id}|{service_id}")
        if cached:
            return cached[0]
        # A resumed process has an empty cache: the stored observation remembers.
        for card in reversed(state.evidence):
            if card.tool != INSTANCE_STATUS:
                continue
            obs = await self._store.get_observation(card.id)
            insts = ((obs.extra.get("structured") or {}).get("instances") or []) if obs else []
            ids = [str(i["instance_id"]) for i in insts if i.get("instance_id")]
            if ids:
                self._remember_instances(state.incident_id, service_id, ids)
                return ids[0]
        try:
            result = await self._invoke("list_instances", {"service_id": service_id}, context)
        except SourceUnavailable:
            return None
        if not result.ok or result.degraded or result.value is None:
            return None
        value = result.value.model_dump(mode="json")
        ids = [
            str(i.get("instance_id")) for i in value.get("instances") or [] if i.get("instance_id")
        ]
        if ids:
            self._remember_instances(state.incident_id, service_id, ids)
        return ids[0] if ids else None

    async def record_card(
        self,
        state: HorizonState,
        *,
        tool: str,
        claim_raw: str,
        structured: dict[str, Any],
        origin: Source,
        source_type: SourceType,
        evidence_type: EvidenceType,
        weight: float,
    ) -> EvidenceCard:
        """Store a code-produced observation (verification) exactly like any other."""
        raw = _Raw(
            raw=claim_raw,
            structured=structured,
            origin=origin,
            source_name=f"horizon.{tool}",
            source_type=source_type,
            evidence_type=evidence_type,
        )
        evidence_id = await self._evidence_id(raw, state)
        card = await self._compactor.compact(
            CompactionInput(
                evidence_id=evidence_id,
                step=state.step,
                tool=tool,
                origin=origin,
                raw=claim_raw,
                structured=structured,
            ),
            weight=weight,
            unstructured=False,
        )
        await self._store.save_observation(
            StoredObservation(
                evidence_id=evidence_id,
                incident_id=state.incident_id,
                tool=tool,
                raw=claim_raw,
                card=card,
                extra={"structured": structured, "status": "ok"},
            )
        )
        return card


def _status(raw: _Raw) -> str:
    return "gap" if raw.gap_reason is not None else "empty" if raw.empty else "ok"


def _gap(tool: str, reason: str, *, source_type: SourceType = SourceType.RUNTIME) -> _Raw:
    return _Raw(
        raw=f"unavailable: {reason}",
        structured=None,
        origin=Source.SYSTEM,
        source_name=tool,
        source_type=source_type,
        evidence_type=EvidenceType.EVIDENCE_GAP,
        gap_reason=reason,
    )


def _gap_from(tool: str, result: ToolResult, source: str, source_type: SourceType) -> _Raw:
    if result.degraded:
        reason = result.degraded_reason or "source degraded"
    elif result.error is not None:
        reason = f"{result.error.code}: {result.error.message}"
    else:
        reason = "source did not answer"
    raw = _gap(tool, reason[:240], source_type=source_type)
    raw.source_name = source
    # A degraded tool has already written its own gap row; cite that one.
    raw.evidence_ids = result.evidence_ids
    return raw


_WORD: Final = re.compile(r"[a-z][a-z0-9_]{2,}")
_STOP: Final = frozenset({"the", "and", "for", "with", "after", "from", "that", "this"})


def _terms(text: str) -> set[str]:
    return {w for w in _WORD.findall(text.lower()) if w not in _STOP}


# --------------------------------------------------------------------------- #
# act: proposal construction                                                   #
# --------------------------------------------------------------------------- #

_INSTANCE_ACTIONS: Final = frozenset({ActionType.RESTART_INSTANCE, ActionType.DRAIN_INSTANCE})

VERIFY_METRIC: Final = "error_rate"
VERIFY_THRESHOLD: Final = 0.02
VERIFY_WINDOW_S: Final = 120


def resource_for(
    action_type: ActionType, *, service_id: str, instance_id: str | None, environment: str
) -> ResourceRef:
    if action_type in _INSTANCE_ACTIONS:
        return ResourceRef(
            resource_type="instance",
            resource_id=instance_id or service_id,
            environment=environment,
            service_id=service_id,
        )
    if action_type is ActionType.CLEAR_CACHE_KEY:
        rtype = "cache"
    elif action_type is ActionType.UPDATE_CONFIG:
        rtype = "config"
    else:
        rtype = "service"
    return ResourceRef(
        resource_type=rtype, resource_id=service_id, environment=environment, service_id=service_id
    )


def rollback_plan_for(action_type: ActionType) -> RollbackPlan:
    if action_type is ActionType.ROLLBACK_DEPLOYMENT:
        return RollbackPlan(
            strategy="compensating_action",
            description="roll forward to the version that was running before",
            inverse_action_type=ActionType.ROLLBACK_DEPLOYMENT,
        )
    if action_type is ActionType.RESTART_INSTANCE:
        return RollbackPlan(
            strategy="inverse_action",
            description="the instance restarts back into service; escalate if it does not",
            automatic=True,
        )
    return RollbackPlan(strategy="compensating_action", description="operator-defined reversal")


def idempotency_key(incident_id: str, signature: str) -> str:
    return f"hz:{incident_id}:{signature}"[:200]


def build_proposal(
    *,
    incident_id: str,
    action_type: ActionType,
    target: ResourceRef,
    arguments: dict[str, str | int | float | bool],
    evidence_ids: list[str],
    statement: str,
    signature: str,
    proposed_at: datetime,
    action_id: str,
) -> ActionProposal:
    """Deterministic construction. The reason text is for humans; no gate reads it."""
    service_id = target.service_id or target.resource_id
    verification = VerificationPlan(
        target_metric=VERIFY_METRIC,
        direction=MetricDirection.DECREASE,
        threshold=VERIFY_THRESHOLD,
        observation_window_s=VERIFY_WINDOW_S,
        protected_metrics=["latency_p99"],
    )
    return ActionProposal(
        id=action_id,
        incident_id=incident_id,
        action_type=action_type,
        target=target,
        reason=statement[:500],
        arguments=arguments,
        supporting_evidence=evidence_ids,
        expected_effect=ExpectedEffect(
            metric=VERIFY_METRIC,
            direction=MetricDirection.DECREASE,
            threshold=VERIFY_THRESHOLD,
            window_seconds=VERIFY_WINDOW_S,
        ),
        blast_radius=BlastRadius(directly_affected=[service_id]),
        rollback=rollback_plan_for(action_type),
        verification=verification,
        idempotency_key=idempotency_key(incident_id, signature),
        proposed_at=proposed_at,
    )


# --------------------------------------------------------------------------- #
# verify: sustained-window health                                              #
# --------------------------------------------------------------------------- #

P99_MS_MAX: Final = 250.0
ERROR_RATE_MAX: Final = 0.02
POOL_UTILISATION_MAX: Final = 0.8


@dataclass(frozen=True, slots=True)
class HealthSample:
    p99_ms: float | None
    error_rate: float | None
    pool_utilisation: float | None
    source: Source = Source.PROMETHEUS
    reason: str = ""

    @property
    def outcome(self) -> ClaimOutcome:
        """UNAVAILABLE whenever any signal is missing - never a pass."""
        if self.p99_ms is None or self.error_rate is None or self.pool_utilisation is None:
            return ClaimOutcome.UNAVAILABLE
        ok = (
            self.p99_ms < P99_MS_MAX
            and self.error_rate < ERROR_RATE_MAX
            and self.pool_utilisation < POOL_UTILISATION_MAX
        )
        return ClaimOutcome.PASS if ok else ClaimOutcome.FAIL

    def describe(self) -> str:
        def f(v: float | None, spec: str) -> str:
            return "n/a" if v is None else format(v, spec)

        return (
            f"p99={f(self.p99_ms, '.0f')}ms err={f(self.error_rate, '.3f')} "
            f"pool={f(self.pool_utilisation, '.2f')}"
        )


class HealthProbe(Protocol):
    async def sample(self, service: str) -> HealthSample: ...


class PrometheusHealthProbe:
    """Reads the three health signals through the existing templated client.

    The before/after ``VerificationEngine`` compares two window means; it has
    no notion of "N consecutive samples all within a threshold", so this probe
    reuses the same client and query templates and ``SustainedVerifier`` owns
    the window. An exception from any read makes that signal ``None``, which
    makes the sample UNAVAILABLE, which fails the window.
    """

    WINDOW_S: Final = 120

    def __init__(self, prometheus: Any) -> None:
        self._prom = prometheus

    async def _latest(self, fn: Callable[..., Awaitable[Any]], service: str) -> float | None:
        try:
            series = await fn(service, window_s=self.WINDOW_S)
        except SourceUnavailable:
            return None
        values = [s.latest for s in series if s.latest is not None]
        return float(values[0]) if values else None

    async def sample(self, service: str) -> HealthSample:
        p99_s, err, pool = await asyncio.gather(
            self._latest(self._prom.latency_p99, service),
            self._latest(self._prom.error_rate, service),
            self._latest(self._prom.pool_saturation, service),
        )
        return HealthSample(
            p99_ms=None if p99_s is None else p99_s * 1000.0,
            error_rate=err,
            pool_utilisation=pool,
            source=Source.PROMETHEUS,
            reason="" if None not in (p99_s, err, pool) else "a health signal was unreadable",
        )


@dataclass(frozen=True, slots=True)
class SustainedResult:
    passed: bool
    samples: tuple[HealthSample, ...]
    required: int
    summary: str

    @property
    def outcomes(self) -> list[str]:
        return [s.outcome.value for s in self.samples]


class SustainedVerifier:
    """Pass only when ``required`` consecutive samples are all within thresholds."""

    def __init__(
        self,
        probe: HealthProbe | None,
        *,
        required: int,
        interval_s: float,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._probe = probe
        self._required = max(1, required)
        self._interval = max(0.0, interval_s)
        self._sleep = sleep

    async def verify(self, service: str) -> SustainedResult:
        if self._probe is None:
            sample = HealthSample(None, None, None, Source.SYSTEM, "no health probe configured")
            return SustainedResult(
                False,
                (sample,),
                self._required,
                "FAILED: no health probe is configured; recovery cannot be proven",
            )
        samples: list[HealthSample] = []
        for i in range(self._required):
            if i:
                await self._sleep(self._interval)
            sample = await self._probe.sample(service)
            samples.append(sample)
            outcome = sample.outcome
            if outcome is not ClaimOutcome.PASS:
                # Fail fast: one bad or unreadable sample already breaks the
                # window, and waiting out the rest only delays reassessment.
                word = "unreadable" if outcome is ClaimOutcome.UNAVAILABLE else "outside thresholds"
                return SustainedResult(
                    False,
                    tuple(samples),
                    self._required,
                    f"FAILED: sample {i + 1}/{self._required} {word}: {sample.describe()}",
                )
        return SustainedResult(
            True,
            tuple(samples),
            self._required,
            f"PASSED: {self._required}/{self._required} consecutive samples within "
            f"thresholds; last {samples[-1].describe()}",
        )


__all__ = [
    "CONTAINER_LOGS",
    "DEPLOYMENT_HISTORY",
    "INSTANCE_STATUS",
    "OBSERVE_TOOLS",
    "PROPOSE_REMEDIATION",
    "QUERY_HISTORY",
    "QUERY_METRICS",
    "RECALL_MEMORY",
    "SEARCH_KNOWN_ISSUES",
    "SEARCH_TRACES",
    "SPECS",
    "EvidenceRecorder",
    "HealthProbe",
    "HealthSample",
    "Observation",
    "Observer",
    "PrometheusHealthProbe",
    "ServiceNaming",
    "SustainedResult",
    "SustainedVerifier",
    "build_proposal",
    "idempotency_key",
    "resource_for",
]
