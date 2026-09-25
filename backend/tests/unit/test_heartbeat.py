"""Forwarder parsing, heartbeat detection/debounce, and anomaly -> incident."""

from __future__ import annotations

import random
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram, generate_latest

from aegis.agents.horizon.ports import QueryResult
from aegis.core.clock import FrozenClock
from aegis.core.config import Settings
from aegis.core.resilience import reset_breakers
from aegis.domain.horizon import HorizonEvent, MemoryCard, Source
from aegis.telemetry.rawtree import (
    ERROR_RATE,
    P99_MS,
    POOL_UTILISATION,
    Anomaly,
    Heartbeat,
    MetricsForwarder,
    derive,
    open_incident_from_anomaly,
    p99_from_buckets,
    parse_metrics,
)

URL = "http://checkout:8080/metrics"


@pytest.fixture(autouse=True)
def _breakers() -> None:
    reset_breakers()


def _settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "rawtree_write_key": "",
        "rawtree_read_key": "",
        "workload_metrics_targets": f"checkout={URL}",
        "forwarder_interval_s": 5.0,
        "heartbeat_interval_s": 5.0,
        "heartbeat_zscore_threshold": 3.0,
    }
    base.update(overrides)
    return Settings(_env_file=None, **base)  # type: ignore[call-arg]


class Workload:
    """The workload's real metric names and labels, in a private registry.

    Mirrors ``workload/service.py`` (same names, label sets and buckets) so the
    forwarder is tested against genuine Prometheus exposition text.
    """

    def __init__(self, pool_size: int = 100) -> None:
        self.registry = CollectorRegistry()
        self.requests = Counter(
            "http_requests_total", "r", ["service", "endpoint", "status"], registry=self.registry
        )
        self.latency = Histogram(
            "http_request_duration_seconds",
            "d",
            ["service", "endpoint"],
            buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0),
            registry=self.registry,
        )
        self.in_use = Gauge("connection_pool_in_use", "u", ["service"], registry=self.registry)
        self.size = Gauge("connection_pool_size", "s", ["service"], registry=self.registry)
        self.size.labels("checkout").set(pool_size)
        # A different service's samples in the same text must be ignored.
        self.in_use.labels("payment").set(99)

    def traffic(self, ok: int, err: int, latency_s: float) -> None:
        self.requests.labels("checkout", "/work", "200").inc(ok)
        if err:
            self.requests.labels("checkout", "/work", "500").inc(err)
        for _ in range(ok + err):
            self.latency.labels("checkout", "/work").observe(latency_s)

    def text(self) -> str:
        return generate_latest(self.registry).decode()


class FakeRawTree:
    def __init__(self, *, read: bool = False, result: QueryResult | None = None) -> None:
        self.metrics: list[dict[str, Any]] = []
        self._read = read
        self.result = result
        self.queries: list[str] = []

    @property
    def write_configured(self) -> bool:
        return True

    @property
    def read_configured(self) -> bool:
        return self._read

    def enqueue_metrics(self, rows: list[dict[str, Any]]) -> None:
        self.metrics.extend(rows)

    def enqueue_event(self, event: HorizonEvent) -> None:
        raise AssertionError("not used")

    def enqueue_observation(self, **_kw: Any) -> None:
        raise AssertionError("not used")

    def enqueue_memory_card(self, card: MemoryCard) -> None:
        raise AssertionError("not used")

    async def named_query(self, name: str, params: dict[str, Any]) -> QueryResult:
        self.queries.append(name)
        assert self.result is not None
        return self.result

    def stats(self) -> dict[str, Any]:
        return {}


def _forwarder(workload: Workload, clock: FrozenClock, rawtree: FakeRawTree) -> MetricsForwarder:
    transport = httpx.MockTransport(lambda _r: httpx.Response(200, text=workload.text()))
    return MetricsForwarder(_settings(), rawtree, transport, clock=clock)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# parsing and derivation                                                       #
# --------------------------------------------------------------------------- #


