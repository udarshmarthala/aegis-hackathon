"""Metrics forwarder and heartbeat: how an incident starts without an alert.

The forwarder scrapes each workload's ``/metrics`` (Prometheus text), derives
the four health signals the rest of the system reasons about - p99 latency
(ms), error rate (0-1), pool utilisation (0-1) and requests per second - and
ships them to RawTree's ``metrics`` table. It also keeps a bounded in-process
ring buffer of the same values, which is what the heartbeat falls back to when
RawTree cannot be read.

The heartbeat asks "is anything anomalous?" every few seconds: RawTree's
``anomaly_detect`` when the read key is configured, otherwise a z-score over the
ring buffer. Either way a candidate only fires if it also crosses an absolute
floor, so a flat baseline wobbling by a rounding error cannot open an incident.
One anomaly per service stays open until that service is healthy again.

A failed scrape is recorded as unavailable. It never becomes a zero: a zero
error rate from a service we could not reach is a lie that reads as health.
"""

from __future__ import annotations

import asyncio
import contextlib
import math
import statistics
import time
from collections import deque
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Final

import httpx
from prometheus_client.parser import text_string_to_metric_families

from aegis.agents.horizon.ports import RawTreePort
from aegis.core.clock import SYSTEM_CLOCK, Clock
from aegis.core.config import Settings
from aegis.core.errors import AegisError
from aegis.core.ids import correlation_id
from aegis.core.logging import get_logger
from aegis.core.resilience import guarded_call
from aegis.domain.enums import Severity
from aegis.domain.horizon import Source
from aegis.persistence.db import Database
from aegis.persistence.incidents import IncidentRepository
from aegis.persistence.jobs import JobQueue

log = get_logger(__name__)

P99_MS: Final = "p99_ms"
ERROR_RATE: Final = "error_rate"
POOL_UTILISATION: Final = "pool_utilisation"
RPS: Final = "rps"
HEALTH_METRICS: Final = (P99_MS, ERROR_RATE, POOL_UTILISATION)

# A candidate must be abnormal relative to its own history *and* bad in
# absolute terms. These sit just inside the verification thresholds (p99 <
# 250 ms, error rate < 0.02, pool < 0.8) so detection leads verification.
ABSOLUTE_FLOORS: Final[dict[str, float]] = {
    POOL_UTILISATION: 0.7,
    P99_MS: 250.0,
    ERROR_RATE: 0.02,
}

RING_SECONDS: Final = 15 * 60
RECENT_WINDOW_S: Final = 60.0
BASELINE_WINDOW_S: Final = 600.0
MIN_BASELINE_SAMPLES: Final = 6

_REQUESTS: Final = "http_requests_total"
_BUCKET: Final = "http_request_duration_seconds_bucket"
_IN_USE: Final = "connection_pool_in_use"
_SIZE: Final = "connection_pool_size"


# --------------------------------------------------------------------------- #
# parsing                                                                      #
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class Scrape:
    """Cumulative counters and gauges from one scrape of one service."""

    at: float  # monotonic seconds
    requests: float = 0.0
    errors: float = 0.0
    buckets: dict[float, float] = field(default_factory=dict)  # le -> cumulative
    pool_in_use: float | None = None
    pool_size: float | None = None


def parse_metrics(text: str, service: str) -> Scrape:
    """Aggregate one exposition across endpoints for ``service``.

    Samples labelled for a different service are ignored, so a shared registry
    cannot leak another service's numbers into this one.
    """
    scrape = Scrape(at=0.0)
    for family in text_string_to_metric_families(text):
        for sample in family.samples:
            labels = sample.labels
            if labels.get("service", service) != service:
                continue
            name, value = sample.name, float(sample.value)
            if name == _REQUESTS:
                scrape.requests += value
                if labels.get("status", "").startswith("5"):
                    scrape.errors += value
            elif name == _BUCKET:
                le = float(labels.get("le", "+Inf"))
                scrape.buckets[le] = scrape.buckets.get(le, 0.0) + value
            elif name == _IN_USE:
                scrape.pool_in_use = (scrape.pool_in_use or 0.0) + value
            elif name == _SIZE:
                scrape.pool_size = (scrape.pool_size or 0.0) + value
    return scrape


