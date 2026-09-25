"""RawTree: the agent's long-term memory and the heartbeat's data plane.

Two keys, two HTTP clients, and neither can stand in for the other. The write
key only ever reaches ``POST /v1/tables/{table}``; the read key only ever
reaches ``POST /v1/query``. The read client is constructed from the read key
alone, so there is no code path by which a query is sent with a credential
that could write.

What was verified against the live API (not just the docs):

* insert: ``POST {api}/v1/tables/{table}`` with a JSON **array** body and
  ``Authorization: Bearer``; the response is ``{"inserted": n}``. Tables are
  created on first insert.
* query: ``POST {api}/v1/query`` with ``{"sql": ...}``; the response is
  ClickHouse ``JSON`` format - ``meta``, ``data`` (a list of objects), ``rows``,
  ``statistics``. The dialect is ClickHouse (``now64``, ``countIf``,
  ``parseDateTime64BestEffortOrNull`` all work). Anything that is not
  ``SELECT``/``WITH``/``EXPLAIN``/``DESCRIBE`` is refused with 400.
* key scopes are enforced server-side: a write-only key gets 403 on query and a
  read-only key gets 403 on insert.
* database selection is the ``?database=`` query parameter (the
  ``x-rawtree-database`` header is documented as an alternative); without it
  the key's default database applies, which is ``default``.
* rate limits arrive as ``x-ratelimit-*`` headers: 1000 inserts and 10000
  queries per one-second window at the time of writing.
* the default database is shared across the cluster and already held tables
  called ``agent_events``, ``observations`` and ``metrics`` belonging to other
  producers, so Aegis tables are prefixed ``aegis_`` (see ``TABLE_PREFIX``).
* auto-created columns are typed ``Dynamic``. Every named query therefore
  casts explicitly (``toString``, ``toFloat64OrNull``,
  ``parseDateTime64BestEffortOrNull``) instead of trusting inferred types.

Named queries live in this module and nowhere else. Callers pass a name and
typed parameters; every parameter is validated against a strict pattern or
numeric range and only then rendered as a quoted literal. No SQL is accepted
from a caller, ever.

When the read key is missing or RawTree fails, the timeline and token queries
fall back to the Postgres horizon event store and ``action_success_rate`` falls
back to the remediation tables, each result labelled ``source=postgres``.
``anomaly_detect`` has no Postgres equivalent: the heartbeat falls back to its
in-process z-score, and this client reports the failure in ``error``.
"""

from __future__ import annotations

import asyncio
import contextlib
import math
import re
import time
from collections import deque
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any, Final

import httpx

from aegis.agents.horizon.ports import HorizonStore, QueryResult
from aegis.core.clock import SYSTEM_CLOCK, Clock
from aegis.core.config import Settings
from aegis.core.errors import AegisError, ExternalServiceError, ValidationError
from aegis.core.logging import get_logger
from aegis.core.resilience import Bulkhead, guarded_call, with_timeout
from aegis.domain.horizon import HorizonEvent, HorizonEventType, MemoryCard, Source
from aegis.persistence.db import Database

log = get_logger(__name__)

# The key's default database is shared cluster-wide: verified live, it already
# held unrelated tables named ``agent_events``, ``observations`` and
# ``metrics`` with foreign schemas. Unprefixed names would interleave our rows
# with someone else's and feed their numbers to the heartbeat, so every Aegis
# table is namespaced. The logical names (metrics, agent_events ...) are kept.
TABLE_PREFIX: Final = "aegis_"
TABLE_METRICS: Final = f"{TABLE_PREFIX}metrics"
TABLE_EVENTS: Final = f"{TABLE_PREFIX}agent_events"
TABLE_OBSERVATIONS: Final = f"{TABLE_PREFIX}observations"
TABLE_MEMORY: Final = f"{TABLE_PREFIX}memory_cards"
TABLES: Final = (TABLE_METRICS, TABLE_EVENTS, TABLE_OBSERVATIONS, TABLE_MEMORY)

# Rows per insert request. RawTree accepts larger bodies, but a bounded request
# keeps a slow flush from holding the writer for the whole timeout.
MAX_ROWS_PER_INSERT: Final = 1000
MAX_ROWS_PER_METRIC_BATCH: Final = 1000
MAX_RAW_CHARS: Final = 16_000
MAX_RESULT_ROWS: Final = 1000
MAX_RECENT_QUERIES: Final = 50
_ERROR_BODY_CHARS: Final = 300