def test_parse_real_exposition_ignores_other_services() -> None:
    w = Workload()
    w.in_use.labels("checkout").set(40)
    w.traffic(ok=90, err=10, latency_s=0.03)
    scrape = parse_metrics(w.text(), "checkout")
    assert scrape.requests == 100 and scrape.errors == 10
    assert scrape.pool_in_use == 40 and scrape.pool_size == 100
    assert scrape.buckets[float("inf")] == 100


def test_p99_interpolates_within_the_right_bucket() -> None:
    # 100 requests: 98 under 50 ms, 2 between 250 and 500 ms.
    deltas = {0.05: 98.0, 0.1: 98.0, 0.25: 98.0, 0.5: 100.0, float("inf"): 100.0}
    p99 = p99_from_buckets(deltas)
    assert p99 is not None and 0.25 < p99 <= 0.5
    assert p99_from_buckets({0.1: 0.0, float("inf"): 0.0}) is None


async def test_forwarder_derives_p99_error_rate_pool_and_rps_from_deltas() -> None:
    w, clock, rt = Workload(), FrozenClock(datetime(2026, 9, 26, tzinfo=UTC)), FakeRawTree()
    fwd = _forwarder(w, clock, rt)
    w.traffic(ok=1000, err=0, latency_s=0.004)  # history before the first scrape
    await fwd.scrape_once()
    clock.advance(5)
    w.in_use.labels("checkout").set(72)
    w.traffic(ok=95, err=5, latency_s=0.3)
    rows = await fwd.scrape_once()
    values = {r["metric"]: r["value"] for r in rows}
    assert values[POOL_UTILISATION] == pytest.approx(0.72)
    assert values[ERROR_RATE] == pytest.approx(0.05)  # deltas, not lifetime totals
    assert 250 < values[P99_MS] <= 500
    assert values["rps"] == pytest.approx(20.0)
    assert rt.metrics[-1]["service"] == "checkout"
    assert fwd.latest("checkout")[POOL_UTILISATION] == pytest.approx(0.72)


