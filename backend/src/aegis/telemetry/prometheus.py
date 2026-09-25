"""Prometheus evidence source.

A soft dependency. Every failure becomes ``SourceUnavailable``, which the
investigation records as an evidence gap and continues with lower confidence,
rather than aborting.

Queries are built from a small set of templates with the service name bound as a
PromQL label matcher. Callers never pass raw PromQL.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

import httpx

from aegis.core.config import Settings
from aegis.core.errors import SourceNotConfigured, SourceUnavailable, is_unset
from aegis.core.logging import get_logger
from aegis.core.resilience import Bulkhead, guarded_call

log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class MetricPoint:
    timestamp: float
    value: float


@dataclass(frozen=True, slots=True)
class MetricSeries:
    """A resolved series plus the exact query that produced it.

    ``query`` is carried through into evidence provenance so an operator can
    re-run it and confirm the citation themselves.
    """

    metric: str
    labels: dict[str, str]
    points: list[MetricPoint]
    query: str

    @property
    def latest(self) -> float | None:
        return self.points[-1].value if self.points else None

    @property
    def mean(self) -> float | None:
        return sum(p.value for p in self.points) / len(self.points) if self.points else None

    @property
    def peak(self) -> float | None:
        return max((p.value for p in self.points), default=None)

    def delta_vs(self, other: MetricSeries) -> float | None:
        """Relative change against a baseline window, used for before/after."""
        a, b = other.mean, self.mean
        if a is None or b is None or a == 0:
            return None
        return (b - a) / abs(a)


def escape_label(value: str) -> str:
    """Escape a value for safe inclusion in a PromQL label matcher."""
    return value.replace("\\", "").replace('"', "").replace("\n", "")


class PrometheusClient:
    __slots__ = ("_settings", "_bulkhead", "_client")

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._bulkhead = Bulkhead("prometheus", limit=8)
        self._client: httpx.AsyncClient | None = None

    @property
    def configured(self) -> bool:
        """False when ``PROMETHEUS_URL`` is empty - Prometheus is not deployed."""
        return not is_unset(self._settings.prometheus_url)

    def _require_configured(self) -> None:
        # Checked before ``guarded_call`` rather than inside it: an absent
        # deployment is not an outage, so it must neither be retried nor count
        # against the breaker that protects a real Prometheus.
        if not self.configured:
            raise SourceNotConfigured.for_setting("prometheus", "PROMETHEUS_URL")

    async def _http(self) -> httpx.AsyncClient:
        self._require_configured()
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self._settings.prometheus_url,
                timeout=self._settings.source_timeout_s,
                limits=httpx.Limits(max_connections=16, max_keepalive_connections=8),
            )
        return self._client

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def query_range(
        self, promql: str, *, start: float, end: float, step: float = 15.0
    ) -> list[MetricSeries]:
        """Range query. Raises SourceUnavailable rather than returning empty.

        The distinction matters: an empty result means 'no such data', an
        exception means 'we could not look'. Collapsing them would let the
        investigation silently treat an outage as evidence of health.
        """
        self._require_configured()

        async def _call() -> dict[str, Any]:
            client = await self._http()
            resp = await client.get(
                "/api/v1/query_range",
                params={"query": promql, "start": start, "end": end, "step": step},
            )
            resp.raise_for_status()
            payload: dict[str, Any] = resp.json()
            return payload

        try:
            payload = await guarded_call(
                _call,
                dependency="prometheus",
                timeout_s=self._settings.source_timeout_s,
                attempts=2,
                bulkhead=self._bulkhead,
            )
        except Exception as exc:
            raise SourceUnavailable(
                f"prometheus unavailable: {type(exc).__name__}",
                context={"dependency": "prometheus", "query": promql},
            ) from exc

        if payload.get("status") != "success":
            raise SourceUnavailable(
                "prometheus returned an error",
                context={"query": promql, "error": payload.get("error", "")},
            )

        out: list[MetricSeries] = []
        for series in payload.get("data", {}).get("result", []):
            metric_labels = dict(series.get("metric", {}))
            points = [
                MetricPoint(timestamp=float(ts), value=float(val))
                for ts, val in series.get("values", [])
                # Prometheus renders absent samples as NaN strings.
                if val not in ("NaN", "+Inf", "-Inf")
            ]
            out.append(
                MetricSeries(
                    metric=metric_labels.get("__name__", promql),
                    labels=metric_labels,
                    points=points,
                    query=promql,
                )
            )
        return out

    async def instant(self, promql: str) -> list[MetricSeries]:
        now = time.time()
        return await self.query_range(promql, start=now - 60, end=now, step=30)

    # ---- templated operational queries -------------------------------------

    async def error_rate(self, service: str, window_s: int = 900) -> list[MetricSeries]:
        svc = escape_label(service)
        # `or vector(0)` on the numerator is load-bearing. A service with no 5xx
        # has no series on the left of the division, and a PromQL binary
        # operator with an empty side yields an empty result - so a perfectly
        # healthy service reported "no data" rather than "zero errors". Those
        # are different states: one is a finding, the other is an evidence gap,
        # and collapsing them is the exact confusion this codebase forbids.
        q = (
            f'(sum(rate(http_requests_total{{service="{svc}",status=~"5.."}}[1m]))'
            f" or vector(0))"
            f' / clamp_min(sum(rate(http_requests_total{{service="{svc}"}}[1m])), 0.001)'
        )
        now = time.time()
        return await self.query_range(q, start=now - window_s, end=now)

    async def latency_p99(self, service: str, window_s: int = 900) -> list[MetricSeries]:
        svc = escape_label(service)
        q = (
            "histogram_quantile(0.99, sum by (le) (rate("
            f'http_request_duration_seconds_bucket{{service="{svc}"}}[1m])))'
        )
        now = time.time()
        return await self.query_range(q, start=now - window_s, end=now)

    async def request_rate(self, service: str, window_s: int = 900) -> list[MetricSeries]:
        svc = escape_label(service)
        q = f'sum(rate(http_requests_total{{service="{svc}"}}[1m]))'
        now = time.time()
        return await self.query_range(q, start=now - window_s, end=now)

    # ---- saturation --------------------------------------------------------
    #
    # RED metrics say a service is slow. They cannot say why, and the difference
    # between "the CPU is pinned", "the connection pool is full" and "the cache
    # went cold" is exactly what a remediation decision turns on - scale out,
    # raise the pool, or wait for it to warm are three different actions with
    # three different blast radii.
    #
    # Without these, a scenario whose answer key says `resource_saturation` can
    # only be answered by inference from latency, which is a guess dressed as a
    # diagnosis. Adding them here rather than opening up PromQL keeps the
    # catalogue closed: an agent still selects from a reviewed list.

    async def cpu_utilisation(self, service: str, window_s: int = 900) -> list[MetricSeries]:
        """CPU seconds burned per second - 1.0 means one core fully occupied."""
        svc = escape_label(service)
        q = f'sum(rate(process_cpu_seconds_total{{service="{svc}"}}[1m]))'
        now = time.time()
        return await self.query_range(q, start=now - window_s, end=now)

    async def memory_bytes(self, service: str, window_s: int = 900) -> list[MetricSeries]:
        """Resident set size. A leak is a trend here long before it is a kill."""
        svc = escape_label(service)
        q = f'sum(process_resident_memory_bytes{{service="{svc}"}})'
        now = time.time()
        return await self.query_range(q, start=now - window_s, end=now)

    async def pool_saturation(self, service: str, window_s: int = 900) -> list[MetricSeries]:
        """Connections in use as a share of capacity, 0 to 1.

        A ratio rather than a raw count, because "18 connections" means nothing
        without the size beside it, and a model comparing a raw count against a
        threshold it invented is the failure mode this catalogue exists to stop.
        """
        svc = escape_label(service)
        # The numerator is defaulted, the denominator is not. A service that
        # has a pool but is not currently using it is genuinely at 0.0
        # saturation; a service that reports no pool size has no pool, and that
        # is not applicable rather than zero.
        q = (
            f'(sum(connection_pool_in_use{{service="{svc}"}}) or vector(0))'
            f' / clamp_min(sum(connection_pool_size{{service="{svc}"}}), 1)'
        )
        now = time.time()
        return await self.query_range(q, start=now - window_s, end=now)

    async def queue_depth(self, service: str, window_s: int = 900) -> list[MetricSeries]:
        """Requests waiting for a worker or a connection.

        Sustained depth above zero is queueing, which is what distinguishes a
        starved pool from a slow dependency: both raise latency, only one has a
        queue behind it.
        """
        svc = escape_label(service)
        # Both sides defaulted, for the same reason as error_rate: a service
        # that has never queued on a worker has no worker_queue_depth series,
        # and without the default the sum of the two is empty rather than zero.
        q = (
            f'(sum(worker_queue_depth{{service="{svc}"}}) or vector(0))'
            f' + (sum(connection_pool_waiting{{service="{svc}"}}) or vector(0))'
        )
        now = time.time()
        return await self.query_range(q, start=now - window_s, end=now)

    async def cache_hit_ratio(self, service: str, window_s: int = 900) -> list[MetricSeries]:
        """Hits as a share of lookups, 0 to 1.

        A cliff here at the same moment as a latency rise is a cold cache; a
        latency rise without one is not, however much it looks like one.
        """
        svc = escape_label(service)
        # A service with no cache has no series at all, and that stays "no
        # data" deliberately: "this service does not cache" is not the same
        # claim as "its hit rate is zero", and an evaluator reads None as not
        # applicable rather than as a failing measurement.
        # Deliberately NOT defaulted, unlike error_rate. A service with no cache
        # emits no cache series, and defaulting would report a hit ratio of
        # 0.0 - which reads as "every lookup missed", an alarming claim about a
        # service that simply does not cache. Absent stays absent so the
        # evaluator reads it as not applicable rather than as a failure.
        hits = f'sum(rate(cache_hits_total{{service="{svc}"}}[1m]))'
        misses = f'(sum(rate(cache_misses_total{{service="{svc}"}}[1m])) or vector(0))'
        q = f"{hits} / clamp_min({hits} + {misses}, 0.001)"
        now = time.time()
        return await self.query_range(q, start=now - window_s, end=now)

    async def restart_count(self, service: str, window_s: int = 900) -> list[MetricSeries]:
        """Distinct process start times in the window.

        A crash loop is otherwise invisible to RED metrics: the service answers
        fine between restarts, and the gaps read as a scrape problem.
        """
        svc = escape_label(service)
        q = f'changes(workload_start_time_seconds{{service="{svc}"}}[{int(window_s)}s])'
        now = time.time()
        return await self.query_range(q, start=now - window_s, end=now)

    async def known_services(self) -> list[str]:
        """Discover services from telemetry, so topology is never hardcoded.

        Raises ``SourceUnavailable`` rather than returning an empty list. An
        empty list means "Prometheus knows of no services"; an exception means
        "we could not ask". The caller must be able to tell those apart, or it
        will record no evidence gap and quietly reason from nothing.
        """
        self._require_configured()

        async def _call() -> list[str]:
            client = await self._http()
            resp = await client.get("/api/v1/label/service/values")
            resp.raise_for_status()
            return sorted(resp.json().get("data", []))

        try:
            return await guarded_call(
                _call,
                dependency="prometheus",
                timeout_s=self._settings.source_timeout_s,
                attempts=2,
                bulkhead=self._bulkhead,
            )
        except Exception as exc:
            raise SourceUnavailable(
                f"prometheus service discovery failed: {type(exc).__name__}",
                context={"dependency": "prometheus"},
            ) from exc