# Identifiers that may be rendered into SQL. Deliberately narrower than
# anything the ids module generates can need: no quotes, no whitespace, no
# backslashes, no comment markers.
_IDENT_RE: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_SYMPTOM_RE: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/ -]{0,119}$")


# --------------------------------------------------------------------------- #
# literal rendering                                                            #
# --------------------------------------------------------------------------- #


def _quote(value: str) -> str:
    """ClickHouse string literal.

    Only ever called on values that already matched a strict pattern, so the
    escaping below is a second line, not the defence.
    """
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


def _ident(
    params: dict[str, Any], key: str, *, required: bool, pattern: re.Pattern[str] = _IDENT_RE
) -> str | None:
    value = params.get(key)
    if value is None or value == "":
        if required:
            raise ValidationError(f"parameter {key} is required", context={"param": key})
        return None
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise ValidationError(
            f"parameter {key} is not a valid identifier",
            context={"param": key, "value": str(value)[:64]},
        )
    return value


def _int(params: dict[str, Any], key: str, *, default: int, lo: int, hi: int) -> int:
    value = params.get(key, default)
    # bool is an int subclass; True is not a window length.
    if isinstance(value, bool) or not isinstance(value, int) or not lo <= value <= hi:
        raise ValidationError(
            f"parameter {key} must be an integer in [{lo}, {hi}]",
            context={"param": key, "value": str(value)[:64]},
        )
    return value


def _float(params: dict[str, Any], key: str, *, default: float, lo: float, hi: float) -> float:
    value = params.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValidationError(f"parameter {key} must be a number", context={"param": key})
    number = float(value)
    if not math.isfinite(number) or not lo <= number <= hi:
        raise ValidationError(
            f"parameter {key} must be in [{lo}, {hi}]",
            context={"param": key, "value": str(value)[:64]},
        )
    return number


# Every column is ``Dynamic`` when RawTree auto-creates a table, so reads cast.
_TS: Final = "parseDateTime64BestEffortOrNull(toString(ts), 3)"
_VAL: Final = "toFloat64OrNull(toString(value))"


def _sql_anomaly_detect(params: dict[str, Any], default_threshold: float) -> str:
    threshold = _float(params, "threshold", default=default_threshold, lo=0.5, hi=100.0)
    window_s = _int(params, "window_s", default=60, lo=10, hi=600)
    baseline_s = _int(params, "baseline_s", default=600, lo=60, hi=7200)
    min_samples = _int(params, "min_samples", default=5, lo=2, hi=1000)
    service = _ident(params, "service", required=False)
    span = window_s + baseline_s
    service_clause = f" AND toString(service) = {_quote(service)}" if service else ""
    recent = f"t >= now64(3) - INTERVAL {window_s} SECOND"
    base = f"t < now64(3) - INTERVAL {window_s} SECOND"
    # The std floor (5 % of the mean, at least 0.001) stops a perfectly flat
    # baseline turning any wobble into an infinite z-score. The heartbeat adds
    # absolute floors on top.
    return (
        f"WITH {_TS} AS t, {_VAL} AS v "  # noqa: S608 - every literal passed _ident/_int/_float
        "SELECT toString(service) AS service, toString(metric) AS metric, "
        f"avgIf(v, {recent}) AS recent_value, "
        f"avgIf(v, {base}) AS baseline_mean, "
        f"stddevPopIf(v, {base}) AS baseline_std, "
        f"countIf({base}) AS baseline_n, "
        f"countIf({recent}) AS recent_n, "
        "(recent_value - baseline_mean) / "
        "greatest(baseline_std, 0.05 * abs(baseline_mean), 0.001) AS z "
        f"FROM {TABLE_METRICS} "
        f"WHERE t >= now64(3) - INTERVAL {span} SECOND AND v IS NOT NULL{service_clause} "
        "GROUP BY service, metric "
        f"HAVING baseline_n >= {min_samples} AND recent_n >= 1 AND z > {threshold!r} "
        "ORDER BY z DESC LIMIT 200"
    )


