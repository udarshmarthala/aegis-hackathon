"""Loki log evidence source.

A soft dependency, exactly like Prometheus. Every failure becomes
``SourceUnavailable`` so an outage is recorded as an evidence gap rather than
mistaken for "no matching logs".

Two properties drive the design of this module:

**LogQL is built here, never by a caller.** Callers describe what they want with
a ``LogSelector`` - a service, an optional level, an optional substring - and
this module renders the query. A service name lifted out of an alert payload is
attacker-influenceable, and ``{service="x"} |= ""} |= "secret"`` is a query
injection in exactly the way SQL is.

**Log bodies are Tier D.** A log line is free text an attacker can often write
into. Every line leaves this module wrapped in ``UntrustedText`` so a developer
cannot accidentally concatenate one into a prompt, and so callers store them
with ``untrusted=True``.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from typing import Any

import httpx

from aegis.core.config import Settings
from aegis.core.errors import SourceUnavailable, ValidationError
from aegis.core.logging import get_logger
from aegis.core.resilience import Bulkhead, guarded_call
from aegis.domain.models import UntrustedText

log = get_logger(__name__)

# Loki paginates; beyond this a caller wants aggregation, not more lines.
MAX_LIMIT = 1000

# Pattern counting reads a bounded sample rather than the whole window. A
# pattern that does not appear in a thousand lines is not the pattern driving
# the incident, and an unbounded read would grow with the outage.
PATTERN_SCAN_LIMIT = 1000
MAX_PATTERNS = 50

# Label values land inside a matcher. They are validated rather than escaped:
# nothing legitimate in a service or level name needs a quote or a brace, so a
# value that contains one is an injection attempt and is refused outright.
_LABEL_VALUE_RE = re.compile(r"^[A-Za-z0-9_.:/@-]{1,255}$")
_LABEL_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")

# Anything matching an error shape, independent of whether the pipeline
# populated a ``level`` label - Docker and OTLP pipelines frequently do not.
ERROR_LINE_PATTERN = r"(?i)(error|exception|traceback|fatal|panic|critical)"

_ORIGIN = "log"

# Normalisation steps, applied in this order. UUIDs and hex must be collapsed
# before bare numbers or a digit inside an identifier would be replaced first
# and the identifier would never match its own shape.
_NORMALISERS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(
            r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?(?:Z|[+-]\d{2}:?\d{2})?"
        ),
        "<ts>",
    ),
    (
        re.compile(
            r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
            r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
        ),
        "<uuid>",
    ),
    (re.compile(r"\b[a-z]{2,5}_[0-9A-HJKMNP-TV-Z]{26}\b"), "<id>"),
    (re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}(?::\d{1,5})?\b"), "<ip>"),
    (re.compile(r"\b0x[0-9a-fA-F]+\b"), "<hex>"),
    (re.compile(r"\b[0-9a-fA-F]{8,}\b"), "<hex>"),
    (re.compile(r"\b\d+(?:\.\d+)?(?:ms|s|m|h|kb|mb|gb|%)?\b", re.IGNORECASE), "<num>"),
    (re.compile(r"\s+"), " "),
)


def escape_label(value: str) -> str:
    """Strip characters that could terminate a LogQL literal or selector.

    Mirrors ``prometheus.escape_label``. Stripping rather than encoding is
    deliberate: an encoded quote would still be a quote to some future parser,
    and no real log substring loses meaning without one.
    """
    for ch in ("\\", '"', "\n", "\r", "{", "}", "|", "`"):
        value = value.replace(ch, "")
    return value


def _label_value(name: str, value: str) -> str:
    """Validate a label value, refusing anything that is not label-shaped."""
    cleaned = escape_label(value)
    if cleaned != value or not _LABEL_VALUE_RE.match(cleaned):
        raise ValidationError(
            f"unsafe LogQL label value for {name!r}",
            context={"label": name, "value": value[:64]},
        )
    return cleaned


def as_untrusted(
    text: str, *, origin: str = _ORIGIN, evidence_id: str | None = None
) -> UntrustedText:
    """Wrap a log body as Tier-D text.

    The helper exists so callers never construct the envelope by hand and never
    have a plain ``str`` of log content in scope to interpolate by accident.
    """
    return UntrustedText(text=text, origin=origin, evidence_id=evidence_id)


@dataclass(frozen=True, slots=True)
class LogSelector:
    """A structured log filter. The only input shape ``query_range`` accepts.

    Keeping this a dataclass rather than a string is the whole defence: there is
    no field a caller can use to smuggle a second selector or a line filter in.
    """

    service: str
    level: str | None = None
    contains: str | None = None
    extra_labels: dict[str, str] | None = None


@dataclass(frozen=True, slots=True)
class LogLine:
    """One log entry. ``line`` is Tier D by construction."""

    timestamp_s: float
    line: UntrustedText
    labels: dict[str, str]

    @property
    def service(self) -> str:
        return self.labels.get("service", "")


@dataclass(frozen=True, slots=True)
class LogPattern:
    """A group of lines that normalise to the same shape.

    ``sample`` is one real line from the group, kept untrusted. ``pattern`` is
    machine-derived and safe to render, which is what makes it usable in a
    summary without exposing attacker-controlled text.
    """

    pattern: str
    count: int
    sample: UntrustedText
    first_seen_s: float
    last_seen_s: float


def normalise_line(line: str) -> str:
    """Collapse variable tokens so similar lines group together.

    Deterministic and purely lexical - no model, no learned clustering - because
    the grouping ends up in evidence and has to be reproducible months later
    during an audit. "user 123 not found" and "user 456 not found" both become
    "user <num> not found".
    """
    out = line.strip()
    for pattern, replacement in _NORMALISERS:
        out = pattern.sub(replacement, out)
    return out.strip()


def build_logql(selector: LogSelector) -> str:
    """Render a ``LogSelector`` into LogQL.

    Every value is validated or escaped here, and the structure - one stream
    selector plus at most one line filter - is fixed by this function rather
    than by anything a caller passes.
    """
    matchers = [f'service="{_label_value("service", selector.service)}"']
    if selector.level is not None:
        matchers.append(f'level="{_label_value("level", selector.level)}"')
    for name, value in sorted((selector.extra_labels or {}).items()):
        if not _LABEL_NAME_RE.match(name):
            raise ValidationError("unsafe LogQL label name", context={"label": name[:64]})
        matchers.append(f'{name}="{_label_value(name, value)}"')

    query = "{" + ", ".join(matchers) + "}"
    if selector.contains:
        # Substrings are user- and model-derived free text, so unlike labels
        # they are escaped rather than refused: an operator legitimately
        # searches for odd strings, they just cannot end the literal.
        needle = escape_label(selector.contains)
        if not needle:
            raise ValidationError(
                "substring filter is empty after sanitisation",
                context={"contains": selector.contains[:64]},
            )
        query += f' |= "{needle}"'
    return query


class LokiClient:
    """Read-only client. Loki exposes no write surface Aegis may use."""

    __slots__ = ("_settings", "_bulkhead", "_client")

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._bulkhead = Bulkhead("loki", limit=8)
        self._client: httpx.AsyncClient | None = None

    async def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self._settings.loki_url,
                timeout=self._settings.source_timeout_s,
                limits=httpx.Limits(max_connections=16, max_keepalive_connections=8),
            )
        return self._client

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _query(self, logql: str, *, start: float, end: float, limit: int) -> list[LogLine]:
        """One guarded range query.

        Raises ``SourceUnavailable`` rather than returning empty. An empty list
        means Loki holds no matching line; an exception means we could not ask.
        Collapsing the two would let an outage read as a healthy service.
        """
        params: dict[str, Any] = {
            "query": logql,
            # Loki takes nanosecond epochs.
            "start": int(start * 1e9),
            "end": int(end * 1e9),
            "limit": _bounded_limit(limit),
            "direction": "backward",
        }

        async def _call() -> dict[str, Any]:
            client = await self._http()
            resp = await client.get("/loki/api/v1/query_range", params=params)
            resp.raise_for_status()
            payload = resp.json()
            return payload if isinstance(payload, dict) else {}

        try:
            payload = await guarded_call(
                _call,
                dependency="loki",
                timeout_s=self._settings.source_timeout_s,
                attempts=2,
                bulkhead=self._bulkhead,
            )
        except Exception as exc:
            raise SourceUnavailable(
                f"loki unavailable: {type(exc).__name__}",
                context={"dependency": "loki", "query": logql},
            ) from exc

        if payload.get("status") not in (None, "success"):
            raise SourceUnavailable(
                "loki returned an error",
                context={"query": logql, "error": str(payload.get("error", ""))[:200]},
            )

        out: list[LogLine] = []
        for stream in payload.get("data", {}).get("result", []) or []:
            if not isinstance(stream, dict):
                continue
            labels = {str(k): str(v) for k, v in (stream.get("stream") or {}).items()}
            for entry in stream.get("values", []) or []:
                if not isinstance(entry, (list, tuple)) or len(entry) < 2:
                    continue
                try:
                    ts = float(entry[0]) / 1e9
                except (TypeError, ValueError):
                    continue
                out.append(
                    LogLine(timestamp_s=ts, line=as_untrusted(str(entry[1])), labels=labels)
                )
        out.sort(key=lambda entry: entry.timestamp_s)
        return out

    async def query_range(
        self, selector_parts: LogSelector, *, start: float, end: float, limit: int = 200
    ) -> list[LogLine]:
        """Range query over a structured selector.

        ``selector_parts`` is the only way to shape a query. There is no
        raw-LogQL entry point on this client, by design.
        """
        return await self._query(
            build_logql(selector_parts), start=start, end=end, limit=limit
        )

    # ---- templated operational queries -------------------------------------

    async def error_logs(
        self, service: str, start: float, end: float, limit: int = 200
    ) -> list[LogLine]:
        """Error-shaped lines for a service.

        The regex is a module constant, never caller input, so this stays a
        template rather than a raw-query back door.
        """
        base = build_logql(LogSelector(service=service))
        logql = f'{base} |~ "{ERROR_LINE_PATTERN}"'
        return await self._query(logql, start=start, end=end, limit=limit)

    async def pattern_counts(
        self, service: str, start: float, end: float, limit: int = MAX_PATTERNS
    ) -> list[LogPattern]:
        """Group a bounded sample of lines into normalised patterns.

        An empty list means the service logged nothing in the window - a real
        answer, and a different one from ``SourceUnavailable``.
        """
        lines = await self._query(
            build_logql(LogSelector(service=service)),
            start=start,
            end=end,
            limit=PATTERN_SCAN_LIMIT,
        )

        counts: Counter[str] = Counter()
        samples: dict[str, LogLine] = {}
        first: dict[str, float] = {}
        last: dict[str, float] = {}
        for entry in lines:
            key = normalise_line(entry.line.text)
            if not key:
                continue
            counts[key] += 1
            samples.setdefault(key, entry)
            first[key] = min(first.get(key, entry.timestamp_s), entry.timestamp_s)
            last[key] = max(last.get(key, entry.timestamp_s), entry.timestamp_s)

        # Ties break on the pattern text so repeated runs produce identical
        # evidence for identical input.
        ordered = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
        return [
            LogPattern(
                pattern=pattern,
                count=count,
                sample=samples[pattern].line,
                first_seen_s=first[pattern],
                last_seen_s=last[pattern],
            )
            for pattern, count in ordered[: max(1, min(limit, MAX_PATTERNS))]
        ]


def _bounded_limit(limit: int) -> int:
    if limit < 1:
        raise ValidationError("limit must be >= 1", context={"limit": limit})
    return min(limit, MAX_LIMIT)


__all__ = [
    "ERROR_LINE_PATTERN",
    "MAX_LIMIT",
    "MAX_PATTERNS",
    "PATTERN_SCAN_LIMIT",
    "LogLine",
    "LogPattern",
    "LogSelector",
    "LokiClient",
    "as_untrusted",
    "build_logql",
    "escape_label",
    "normalise_line",
]