def _delta(cur: float, prev: float) -> float:
    # A counter that went backwards means the process restarted; everything
    # since the restart is the delta.
    return cur - prev if cur >= prev else cur


def p99_from_buckets(deltas: dict[float, float]) -> float | None:
    """p99 in seconds from cumulative bucket deltas, interpolated linearly."""
    if not deltas:
        return None
    bounds = sorted(deltas)
    total = deltas[bounds[-1]]
    if total <= 0:
        return None
    target = 0.99 * total
    prev_bound, prev_count = 0.0, 0.0
    for bound in bounds:
        count = deltas[bound]
        if count >= target:
            if math.isinf(bound):
                # Past the last finite bucket: report that bound, as
                # Prometheus's histogram_quantile does.
                return prev_bound
            span = count - prev_count
            frac = (target - prev_count) / span if span > 0 else 1.0
            return prev_bound + (bound - prev_bound) * frac
        prev_bound, prev_count = bound, count
    return prev_bound


def derive(prev: Scrape | None, cur: Scrape) -> dict[str, float]:
    """Health signals from two scrapes. Missing signals are absent, never 0."""
    out: dict[str, float] = {}
    if cur.pool_in_use is not None and cur.pool_size:
        out[POOL_UTILISATION] = max(0.0, min(1.0, cur.pool_in_use / cur.pool_size))
    if prev is None:
        return out
    dt = cur.at - prev.at
    d_req = _delta(cur.requests, prev.requests)
    d_err = _delta(cur.errors, prev.errors)
    if dt > 0:
        out[RPS] = d_req / dt
    if d_req > 0:
        out[ERROR_RATE] = max(0.0, min(1.0, d_err / d_req))
    deltas = {le: _delta(c, prev.buckets.get(le, 0.0)) for le, c in cur.buckets.items()}
    p99 = p99_from_buckets(deltas)
    if p99 is not None:
        out[P99_MS] = p99 * 1000.0
    return out


# --------------------------------------------------------------------------- #
# forwarder                                                                    #
# --------------------------------------------------------------------------- #


