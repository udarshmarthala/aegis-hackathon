"""Tempo trace evidence source.

A soft dependency, exactly like Prometheus. Every failure becomes
``SourceUnavailable``, which the investigation records as an evidence gap and
continues with lower confidence, rather than aborting.

Queries are built from templates with caller values bound through
``escape_traceql``. Callers never pass raw TraceQL: a service name that arrives
in an alert payload is attacker-influenceable, and a query language is a query
language.

``service_call_edges`` is the one function other packages build on - the graph
package turns its output into CALLS edges - so it returns a frozen dataclass
rather than a dict. A dict would let a key rename break topology ingestion
silently at runtime.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any

import httpx

from aegis.core.config import Settings
from aegis.core.errors import SourceUnavailable, ValidationError
from aegis.core.logging import get_logger
from aegis.core.resilience import Bulkhead, guarded_call

log = get_logger(__name__)

# Tempo answers a search from its index; asking for more than this per call
# trades latency for traces an investigation will never read.
MAX_SEARCH_LIMIT = 200

# Edge derivation fetches whole traces. Each fetch is a round trip, so the
# number of traces walked is capped independently of the number of edges asked
# for - otherwise a wide time window turns into an unbounded fan-out.
MAX_TRACES_PER_EDGE_SCAN = 50

_TRACE_ID_RE = re.compile(r"^[0-9a-fA-F]{8,32}$")
_TAG_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]{0,63}$")

# OTLP renders the enum either as its name or its number depending on the
# exporter, and Tempo passes both through untouched.
_ERROR_STATUS: frozenset[Any] = frozenset({"STATUS_CODE_ERROR", "ERROR", 2, "2"})


def escape_traceql(value: str) -> str:
    """Escape a value for safe inclusion in a TraceQL string literal.

    Same discipline as ``prometheus.escape_label``: strip the characters that
    could terminate the literal or the selector rather than trying to encode
    them, because nothing legitimate in a service name needs them.
    """
    for ch in ("\\", '"', "\n", "\r", "{", "}", "|"):
        value = value.replace(ch, "")
    return value


@dataclass(frozen=True, slots=True)
class TraceSummary:
    """One row of a search result - enough to decide whether to fetch it."""

    trace_id: str
    root_service: str
    root_name: str
    start_time_s: float
    duration_ms: float


@dataclass(frozen=True, slots=True)
class Span:
    """A single normalised span.

    ``service`` is resolved from the batch resource attributes, so callers never
    have to know how OTLP nests resource-level attributes.
    """

    trace_id: str
    span_id: str
    parent_span_id: str | None
    service: str
    name: str
    start_time_s: float
    duration_ms: float
    is_error: bool
    status_message: str
    attributes: dict[str, str]


@dataclass(frozen=True, slots=True)
class TraceDetail:
    """A whole trace, flattened. ``spans`` keeps Tempo's ordering."""

    trace_id: str
    spans: tuple[Span, ...]
    query: str

    @property
    def services(self) -> tuple[str, ...]:
        seen: dict[str, None] = {}
        for span in self.spans:
            seen.setdefault(span.service, None)
        return tuple(seen)

    @property
    def duration_ms(self) -> float:
        return max((s.duration_ms for s in self.spans), default=0.0)

    @property
    def error_count(self) -> int:
        return sum(1 for s in self.spans if s.is_error)


@dataclass(frozen=True, slots=True)
class CallEdge:
    """A derived caller -> callee relationship.

    This is the contract the graph package ingests as a CALLS edge. The shape is
    deliberately small and frozen: topology is rebuilt from it on every refresh,
    so an unstable field set would mean silently dropping edges.

    ``p99_ms`` is the nearest-rank percentile over the observed callee spans, not
    an interpolation, so the number is always a value that really occurred.
    """

    caller_service: str
    callee_service: str
    count: int
    p99_ms: float


def _percentile(values: list[float], q: float) -> float:
    """Nearest-rank percentile. Deterministic and never invents a value."""
    if not values:
        return 0.0
    ordered = sorted(values)
    rank = max(1, math.ceil(q * len(ordered)))
    return ordered[min(rank, len(ordered)) - 1]


def _attr_value(raw: Any) -> str:
    """Flatten an OTLP AnyValue to a string.

    Attribute values are only ever used for display and matching, so one string
    representation is enough and keeps OTLP's union type out of every caller.
    """
    if not isinstance(raw, dict):
        return str(raw)
    for key in ("stringValue", "boolValue", "intValue", "doubleValue"):
        if key in raw:
            return str(raw[key])
    array = raw.get("arrayValue")
    if isinstance(array, dict):
        return ",".join(_attr_value(v) for v in array.get("values", []) or [])
    return ""