def _sql_action_success_rate(params: dict[str, Any]) -> str:
    days = _int(params, "lookback_days", default=30, lo=1, hi=365)
    action_type = _ident(params, "action_type", required=False)
    symptom = _ident(params, "symptom", required=False, pattern=_SYMPTOM_RE)
    clauses = [
        f"event_type = {_quote(HorizonEventType.VERIFICATION_RESULT.value)}",
        "action_type != ''",
        f"{_TS} >= now64(3) - INTERVAL {days} DAY",
    ]
    if action_type:
        clauses.append(f"action_type = {_quote(action_type)}")
    if symptom:
        clauses.append(f"symptom = {_quote(symptom)}")
    return (
        "SELECT action_type, symptom, count() AS attempts, "  # noqa: S608 - every literal passed _ident/_int/_float
        "countIf(verified = 1) AS verified_successes, "
        "avgIf(recovery_s, verified = 1 AND recovery_s > 0) AS mean_recovery_s "
        "FROM (SELECT toString(action_type) AS action_type, toString(symptom) AS symptom, "
        "toInt64OrZero(toString(verified)) AS verified, "
        "toFloat64OrZero(toString(recovery_s)) AS recovery_s, "
        f"toString(event_type) AS event_type, ts FROM {TABLE_EVENTS}) "
        f"WHERE {' AND '.join(clauses)} "
        "GROUP BY action_type, symptom ORDER BY attempts DESC LIMIT 200"
    )


def _sql_context_tokens(params: dict[str, Any]) -> str:
    run_id = _ident(params, "run_id", required=False)
    incident_id = _ident(params, "incident_id", required=False)
    if not run_id and not incident_id:
        raise ValidationError("context_tokens needs run_id or incident_id")
    where = [
        f"toString(run_id) = {_quote(run_id)}" if run_id else "",
        f"toString(incident_id) = {_quote(incident_id)}" if incident_id else "",
    ]
    return (
        "SELECT toInt64OrZero(toString(step)) AS step, "  # noqa: S608 - every literal passed _ident/_int/_float
        "max(toInt64OrZero(toString(context_tokens))) AS context_tokens, "
        "max(toInt64OrZero(toString(naive_tokens))) AS naive_tokens "
        f"FROM {TABLE_EVENTS} WHERE {' AND '.join(w for w in where if w)} "
        "GROUP BY step HAVING context_tokens > 0 ORDER BY step LIMIT 1000"
    )


def _sql_incident_timeline(params: dict[str, Any]) -> str:
    incident_id = _ident(params, "incident_id", required=True)
    assert incident_id is not None  # required=True raised otherwise
    limit = _int(params, "limit", default=500, lo=1, hi=MAX_RESULT_ROWS)
    return (
        "SELECT toString(ts) AS ts, toString(run_id) AS run_id, "  # noqa: S608 - every literal passed _ident/_int/_float
        "toInt64OrZero(toString(step)) AS step, toString(phase) AS phase, "
        "toString(event_type) AS event_type, toString(tool) AS tool, "
        "toString(status) AS status, toInt64OrZero(toString(duration_ms)) AS duration_ms, "
        "toString(source) AS source, toString(message) AS message "
        f"FROM {TABLE_EVENTS} WHERE toString(incident_id) = {_quote(incident_id)} "
        f"ORDER BY {_TS}, step LIMIT {limit}"
    )


NAMED_QUERIES: Final = frozenset(
    {"anomaly_detect", "action_success_rate", "context_tokens", "incident_timeline"}
)


def render_named_query(name: str, params: dict[str, Any], *, zscore_threshold: float = 3.0) -> str:
    """Validated parameters in, SQL out. Unknown names raise."""
    if name == "anomaly_detect":
        return _sql_anomaly_detect(params, zscore_threshold)
    if name == "action_success_rate":
        return _sql_action_success_rate(params)
    if name == "context_tokens":
        return _sql_context_tokens(params)
    if name == "incident_timeline":
        return _sql_incident_timeline(params)
    raise ValidationError(f"unknown named query {name!r}", context={"query": name[:64]})


# --------------------------------------------------------------------------- #
# row shapes                                                                   #
# --------------------------------------------------------------------------- #


def rawtree_ts(value: datetime) -> str:
    """UTC, millisecond precision, the shape ClickHouse parses best-effort."""
    if value.tzinfo is not None:
        value = value.astimezone(UTC)
    return value.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


def event_row(event: HorizonEvent) -> dict[str, Any]:
    """``agent_events`` row.

    Beyond the documented columns, four are lifted from the payload of a
    ``verification_result`` event so ``action_success_rate`` has something to
    aggregate: ``action_type``, ``symptom``, ``verified`` (0/1) and
    ``recovery_s``. They are always present with typed defaults so the
    inferred column types stay stable across rows. The payload itself is not
    shipped: it is bulky and Postgres keeps it.
    """
    payload = event.payload
    verified_raw = payload.get("verified")
    verified = 1 if verified_raw is True or payload.get("verdict") == "VERIFIED" else 0
    recovery = payload.get("recovery_s")
    return {
        "ts": rawtree_ts(event.ts),
        "run_id": event.run_id,
        "incident_id": event.incident_id,
        "step": event.step,
        "phase": event.phase.value,
        "event_type": event.event_type.value,
        "tool": event.tool or "",
        "status": event.status,
        "duration_ms": event.duration_ms,
        "source": event.source.value,
        "context_tokens": event.context_tokens,
        "naive_tokens": event.naive_tokens,
        "message": event.message,
        "action_type": str(payload.get("action_type") or ""),
        "symptom": str(payload.get("symptom") or "")[:120],
        "verified": verified,
        "recovery_s": float(recovery) if isinstance(recovery, (int, float)) else 0.0,
    }