class MetricsForwarder:
    """Scrape -> derive -> RawTree + ring buffer, every ``forwarder_interval_s``."""

    def __init__(
        self,
        settings: Settings,
        rawtree: RawTreePort,
        transport: httpx.AsyncBaseTransport | None = None,
        *,
        clock: Clock = SYSTEM_CLOCK,
        run_id: str = "",
    ) -> None:
        self._settings = settings
        self._rawtree = rawtree
        self._transport = transport
        self._clock = clock
        self._run_id = run_id
        self._targets = settings.metrics_targets
        self._interval = settings.forwarder_interval_s
        maxlen = int(RING_SECONDS / self._interval) + 2
        self._ring: dict[tuple[str, str], deque[tuple[float, float]]] = {}
        self._ring_len = maxlen
        self._prev: dict[str, Scrape] = {}
        self.unavailable: dict[str, str] = {}
        self.last_scrape_at: datetime | None = None
        self._http: httpx.AsyncClient | None = None

    @property
    def services(self) -> list[str]:
        return list(self._targets)

    def _client(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(
                timeout=min(self._interval, 3.0), transport=self._transport
            )
        return self._http

    async def _fetch(self, service: str, url: str) -> str:
        async def _call() -> str:
            resp = await self._client().get(url)
            resp.raise_for_status()
            return resp.text

        return await guarded_call(
            _call, dependency=f"metrics.{service}", timeout_s=min(self._interval, 3.0), attempts=1
        )

    async def scrape_once(self) -> list[dict[str, Any]]:
        """One pass over every target. Returns the rows it enqueued."""
        now = self._clock.now()
        mono = self._clock.monotonic()
        rows: list[dict[str, Any]] = []
        results = await asyncio.gather(
            *(self._fetch(s, u) for s, u in self._targets.items()), return_exceptions=True
        )
        for service, result in zip(self._targets, results, strict=True):
            if isinstance(result, BaseException):
                if not isinstance(result, (AegisError, httpx.HTTPError, OSError)):
                    raise result
                self.unavailable[service] = f"{type(result).__name__}: {result}"[:200]
                continue
            try:
                scrape = parse_metrics(result, service)
            except ValueError as exc:
                self.unavailable[service] = f"unparseable metrics: {exc}"[:200]
                continue
            scrape.at = mono
            signals = derive(self._prev.get(service), scrape)
            self._prev[service] = scrape
            self.unavailable.pop(service, None)
            for metric, value in signals.items():
                self._record(service, metric, mono, value)
                rows.append(
                    {
                        "ts": now,
                        "run_id": self._run_id,
                        "service": service,
                        "metric": metric,
                        "value": value,
                    }
                )
        self.last_scrape_at = now
        if rows:
            self._rawtree.enqueue_metrics(rows)
        return rows

    def _record(self, service: str, metric: str, at: float, value: float) -> None:
        ring = self._ring.get((service, metric))
        if ring is None:
            ring = self._ring[(service, metric)] = deque(maxlen=self._ring_len)
        ring.append((at, value))

    def history(self, service: str, metric: str) -> list[tuple[float, float]]:
        return list(self._ring.get((service, metric), ()))

    def series(self) -> Iterable[tuple[str, str]]:
        return list(self._ring)

    def latest(self, service: str) -> dict[str, float | None]:
        """Latest value per health metric; ``None`` when unknown or unavailable."""
        if service in self.unavailable:
            return dict.fromkeys((*HEALTH_METRICS, RPS))
        return {
            m: (ring[-1][1] if (ring := self._ring.get((service, m))) else None)
            for m in (*HEALTH_METRICS, RPS)
        }

    async def run(self, stop: asyncio.Event) -> None:
        log.info("metrics forwarder started", targets=list(self._targets))
        while not stop.is_set():
            try:
                await self.scrape_once()
            except (AegisError, httpx.HTTPError, OSError, ValueError) as exc:
                log.warning("metrics forwarder pass failed", error=str(exc))
            with contextlib.suppress(TimeoutError):  # next tick
                await asyncio.wait_for(stop.wait(), timeout=self._interval)
        if self._http is not None:
            await self._http.aclose()
            self._http = None


# --------------------------------------------------------------------------- #
# heartbeat                                                                    #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class Anomaly:
    service: str
    metric: str
    value: float
    baseline_mean: float
    baseline_std: float
    z: float
    source: Source  # rawtree | zscore
    detected_at: datetime

    def to_dict(self) -> dict[str, Any]:
        out = asdict(self)
        out["source"] = self.source.value
        out["detected_at"] = self.detected_at.isoformat()
        return out


def _effective_std(std: float, mean: float) -> float:
    return max(std, 0.05 * abs(mean), 0.001)


def zscore_candidates(
    forwarder: MetricsForwarder, *, threshold: float, now_mono: float
) -> list[Anomaly]:
    """In-process z-score over the ring buffer (last 60 s vs prior 10 min)."""
    out: list[Anomaly] = []
    for service, metric in forwarder.series():
        if metric not in ABSOLUTE_FLOORS:
            continue
        points = forwarder.history(service, metric)
        recent = [v for t, v in points if now_mono - t <= RECENT_WINDOW_S]
        base = [
            v
            for t, v in points
            if RECENT_WINDOW_S < now_mono - t <= RECENT_WINDOW_S + BASELINE_WINDOW_S
        ]
        if not recent or len(base) < MIN_BASELINE_SAMPLES:
            continue
        mean = statistics.fmean(base)
        std = statistics.pstdev(base)
        value = statistics.fmean(recent)
        z = (value - mean) / _effective_std(std, mean)
        if z > threshold:
            out.append(Anomaly(service, metric, value, mean, std, z, Source.ZSCORE, datetime.min))
    return out


class Heartbeat:
    """Anomaly detection loop. ``on_anomaly`` is called once per open anomaly."""

    def __init__(
        self,
        settings: Settings,
        rawtree: RawTreePort,
        forwarder: MetricsForwarder,
        on_anomaly: Callable[[Anomaly], Awaitable[None]],
        clock: Clock = SYSTEM_CLOCK,
    ) -> None:
        self._settings = settings
        self._rawtree = rawtree
        self._forwarder = forwarder
        self._on_anomaly = on_anomaly
        self._clock = clock
        self._threshold = settings.heartbeat_zscore_threshold
        self.open: dict[str, Anomaly] = {}
        self.last_source: Source | None = None
        self.last_error: str | None = None

    async def _candidates(self) -> list[Anomaly]:
        now = self._clock.now()
        if self._rawtree.read_configured:
            result = await self._rawtree.named_query(
                "anomaly_detect", {"threshold": self._threshold}
            )
            if result.error is None:
                self.last_source = Source.RAWTREE
                return [a for row in result.rows if (a := _from_row(row, now)) is not None]
            self.last_error = result.error
            log.warning("heartbeat falling back to in-process z-score", reason=result.error)
        self.last_source = Source.ZSCORE
        return [
            Anomaly(
                a.service, a.metric, a.value, a.baseline_mean, a.baseline_std, a.z, a.source, now
            )
            for a in zscore_candidates(
                self._forwarder, threshold=self._threshold, now_mono=self._clock.monotonic()
            )
        ]

    def _healthy(self, service: str) -> bool:
        latest = self._forwarder.latest(service)
        known = [(m, latest.get(m)) for m in ABSOLUTE_FLOORS]
        if any(v is None for _m, v in known):
            return False  # unknown is not healthy
        return all(v is not None and v < ABSOLUTE_FLOORS[m] for m, v in known)

    async def check_once(self) -> list[Anomaly]:
        """One detection pass. Returns the anomalies newly emitted."""
        candidates = [
            a
            for a in await self._candidates()
            if a.metric in ABSOLUTE_FLOORS and a.value >= ABSOLUTE_FLOORS[a.metric]
        ]
        flagged = {a.service for a in candidates}
        for service in list(self.open):
            if service not in flagged and self._healthy(service):
                log.info("heartbeat anomaly cleared", service=service)
                del self.open[service]

        emitted: list[Anomaly] = []
        for anomaly in sorted(candidates, key=lambda a: -a.z):
            if anomaly.service in self.open:
                continue  # debounced: one open anomaly per service
            self.open[anomaly.service] = anomaly
            emitted.append(anomaly)
            log.warning(
                "heartbeat anomaly",
                service=anomaly.service,
                metric=anomaly.metric,
                value=round(anomaly.value, 4),
                z=round(anomaly.z, 2),
                source=anomaly.source.value,
            )
            try:
                await self._on_anomaly(anomaly)
            except Exception as exc:  # noqa: BLE001 - the detector must outlive its consumer
                log.error("heartbeat on_anomaly failed", service=anomaly.service, error=str(exc))
        return emitted

    def clear(self, service: str) -> None:
        self.open.pop(service, None)

    async def run(self, stop: asyncio.Event) -> None:
        interval = self._settings.heartbeat_interval_s
        log.info("heartbeat started", interval_s=interval, rawtree=self._rawtree.read_configured)
        while not stop.is_set():
            started = time.perf_counter()
            try:
                await self.check_once()
            except (AegisError, httpx.HTTPError, OSError, ValueError) as exc:
                self.last_error = str(exc)
                log.warning("heartbeat pass failed", error=str(exc))
            remaining = max(0.1, interval - (time.perf_counter() - started))
            with contextlib.suppress(TimeoutError):  # next tick
                await asyncio.wait_for(stop.wait(), timeout=remaining)


def _from_row(row: dict[str, Any], now: datetime) -> Anomaly | None:
    try:
        return Anomaly(
            service=str(row["service"]),
            metric=str(row["metric"]),
            value=float(row["recent_value"]),
            baseline_mean=float(row["baseline_mean"]),
            baseline_std=float(row["baseline_std"]),
            z=float(row["z"]),
            source=Source.RAWTREE,
            detected_at=now,
        )
    except (KeyError, TypeError, ValueError):
        log.warning("anomaly row unparseable", row=str(row)[:200])
        return None


# --------------------------------------------------------------------------- #
# anomaly -> incident                                                          #
# --------------------------------------------------------------------------- #

HEARTBEAT_SOURCE: Final = "heartbeat"


async def open_incident_from_anomaly(
    db: Database,
    anomaly: Anomaly,
    *,
    environment: str = "local",
    workload: str = "default",
) -> str | None:
    """Open an incident and schedule its investigation, as alert ingestion does.

    Same transaction, same rows: incident, ``incident_alerts`` (source
    ``heartbeat``), ``audit_log`` and the ``investigate`` job. Returns the new
    incident id, or ``None`` when an open incident already covers the service.
    The advisory lock makes the dedupe check and the insert atomic across
    workers running their own heartbeats.
    """
    incidents = IncidentRepository(db)
    jobs = JobQueue(db)
    cid = correlation_id()
    title = (
        f"{anomaly.service}: {anomaly.metric} anomalous ({anomaly.value:.3g}, z={anomaly.z:.1f})"
    )
    external_id = (
        f"{HEARTBEAT_SOURCE}:{anomaly.service}:{anomaly.metric}:{anomaly.detected_at.isoformat()}"
    )
    labels = {
        "service": anomaly.service,
        "metric": anomaly.metric,
        "detector": anomaly.source.value,
    }

    async with db.transaction() as conn:
        await conn.execute(
            "SELECT pg_advisory_xact_lock(hashtext($1))", f"heartbeat:{anomaly.service}"
        )
        existing = await conn.fetchval(
            """
            SELECT i.id FROM incidents i
             WHERE i.resolved_at IS NULL
               AND ($1 = ANY(i.affected_services)
                    OR EXISTS (SELECT 1 FROM incident_alerts a
                                WHERE a.incident_id = i.id AND a.service_hint = $1))
             ORDER BY i.created_at DESC LIMIT 1
            """,
            anomaly.service,
        )
        if existing is not None:
            log.info(
                "heartbeat anomaly attached to open incident",
                incident_id=existing,
                service=anomaly.service,
            )
            return None

        incident = await incidents.create(
            title=title[:512],
            severity=Severity.P2,
            environment=environment,
            workload=workload,
            correlation_id=cid,
            conn=conn,
        )
        await conn.execute(
            "UPDATE incidents SET affected_services = $2 WHERE id = $1",
            incident.id,
            [anomaly.service],
        )
        alert_id = f"alr_{correlation_id()}"
        await conn.execute(
            """
            INSERT INTO incident_alerts
                (id, incident_id, source, external_id, title, severity,
                 service_hint, labels, annotations, raw_payload, started_at)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)
            """,
            alert_id,
            incident.id,
            HEARTBEAT_SOURCE,
            external_id[:256],
            title[:512],
            Severity.P2.value,
            anomaly.service,
            labels,
            {},
            anomaly.to_dict(),
            anomaly.detected_at,
        )
        await conn.execute(
            """
            INSERT INTO audit_log
                (incident_id, actor, actor_type, event_type, resource_type,
                 resource_id, detail, correlation_id)
            VALUES ($1,$2,'system','alert_ingested','alert',$3,$4,$5)
            """,
            incident.id,
            HEARTBEAT_SOURCE,
            alert_id,
            {
                "external_id": external_id[:256],
                "severity": Severity.P2.value,
                "detector": anomaly.source.value,
            },
            cid,
        )
        await jobs.enqueue(
            incident_id=incident.id,
            kind="investigate",
            payload={
                "trigger": HEARTBEAT_SOURCE,
                "alert_id": alert_id,
                "service": anomaly.service,
                "metric": anomaly.metric,
            },
            conn=conn,
        )

    log.info(
        "incident opened by heartbeat",
        incident_id=incident.id,
        service=anomaly.service,
        metric=anomaly.metric,
        detector=anomaly.source.value,
    )
    return incident.id


__all__ = [
    "ABSOLUTE_FLOORS",
    "Anomaly",
    "Heartbeat",
    "MetricsForwarder",
    "derive",
    "open_incident_from_anomaly",
    "p99_from_buckets",
    "parse_metrics",
    "zscore_candidates",
]