def _attributes(raw: Any) -> dict[str, str]:
    out: dict[str, str] = {}
    if not isinstance(raw, list):
        return out
    for item in raw:
        if isinstance(item, dict) and "key" in item:
            out[str(item["key"])] = _attr_value(item.get("value"))
    return out


def _nano_to_s(raw: Any) -> float:
    try:
        return float(raw) / 1e9
    except (TypeError, ValueError):
        return 0.0


def _bounded_limit(limit: int) -> int:
    if limit < 1:
        raise ValidationError("limit must be >= 1", context={"limit": limit})
    return min(limit, MAX_SEARCH_LIMIT)


def _flatten(payload: dict[str, Any], trace_id: str) -> list[Span]:
    """Flatten OTLP batches into spans, tolerating partial documents.

    Tempo streams what it has. A malformed batch is skipped rather than failing
    the whole trace, because a partial trace is still usable evidence while an
    exception here would be indistinguishable from Tempo being down.
    """
    spans: list[Span] = []
    batches = payload.get("batches") or payload.get("resourceSpans") or []
    if not isinstance(batches, list):
        return spans

    for batch in batches:
        if not isinstance(batch, dict):
            continue
        resource = batch.get("resource")
        res_attrs = _attributes(resource.get("attributes")) if isinstance(resource, dict) else {}
        service = res_attrs.get("service.name", "")

        scope_spans = batch.get("scopeSpans") or batch.get("instrumentationLibrarySpans") or []
        if not isinstance(scope_spans, list):
            continue
        for scope in scope_spans:
            if not isinstance(scope, dict):
                continue
            for raw in scope.get("spans", []) or []:
                if not isinstance(raw, dict):
                    continue
                status = raw.get("status")
                status = status if isinstance(status, dict) else {}
                start_s = _nano_to_s(raw.get("startTimeUnixNano", 0))
                end_s = _nano_to_s(raw.get("endTimeUnixNano", 0))
                parent = str(raw.get("parentSpanId", "") or "") or None
                spans.append(
                    Span(
                        trace_id=trace_id,
                        span_id=str(raw.get("spanId", "")),
                        parent_span_id=parent,
                        service=service,
                        name=str(raw.get("name", "")),
                        start_time_s=start_s,
                        duration_ms=max(0.0, (end_s - start_s) * 1000.0),
                        is_error=status.get("code") in _ERROR_STATUS,
                        status_message=str(status.get("message", "")),
                        attributes=_attributes(raw.get("attributes")),
                    )
                )
    return spans