def memory_row(card: MemoryCard, ts: datetime) -> dict[str, Any]:
    return {
        "ts": rawtree_ts(ts),
        "incident_id": card.incident_id,
        "symptoms": card.symptoms,
        "root_cause": card.root_cause,
        "failed_actions": ",".join(card.failed_actions),
        "successful_action": card.successful_action or "",
        "recovery_s": float(card.recovery_s) if card.recovery_s is not None else 0.0,
        "lesson": card.lesson,
    }


def metric_row(row: dict[str, Any]) -> dict[str, Any] | None:
    """Normalise one metric row; ``None`` for a row that cannot be stored.

    ``value`` is always a float in the JSON so the inferred type never flips
    between integer and float from one batch to the next.
    """
    value = row.get("value")
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    ts = row.get("ts")
    return {
        "ts": rawtree_ts(ts) if isinstance(ts, datetime) else str(ts or ""),
        "run_id": str(row.get("run_id") or ""),
        "service": str(row.get("service") or ""),
        "metric": str(row.get("metric") or ""),
        "value": float(value),
    }


# --------------------------------------------------------------------------- #
# client                                                                       #
# --------------------------------------------------------------------------- #


class _NotConfigured(Exception):  # noqa: N818 - internal control flow, never escapes
    """The read path has no key; take the fallback without calling out."""