async def test_scrape_failure_is_unavailable_never_zero() -> None:
    clock, rt = FrozenClock(), FakeRawTree()

    def boom(_r: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    fwd = MetricsForwarder(_settings(), rt, httpx.MockTransport(boom), clock=clock)  # type: ignore[arg-type]
    rows = await fwd.scrape_once()
    assert rows == [] and rt.metrics == []
    assert "checkout" in fwd.unavailable
    assert all(v is None for v in fwd.latest("checkout").values())


def test_counter_reset_after_restart_is_not_negative() -> None:
    w = Workload()
    w.traffic(ok=500, err=0, latency_s=0.01)
    before = parse_metrics(w.text(), "checkout")
    fresh = Workload()
    fresh.traffic(ok=10, err=1, latency_s=0.01)
    after = parse_metrics(fresh.text(), "checkout")
    before.at, after.at = 0.0, 5.0
    out = derive(before, after)
    assert out[ERROR_RATE] == pytest.approx(1 / 11)


# --------------------------------------------------------------------------- #
# heartbeat                                                                    #
# --------------------------------------------------------------------------- #


async def _run(
    w: Workload,
    fwd: MetricsForwarder,
    hb: Heartbeat,
    clock: FrozenClock,
    pool_values: list[float],
) -> list[Anomaly]:
    fired: list[Anomaly] = []
    for value in pool_values:
        w.in_use.labels("checkout").set(value)
        w.traffic(ok=50, err=0, latency_s=0.02)
        await fwd.scrape_once()
        fired.extend(await hb.check_once())
        clock.advance(5)
    return fired


def _heartbeat(
    **kw: Any,
) -> tuple[Workload, FrozenClock, FakeRawTree, MetricsForwarder, Heartbeat, list[Anomaly]]:
    w, clock = Workload(), FrozenClock(datetime(2026, 9, 26, tzinfo=UTC))
    rt = FakeRawTree(**kw)
    fwd = _forwarder(w, clock, rt)
    seen: list[Anomaly] = []

    async def on_anomaly(a: Anomaly) -> None:
        seen.append(a)

    hb = Heartbeat(_settings(), rt, fwd, on_anomaly, clock=clock)  # type: ignore[arg-type]
    return w, clock, rt, fwd, hb, seen


async def test_flat_noise_never_fires() -> None:
    w, clock, _rt, fwd, hb, seen = _heartbeat()
    rng = random.Random(7)
    await _run(w, fwd, hb, clock, [30 + rng.uniform(-3, 3) for _ in range(160)])
    assert seen == []


async def test_leak_ramp_fires_once_with_zscore_source_and_debounces() -> None:
    w, clock, rt, fwd, hb, seen = _heartbeat()
    rng = random.Random(3)
    baseline = [30 + rng.uniform(-2, 2) for _ in range(40)]
    ramp = [30 + 4 * i for i in range(1, 17)]  # 34 -> 94 of 100 slots
    plateau = [95.0] * 10
    fired = await _run(w, fwd, hb, clock, baseline + ramp + plateau)
    assert len(fired) == 1 and seen == fired
    anomaly = fired[0]
    assert anomaly.service == "checkout" and anomaly.metric == POOL_UTILISATION
    assert anomaly.source is Source.ZSCORE and anomaly.z > 3 and anomaly.value >= 0.7
    assert rt.queries == []  # no read key: RawTree never asked

    # Recovery clears it; a second leak opens a new one.
    await _run(w, fwd, hb, clock, [30.0] * 3)
    assert "checkout" not in hb.open
    # Long enough that the first leak has aged out of the 10-minute baseline.
    again = await _run(w, fwd, hb, clock, [30.0] * 130 + [96.0] * 14)
    assert len(again) == 1


async def test_rawtree_rows_are_used_when_read_configured() -> None:
    row = {
        "service": "checkout",
        "metric": POOL_UTILISATION,
        "recent_value": 0.9,
        "baseline_mean": 0.3,
        "baseline_std": 0.01,
        "z": 40.0,
    }
    result = QueryResult(name="anomaly_detect", sql="...", rows=[row], source=Source.RAWTREE)
    _w, _clock, rt, _fwd, hb, seen = _heartbeat(read=True, result=result)
    fired = await hb.check_once()
    assert rt.queries == ["anomaly_detect"]
    assert [a.source for a in fired] == [Source.RAWTREE] and seen == fired
    assert await hb.check_once() == []  # debounced


async def test_rawtree_row_below_absolute_floor_does_not_fire() -> None:
    row = {
        "service": "checkout",
        "metric": POOL_UTILISATION,
        "recent_value": 0.4,
        "baseline_mean": 0.3,
        "baseline_std": 0.001,
        "z": 100.0,
    }
    result = QueryResult(name="anomaly_detect", sql="", rows=[row], source=Source.RAWTREE)
    *_, hb, seen = _heartbeat(read=True, result=result)
    assert await hb.check_once() == [] and seen == []


async def test_rawtree_error_falls_back_to_zscore() -> None:
    result = QueryResult(
        name="anomaly_detect",
        sql="",
        rows=[],
        source=Source.RAWTREE,
        error="ExternalServiceError: 503",
    )
    w, clock, _rt, fwd, hb, _seen = _heartbeat(read=True, result=result)
    fired = await _run(w, fwd, hb, clock, [30.0] * 40 + [95.0] * 14)
    assert hb.last_source is Source.ZSCORE
    assert [a.source for a in fired] == [Source.ZSCORE]


async def test_consumer_failure_does_not_kill_the_heartbeat() -> None:
    row = {
        "service": "checkout",
        "metric": ERROR_RATE,
        "recent_value": 0.3,
        "baseline_mean": 0.0,
        "baseline_std": 0.001,
        "z": 50.0,
    }
    result = QueryResult(name="anomaly_detect", sql="", rows=[row], source=Source.RAWTREE)
    rt = FakeRawTree(read=True, result=result)
    w, clock = Workload(), FrozenClock()

    async def broken(_a: Anomaly) -> None:
        raise RuntimeError("db down")

    hb = Heartbeat(_settings(), rt, _forwarder(w, clock, rt), broken, clock=clock)  # type: ignore[arg-type]
    assert len(await hb.check_once()) == 1


# --------------------------------------------------------------------------- #
# anomaly -> incident                                                          #
# --------------------------------------------------------------------------- #


class FakeConn:
    def __init__(self, existing: str | None) -> None:
        self.existing = existing
        self.executed: list[tuple[str, tuple[Any, ...]]] = []

    async def fetchval(self, query: str, *args: Any) -> Any:
        self.executed.append((query, args))
        return self.existing

    async def fetchrow(self, query: str, *args: Any) -> dict[str, Any]:
        self.executed.append((query, args))
        if "INSERT INTO incidents" in query:
            now = datetime(2026, 9, 26, tzinfo=UTC)
            return {
                "id": args[0],
                "title": args[1],
                "severity": args[2],
                "state": args[3],
                "environment": args[4],
                "workload": args[5],
                "affected_services": [],
                "suspected_origin": None,
                "confidence": 0.0,
                "summary": "",
                "owner": None,
                "correlation_id": args[6],
                "created_at": now,
                "updated_at": now,
                "resolved_at": None,
            }
        return {"id": args[0]}

    async def execute(self, query: str, *args: Any) -> str:
        self.executed.append((query, args))
        return "OK"


class FakeDb:
    def __init__(self, existing: str | None = None) -> None:
        self.conn = FakeConn(existing)
        self.transactions = 0

    @asynccontextmanager
    async def transaction(self):  # type: ignore[no-untyped-def]
        self.transactions += 1
        yield self.conn


_ANOMALY = Anomaly(
    "checkout",
    POOL_UTILISATION,
    0.91,
    0.3,
    0.02,
    30.5,
    Source.ZSCORE,
    datetime(2026, 9, 26, 12, tzinfo=UTC),
)


async def test_anomaly_opens_incident_alert_audit_and_job_in_one_transaction() -> None:
    db = FakeDb()
    incident_id = await open_incident_from_anomaly(db, _ANOMALY)  # type: ignore[arg-type]
    assert incident_id is not None and incident_id.startswith("inc")
    assert db.transactions == 1
    sql = " ".join(q for q, _ in db.conn.executed)
    for fragment in (
        "pg_advisory_xact_lock",
        "INSERT INTO incidents",
        "INSERT INTO incident_alerts",
        "INSERT INTO audit_log",
        "INSERT INTO workflow_jobs",
    ):
        assert fragment in sql
    job_args = next(a for q, a in db.conn.executed if "workflow_jobs" in q)
    assert job_args[1] == incident_id and job_args[2] == "investigate"
    assert job_args[3]["trigger"] == "heartbeat"
    alert_args = next(a for q, a in db.conn.executed if "INSERT INTO incident_alerts" in q)
    assert alert_args[2] == "heartbeat" and alert_args[6] == "checkout"


async def test_anomaly_for_service_with_open_incident_is_deduplicated() -> None:
    db = FakeDb(existing="inc_existing")
    assert await open_incident_from_anomaly(db, _ANOMALY) is None  # type: ignore[arg-type]
    assert not any("INSERT" in q for q, _ in db.conn.executed)


async def test_a_pass_that_raises_an_unexpected_type_does_not_end_detection() -> None:
    # A heartbeat task that dies is indistinguishable from a quiet system, so a
    # RuntimeError (or any type outside the expected set) in one pass must leave the loop running.
    import asyncio

    _w, _clock, rt, _fwd, hb, _seen = _heartbeat(read=True)
    calls = {"n": 0}
    stop = asyncio.Event()

    async def flaky(name: str, params: dict[str, Any]) -> QueryResult:
        calls["n"] += 1
        if calls["n"] >= 3:
            stop.set()
        raise RuntimeError("unexpected failure inside a dependency")

    rt.named_query = flaky  # type: ignore[method-assign]
    hb._settings = _settings(heartbeat_interval_s=0.01)
    await asyncio.wait_for(hb.run(stop), timeout=5.0)

    assert calls["n"] >= 3
    assert hb.last_error is not None and "RuntimeError" in hb.last_error