class TempoClient:
    """Read-only client. Tempo exposes no write surface Aegis may use."""

    __slots__ = ("_settings", "_bulkhead", "_client")

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._bulkhead = Bulkhead("tempo", limit=8)
        self._client: httpx.AsyncClient | None = None

    async def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self._settings.tempo_url,
                timeout=self._settings.source_timeout_s,
                limits=httpx.Limits(max_connections=16, max_keepalive_connections=8),
            )
        return self._client

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _get(self, path: str, params: dict[str, Any], *, what: str) -> dict[str, Any]:
        """One guarded GET. Every Tempo failure collapses to SourceUnavailable.

        The distinction that matters is the same one Prometheus makes: an empty
        result means 'no such trace', an exception means 'we could not look'.
        """

        async def _call() -> dict[str, Any]:
            client = await self._http()
            resp = await client.get(path, params=params)
            resp.raise_for_status()
            payload = resp.json()
            return payload if isinstance(payload, dict) else {}

        try:
            return await guarded_call(
                _call,
                dependency="tempo",
                timeout_s=self._settings.source_timeout_s,
                attempts=2,
                bulkhead=self._bulkhead,
            )
        except Exception as exc:
            raise SourceUnavailable(
                f"tempo unavailable: {type(exc).__name__}",
                context={"dependency": "tempo", "operation": what, "path": path},
            ) from exc

    # ---- templated searches -------------------------------------------------

    def _traceql(self, service: str, tags: dict[str, str] | None) -> str:
        svc = escape_traceql(service)
        if not svc:
            raise ValidationError(
                "service name is empty after sanitisation", context={"service": service[:64]}
            )
        parts = [f'resource.service.name = "{svc}"']
        for key, value in (tags or {}).items():
            # A tag key lands in the query unquoted, so it is validated rather
            # than escaped - there is no safe way to quote an attribute path.
            if not _TAG_KEY_RE.match(key):
                raise ValidationError("unsafe trace tag key", context={"key": key[:64]})
            parts.append(f'span.{key} = "{escape_traceql(value)}"')
        return "{ " + " && ".join(parts) + " }"

    async def search_traces(
        self,
        service: str,
        *,
        start: float,
        end: float,
        min_duration_ms: int | None = None,
        tags: dict[str, str] | None = None,
        limit: int = 20,
    ) -> list[TraceSummary]:
        """Search traces touching ``service`` in a window.

        An empty list is a valid answer meaning Tempo held no matching trace.
        Only an exception means the source could not be consulted.
        """
        params: dict[str, Any] = {
            "q": self._traceql(service, tags),
            "start": int(start),
            "end": int(end),
            "limit": _bounded_limit(limit),
        }
        if min_duration_ms is not None:
            if min_duration_ms < 0:
                raise ValidationError(
                    "min_duration_ms must be >= 0", context={"min_duration_ms": min_duration_ms}
                )
            params["minDuration"] = f"{int(min_duration_ms)}ms"

        payload = await self._get("/api/search", params, what="search_traces")
        out: list[TraceSummary] = []
        for row in payload.get("traces", []) or []:
            if not isinstance(row, dict):
                continue
            trace_id = str(row.get("traceID", ""))
            if not trace_id:
                continue
            out.append(
                TraceSummary(
                    trace_id=trace_id,
                    root_service=str(row.get("rootServiceName", "")),
                    root_name=str(row.get("rootTraceName", "")),
                    start_time_s=_nano_to_s(row.get("startTimeUnixNano", 0)),
                    duration_ms=float(row.get("durationMs", 0) or 0),
                )
            )
        return out

    async def get_trace(self, trace_id: str) -> TraceDetail:
        """Fetch one trace and flatten it into normalised spans."""
        if not _TRACE_ID_RE.match(trace_id):
            raise ValidationError("trace_id must be hex", context={"trace_id": trace_id[:64]})
        payload = await self._get(f"/api/traces/{trace_id}", {}, what="get_trace")
        return TraceDetail(
            trace_id=trace_id,
            spans=tuple(_flatten(payload, trace_id)),
            query=f"/api/traces/{trace_id}",
        )

    async def error_spans(
        self, service: str, start: float, end: float, limit: int = 20
    ) -> list[Span]:
        """Spans of ``service`` carrying an error status inside the window.

        Error status is a machine-recorded field, which is why this is Tier-A
        evidence while a log line containing the word "error" is not.
        """
        bounded = _bounded_limit(limit)
        summaries = await self.search_traces(
            service, start=start, end=end, tags={"status": "error"}, limit=bounded
        )
        svc = escape_traceql(service)
        out: list[Span] = []
        for summary in summaries[:bounded]:
            detail = await self.get_trace(summary.trace_id)
            out.extend(s for s in detail.spans if s.is_error and s.service == svc)
            if len(out) >= bounded:
                break
        return out[:bounded]

    async def service_call_edges(
        self, start: float, end: float, limit: int = 50
    ) -> list[CallEdge]:
        """Derive caller -> callee edges from parent/child spans.

        Topology is observed, never declared: an edge exists because a request
        really crossed it, which is what makes a blast-radius claim defensible.
        The scan is bounded by ``MAX_TRACES_PER_EDGE_SCAN`` so a wide window
        cannot turn into an unbounded fan-out of trace fetches.
        """
        params: dict[str, Any] = {
            "start": int(start),
            "end": int(end),
            "limit": min(_bounded_limit(limit), MAX_TRACES_PER_EDGE_SCAN),
        }
        payload = await self._get("/api/search", params, what="service_call_edges")

        durations: dict[tuple[str, str], list[float]] = {}
        scanned = 0
        for row in payload.get("traces", []) or []:
            if scanned >= MAX_TRACES_PER_EDGE_SCAN:
                break
            if not isinstance(row, dict) or not row.get("traceID"):
                continue
            scanned += 1
            detail = await self.get_trace(str(row["traceID"]))
            by_id = {s.span_id: s for s in detail.spans}
            for span in detail.spans:
                parent = by_id.get(span.parent_span_id or "")
                # A same-service parent is an internal call, not a topology edge.
                if parent is None or not parent.service or parent.service == span.service:
                    continue
                durations.setdefault((parent.service, span.service), []).append(span.duration_ms)

        edges = [
            CallEdge(
                caller_service=caller,
                callee_service=callee,
                count=len(samples),
                p99_ms=round(_percentile(samples, 0.99), 3),
            )
            for (caller, callee), samples in durations.items()
        ]
        # Stable ordering so a topology diff reflects real change, not map order.
        edges.sort(key=lambda e: (e.caller_service, e.callee_service))
        return edges[:limit]


__all__ = [
    "MAX_SEARCH_LIMIT",
    "MAX_TRACES_PER_EDGE_SCAN",
    "CallEdge",
    "Span",
    "TempoClient",
    "TraceDetail",
    "TraceSummary",
    "escape_traceql",
]