class RawTreeClient:
    """Implements ``RawTreePort``.

    ``start()`` runs the batch writer; ``aclose()`` flushes (bounded) and
    closes both HTTP clients. ``enqueue_*`` never blocks and never raises.
    """

    def __init__(
        self,
        settings: Settings,
        *,
        fallback_store: HorizonStore | None = None,
        db: Database | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Clock = SYSTEM_CLOCK,
    ) -> None:
        self._settings = settings
        self._fallback_store = fallback_store
        self._db = db
        self._transport = transport
        self._clock = clock
        self._base = settings.rawtree_api_url.rstrip("/")
        self._database = settings.rawtree_database.strip()
        self._write_key = settings.rawtree_write_key.get_secret_value().strip()
        self._read_key = settings.rawtree_read_key.get_secret_value().strip()
        self._write_http: httpx.AsyncClient | None = None
        self._read_http: httpx.AsyncClient | None = None
        self._bulkhead = Bulkhead("rawtree", limit=4)

        cap = settings.rawtree_queue_max
        # Metrics queue holds batches; the others hold rows.
        self._metric_q: asyncio.Queue[list[dict[str, Any]]] = asyncio.Queue(maxsize=cap)
        self._row_q: dict[str, asyncio.Queue[dict[str, Any]]] = {
            t: asyncio.Queue(maxsize=cap) for t in (TABLE_EVENTS, TABLE_OBSERVATIONS, TABLE_MEMORY)
        }

        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        self._flush_lock = asyncio.Lock()

        self.dropped_metric_batches = 0
        self.dropped_metric_rows = 0
        self.failed_metric_rows = 0
        self.skipped_unconfigured = 0
        self.deferred: dict[str, int] = {t: 0 for t in TABLES if t != TABLE_METRICS}
        self.inserted: dict[str, int] = dict.fromkeys(TABLES, 0)
        self.queries_run = 0
        self.query_failures = 0
        self.fallbacks_served = 0
        self.last_error: str | None = None
        self.last_flush_at: datetime | None = None
        self._recent: deque[dict[str, Any]] = deque(maxlen=MAX_RECENT_QUERIES)

    # ------------------------------------------------------------ config

    @property
    def write_configured(self) -> bool:
        return bool(self._write_key)

    @property
    def read_configured(self) -> bool:
        return bool(self._read_key)

    def _params(self) -> dict[str, str]:
        return {"database": self._database} if self._database else {}

    def _client(self, key: str) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=self._base,
            timeout=self._settings.rawtree_timeout_s,
            headers={
                "Authorization": f"Bearer {key}",
                "Accept": "application/json",
                "User-Agent": "aegis-rawtree/1",
            },
            limits=httpx.Limits(max_connections=8, max_keepalive_connections=4),
            transport=self._transport,
        )

    def _writer(self) -> httpx.AsyncClient:
        if self._write_http is None:
            self._write_http = self._client(self._write_key)
        return self._write_http

    def _reader(self) -> httpx.AsyncClient:
        # Built from the read key and nothing else. There is no parameter by
        # which a caller could hand this client a different credential.
        if self._read_http is None:
            self._read_http = self._client(self._read_key)
        return self._read_http

    # ------------------------------------------------------------ enqueue

    def enqueue_metrics(self, rows: list[dict[str, Any]]) -> None:
        if not self.write_configured:
            self.skipped_unconfigured += len(rows)
            return
        batch = [r for r in (metric_row(x) for x in rows[:MAX_ROWS_PER_METRIC_BATCH]) if r]
        if not batch:
            return
        while True:
            try:
                self._metric_q.put_nowait(batch)
                return
            except asyncio.QueueFull:
                # Metrics are the one table where loss is acceptable: the
                # forwarder's ring buffer and Prometheus both still hold them.
                # The oldest batch goes first because it is the least useful to
                # a heartbeat that looks at the last minute.
                try:
                    oldest = self._metric_q.get_nowait()
                except asyncio.QueueEmpty:
                    continue
                self.dropped_metric_batches += 1
                self.dropped_metric_rows += len(oldest)
                log.warning(
                    "rawtree metric batch dropped",
                    rows=len(oldest),
                    dropped_batches=self.dropped_metric_batches,
                )

    def _enqueue_row(self, table: str, row: dict[str, Any], *, incident_id: str) -> None:
        if not self.write_configured:
            self.skipped_unconfigured += 1
            return
        try:
            self._row_q[table].put_nowait(row)
        except asyncio.QueueFull:
            # Never silent: Postgres already holds this row, so it is deferred
            # rather than lost, and the count and the log line say so.
            self.deferred[table] += 1
            log.warning(
                "rawtree queue full; row deferred to postgres",
                table=table,
                incident_id=incident_id,
                deferred=self.deferred[table],
            )

    def enqueue_event(self, event: HorizonEvent) -> None:
        self._enqueue_row(TABLE_EVENTS, event_row(event), incident_id=event.incident_id)

    def enqueue_observation(
        self, *, incident_id: str, evidence_id: str, tool: str, raw: str, ts: datetime
    ) -> None:
        row = {
            "ts": rawtree_ts(ts),
            "incident_id": incident_id,
            "evidence_id": evidence_id,
            "tool": tool,
            "raw": raw[:MAX_RAW_CHARS],
        }
        self._enqueue_row(TABLE_OBSERVATIONS, row, incident_id=incident_id)

    def enqueue_memory_card(self, card: MemoryCard) -> None:
        self._enqueue_row(
            TABLE_MEMORY, memory_row(card, self._clock.now()), incident_id=card.incident_id
        )

    # ------------------------------------------------------------ writer

    async def start(self) -> None:
        if self._task is not None or not self.write_configured:
            if not self.write_configured:
                log.info("rawtree writer not started: write key not configured")
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="rawtree-writer")

    async def _run(self) -> None:
        interval = self._settings.rawtree_flush_interval_ms / 1000
        while not self._stop.is_set():
            with contextlib.suppress(TimeoutError):  # the normal tick; stop was not requested
                await asyncio.wait_for(self._stop.wait(), timeout=interval)
            await self.flush()

    async def flush(self) -> None:
        """Drain every queue once. Failures are counted, never raised."""
        async with self._flush_lock:
            await self._flush_metrics()
            for table, queue in self._row_q.items():
                await self._flush_rows(table, queue)
            self.last_flush_at = self._clock.now()

    async def _flush_metrics(self) -> None:
        rows: list[dict[str, Any]] = []
        while not self._metric_q.empty() and len(rows) < MAX_ROWS_PER_INSERT * 4:
            rows.extend(self._metric_q.get_nowait())
        for chunk in _chunks(rows, MAX_ROWS_PER_INSERT):
            if not await self._insert(TABLE_METRICS, chunk):
                self.failed_metric_rows += len(chunk)

    async def _flush_rows(self, table: str, queue: asyncio.Queue[dict[str, Any]]) -> None:
        rows: list[dict[str, Any]] = []
        while not queue.empty() and len(rows) < MAX_ROWS_PER_INSERT * 4:
            rows.append(queue.get_nowait())
        for chunk in _chunks(rows, MAX_ROWS_PER_INSERT):
            if not await self._insert(table, chunk):
                self.deferred[table] += len(chunk)
                log.warning(
                    "rawtree insert failed; rows deferred to postgres",
                    table=table,
                    rows=len(chunk),
                    error=self.last_error,
                )

    async def _insert(self, table: str, rows: list[dict[str, Any]]) -> bool:
        async def _call() -> int:
            resp = await self._writer().post(
                f"/v1/tables/{table}", params=self._params(), json=rows
            )
            _raise_for(resp, what=f"insert {table}")
            body = resp.json()
            return int(body.get("inserted", len(rows))) if isinstance(body, dict) else len(rows)

        try:
            # attempts=1: an insert is not idempotent, and a retried timeout
            # that had in fact landed would duplicate every row in the batch.
            inserted = await guarded_call(
                _call,
                dependency="rawtree.insert",
                timeout_s=self._settings.rawtree_timeout_s,
                attempts=1,
                bulkhead=self._bulkhead,
            )
        except (AegisError, httpx.HTTPError, ValueError) as exc:
            self.last_error = _describe(exc)
            return False
        self.inserted[table] += inserted
        return True

    async def aclose(self) -> None:
        self._stop.set()
        if self._task is not None:
            try:
                await with_timeout(
                    asyncio.shield(self._task),
                    self._settings.rawtree_timeout_s + 1,
                    what="rawtree writer stop",
                )
            except AegisError as exc:
                self._task.cancel()
                log.warning("rawtree writer did not stop in time", error=str(exc))
            self._task = None
        if self.write_configured:
            try:
                # One final bounded drain so a clean shutdown ships what it holds.
                await with_timeout(
                    self.flush(), self._settings.rawtree_timeout_s * 2, what="rawtree final flush"
                )
            except AegisError as exc:
                log.warning("rawtree final flush incomplete", error=str(exc), **self._depths())
        for client in (self._write_http, self._read_http):
            if client is not None:
                await client.aclose()
        self._write_http = self._read_http = None

    # ------------------------------------------------------------ queries

    async def named_query(self, name: str, params: dict[str, Any]) -> QueryResult:
        """Run a named query; unknown names and invalid parameters raise.

        Unavailability does not raise: the result comes back from the labelled
        fallback, or with ``error`` set and no rows when nothing could answer.
        """
        sql = render_named_query(
            name, params, zscore_threshold=self._settings.heartbeat_zscore_threshold
        )
        started = time.perf_counter()
        reason: str
        try:
            if not self.read_configured:
                raise _NotConfigured
            rows = await self._query(sql)
            result = QueryResult(
                name=name,
                sql=sql,
                rows=rows,
                source=Source.RAWTREE,
                duration_ms=_ms(started),
            )
            self._remember(result)
            return result
        except _NotConfigured:
            reason = "rawtree read key not configured"
        except (AegisError, httpx.HTTPError, ValueError) as exc:
            self.query_failures += 1
            reason = _describe(exc)
            self.last_error = reason
            log.warning("rawtree query failed", query=name, error=reason)

        fallback = await self._fallback(name, params, reason, started)
        self._remember(fallback)
        return fallback

    async def query_sql(self, sql: str) -> list[dict[str, Any]]:
        """Run SQL rendered by this module. Not for caller-supplied text."""
        return await self._query(sql)

    async def _query(self, sql: str) -> list[dict[str, Any]]:
        async def _call() -> list[dict[str, Any]]:
            resp = await self._reader().post("/v1/query", params=self._params(), json={"sql": sql})
            _raise_for(resp, what="query")
            return _rows(resp.json())

        self.queries_run += 1
        return await guarded_call(
            _call,
            dependency="rawtree.query",
            timeout_s=self._settings.rawtree_timeout_s,
            attempts=2,
            bulkhead=self._bulkhead,
        )

    async def _fallback(
        self, name: str, params: dict[str, Any], reason: str, started: float
    ) -> QueryResult:
        handlers: dict[
            str, Callable[[dict[str, Any]], Awaitable[tuple[str, list[dict[str, Any]]]]]
        ] = {
            "incident_timeline": self._pg_timeline,
            "context_tokens": self._pg_context_tokens,
            "action_success_rate": self._pg_success_rate,
        }
        handler = handlers.get(name)
        if handler is None:
            # anomaly_detect: the heartbeat owns the z-score fallback.
            return QueryResult(
                name=name,
                sql="",
                rows=[],
                source=Source.RAWTREE,
                duration_ms=_ms(started),
                error=reason,
            )
        try:
            sql, rows = await handler(params)
        except _NotConfigured:
            return QueryResult(
                name=name,
                sql="",
                rows=[],
                source=Source.SYSTEM,
                duration_ms=_ms(started),
                error=f"{reason}; no postgres fallback wired",
            )
        except (AegisError, OSError, TimeoutError) as exc:
            log.warning("rawtree postgres fallback failed", query=name, error=_describe(exc))
            return QueryResult(
                name=name,
                sql="",
                rows=[],
                source=Source.POSTGRES,
                duration_ms=_ms(started),
                error=f"{reason}; postgres fallback failed: {_describe(exc)}",
            )
        self.fallbacks_served += 1
        log.info("rawtree query served from postgres", query=name, reason=reason)
        return QueryResult(
            name=name,
            sql=sql,
            rows=rows[:MAX_RESULT_ROWS],
            source=Source.POSTGRES,
            duration_ms=_ms(started),
        )

    async def _store_events(self, incident_id: str, cap: int) -> list[tuple[int, HorizonEvent]]:
        store = self._fallback_store
        if store is None:
            raise _NotConfigured
        out: list[tuple[int, HorizonEvent]] = []
        after = 0
        while len(out) < cap:
            page = await with_timeout(
                store.events(incident_id, after_seq=after, limit=500),
                self._settings.rawtree_timeout_s,
                what="horizon event store",
            )
            if not page:
                break
            out.extend(page)
            after = page[-1][0]
            if len(page) < 500:
                break
        return out[:cap]

    async def _pg_timeline(self, params: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
        incident_id = _ident(params, "incident_id", required=True)
        assert incident_id is not None
        limit = _int(params, "limit", default=500, lo=1, hi=MAX_RESULT_ROWS)
        events = await self._store_events(incident_id, limit)
        rows = [
            {
                "ts": e.ts.isoformat(),
                "run_id": e.run_id,
                "step": e.step,
                "phase": e.phase.value,
                "event_type": e.event_type.value,
                "tool": e.tool or "",
                "status": e.status,
                "duration_ms": e.duration_ms,
                "source": e.source.value,
                "message": e.message,
                "seq": seq,
            }
            for seq, e in events
        ]
        return f"horizon_events WHERE incident_id = {_quote(incident_id)} ORDER BY seq", rows

    async def _pg_context_tokens(self, params: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
        run_id = _ident(params, "run_id", required=False)
        incident_id = _ident(params, "incident_id", required=False)
        if not incident_id:
            # The event store is keyed by incident; a run id alone cannot be
            # resolved without RawTree.
            raise _NotConfigured
        per_step: dict[int, dict[str, int]] = {}
        for _seq, e in await self._store_events(incident_id, 5000):
            if run_id and e.run_id != run_id:
                continue
            if e.context_tokens <= 0:
                continue
            cur = per_step.setdefault(
                e.step, {"step": e.step, "context_tokens": 0, "naive_tokens": 0}
            )
            cur["context_tokens"] = max(cur["context_tokens"], e.context_tokens)
            cur["naive_tokens"] = max(cur["naive_tokens"], e.naive_tokens)
        sql = f"horizon_events WHERE incident_id = {_quote(incident_id)} GROUP BY step"
        return sql, [dict(v) for _k, v in sorted(per_step.items())]

    async def _pg_success_rate(self, params: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
        if self._db is None:
            raise _NotConfigured
        days = _int(params, "lookback_days", default=30, lo=1, hi=365)
        action_type = _ident(params, "action_type", required=False)
        _ident(params, "symptom", required=False, pattern=_SYMPTOM_RE)
        # Postgres does not record the symptom an action was aimed at, so this
        # fallback aggregates across symptoms and says so with symptom='*'.
        sql = """
            SELECT a.action_type,
                   '*' AS symptom,
                   count(*) AS attempts,
                   count(*) FILTER (WHERE v.verified) AS verified_successes,
                   avg(EXTRACT(EPOCH FROM (v.completed_at - a.executed_at)))
                       FILTER (WHERE v.verified) AS mean_recovery_s
              FROM remediation_actions a
              LEFT JOIN LATERAL (
                    SELECT bool_or(vr.passed AND vr.verdict = 'VERIFIED') AS verified,
                           max(vr.completed_at) AS completed_at
                      FROM verification_runs vr
                     WHERE vr.action_id = a.id
              ) v ON true
             WHERE a.executed_at IS NOT NULL
               AND a.created_at >= now() - make_interval(days => $1)
               AND ($2::text IS NULL OR a.action_type = $2)
             GROUP BY a.action_type
             ORDER BY attempts DESC
             LIMIT 200
        """
        records = await with_timeout(
            self._db.fetch(sql, days, action_type),
            self._settings.rawtree_timeout_s,
            what="postgres action_success_rate",
        )
        rows = [
            {
                "action_type": r["action_type"],
                "symptom": r["symptom"],
                "attempts": int(r["attempts"]),
                "verified_successes": int(r["verified_successes"]),
                "mean_recovery_s": (
                    float(r["mean_recovery_s"]) if r["mean_recovery_s"] is not None else None
                ),
            }
            for r in records
        ]
        return " ".join(sql.split()), rows

    # ------------------------------------------------------------ status

    def _remember(self, result: QueryResult) -> None:
        self._recent.append(
            {
                "ts": self._clock.now().isoformat(),
                "name": result.name,
                "sql": result.sql,
                "rows": len(result.rows),
                "source": result.source.value,
                "duration_ms": result.duration_ms,
                "error": result.error,
            }
        )

    def recent_queries(self) -> list[dict[str, Any]]:
        """The last few queries, newest last, for the war room's query panel."""
        return list(self._recent)

    def _depths(self) -> dict[str, int]:
        depths = {TABLE_METRICS: self._metric_q.qsize()}
        depths.update({t: q.qsize() for t, q in self._row_q.items()})
        return depths

    def stats(self) -> dict[str, Any]:
        return {
            "write_configured": self.write_configured,
            "read_configured": self.read_configured,
            "database": self._database or "default",
            "running": self._task is not None and not self._task.done(),
            "queue_depths": self._depths(),
            "dropped_metric_batches": self.dropped_metric_batches,
            "dropped_metric_rows": self.dropped_metric_rows,
            "failed_metric_rows": self.failed_metric_rows,
            "deferred": dict(self.deferred),
            "inserted": dict(self.inserted),
            "skipped_unconfigured": self.skipped_unconfigured,
            "queries_run": self.queries_run,
            "query_failures": self.query_failures,
            "fallbacks_served": self.fallbacks_served,
            "last_error": self.last_error,
            "last_flush_at": self.last_flush_at.isoformat() if self.last_flush_at else None,
        }


# --------------------------------------------------------------------------- #
# helpers                                                                      #
# --------------------------------------------------------------------------- #


def _chunks(rows: list[dict[str, Any]], size: int) -> list[list[dict[str, Any]]]:
    return [rows[i : i + size] for i in range(0, len(rows), size)]


def _ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)


def _describe(exc: BaseException) -> str:
    """Error text for stats and logs. Carries no request headers, so no key."""
    return f"{type(exc).__name__}: {exc}"[:_ERROR_BODY_CHARS]


def _raise_for(resp: httpx.Response, *, what: str) -> None:
    if resp.is_success:
        return
    detail = resp.text[:_ERROR_BODY_CHARS]
    # 429 and 5xx may clear on their own; any other 4xx is our defect or a key
    # scope problem, and retrying it only burns the rate limit.
    retryable = resp.status_code == 429 or resp.status_code >= 500
    raise ExternalServiceError(
        f"rawtree {what} returned {resp.status_code}: {detail}",
        code="RAWTREE_HTTP_ERROR",
        context={"status": resp.status_code, "operation": what},
        retryable=retryable,
    )


def _rows(body: Any) -> list[dict[str, Any]]:
    """ClickHouse ``JSON`` output: ``data`` is a list of objects.

    ``JSONCompact`` (lists) is tolerated by zipping with ``meta`` so a server
    default change does not silently produce empty results.
    """
    if not isinstance(body, dict) or "data" not in body:
        raise ValueError("rawtree query response has no data field")
    data = body["data"]
    if not isinstance(data, list):
        raise ValueError("rawtree query data is not a list")
    names = [str(m["name"]) for m in body.get("meta", []) if isinstance(m, dict) and "name" in m]
    out: list[dict[str, Any]] = []
    for item in data[:MAX_RESULT_ROWS]:
        if isinstance(item, dict):
            out.append(item)
        elif isinstance(item, list) and names:
            out.append(dict(zip(names, item, strict=False)))
    return out


__all__ = [
    "NAMED_QUERIES",
    "TABLES",
    "TABLE_EVENTS",
    "TABLE_MEMORY",
    "TABLE_METRICS",
    "TABLE_OBSERVATIONS",
    "TABLE_PREFIX",
    "RawTreeClient",
    "event_row",
    "memory_row",
    "metric_row",
    "rawtree_ts",
    "render_named_query",
]
