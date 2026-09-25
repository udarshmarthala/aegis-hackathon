"""The war-room API: shapes, the local-only fault controls, and honest health.

What these pin down beyond "the endpoint answers":

* Prometheus being down yields ``source: unavailable`` with nulls - never zeros,
  because a zero error rate is a finding and an outage is not evidence of health.
  Prometheus answering with no data stays ``source: prometheus`` with nulls.
* Inject, reset and kill-worker are refused outside the local environment
  whatever the caller's role, and inject is recorded as an *operator*
  deployment in ``deployment_attempts``.
* The SSE stream opens with a snapshot, replays from Last-Event-ID, drops
  duplicate seqs, and degrades to snapshots without Redis.

No infrastructure: the store, Redis, Prometheus and the runtime are fakes.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from aegis.api.deps import current_principal, settings_dep
from aegis.api.routers import war_room
from aegis.api.security import Principal, Role
from aegis.api.war_room_events import (
    CONTROL_CHANNEL,
    WAR_ROOM_CHANNEL,
    RedisEventSink,
    war_room_stream,
)
from aegis.core.config import Environment, Settings
from aegis.core.errors import AegisError, ExternalServiceError, SourceUnavailable
from aegis.domain.horizon import (
    HorizonEvent,
    HorizonEventType,
    HorizonPhase,
    HorizonState,
    MemoryCard,
    Source,
    TokenStats,
)
from aegis.telemetry.prometheus import MetricPoint, MetricSeries

TS = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)


# --------------------------------------------------------------------------- #
# fakes                                                                        #
# --------------------------------------------------------------------------- #


def _event(
    seq_step: int,
    event_type: HorizonEventType = HorizonEventType.STEP_COMPLETED,
    payload: dict[str, Any] | None = None,
    incident_id: str = "inc_1",
) -> HorizonEvent:
    return HorizonEvent(
        ts=TS,
        run_id="run_1",
        incident_id=incident_id,
        step=seq_step,
        phase=HorizonPhase.INVESTIGATING,
        event_type=event_type,
        source=Source.RAWTREE if event_type is HorizonEventType.RAWTREE_QUERY else Source.SYSTEM,
        duration_ms=12,
        payload=payload or {},
    )


class FakeStore:
    def __init__(self) -> None:
        self.run: HorizonState | None = HorizonState(
            run_id="run_1",
            incident_id="inc_1",
            service="checkout",
            step=3,
            phase=HorizonPhase.DIAGNOSING,
            tokens=TokenStats(
                context_tokens=1800, naive_tokens=9000, compacted_raw_tokens=4000,
                compacted_card_tokens=400, cache_hits=2, cache_read_tokens=1500,
                fallbacks_used=1,
            ),
        )
        self.events_log: list[tuple[int, HorizonEvent]] = [
            (1, _event(1)),
            (2, _event(2, HorizonEventType.RAWTREE_QUERY, {
                "name": "anomaly_detect", "sql": "SELECT 1", "rows": [{"a": 1}, {"a": 2}],
                "source": "rawtree", "duration_ms": 40, "error": None,
            })),
            (3, _event(3)),
            (4, _event(1, incident_id="inc_other")),
        ]
        self.cards = [MemoryCard(id="mem_1", incident_id="INC-042", symptoms="pool",
                                 root_cause="leak")]
        self.maps: dict[str, tuple[bytes, str]] = {"mem_1": (b"\xff\xd8jpeg", "image/jpeg")}
        self.idled: list[str] = []

    async def latest_run(self) -> HorizonState | None:
        return self.run

    async def mark_idle(self, run_id: str) -> bool:
        self.idled.append(run_id)
        return True

    async def context_series(self, incident_id: str) -> list[dict[str, int]]:
        return [{"step": 1, "context_tokens": 1700, "naive_tokens": 2000}] if incident_id else []

    async def events(
        self, incident_id: str, *, after_seq: int = 0, limit: int = 500
    ) -> list[tuple[int, HorizonEvent]]:
        rows = [
            (s, e) for s, e in self.events_log if e.incident_id == incident_id and s > after_seq
        ]
        return rows[:limit]

    async def events_of_type(
        self, incident_id: str, event_type: HorizonEventType, *, limit: int = 100
    ) -> list[tuple[int, HorizonEvent]]:
        return [(s, e) for s, e in self.events_log
                if e.incident_id == incident_id and e.event_type is event_type][-limit:]

    async def last_seq(self, incident_id: str | None = None) -> int:
        return max((s for s, _ in self.events_log), default=0)

    async def memory_cards(self, *, limit: int = 20) -> list[MemoryCard]:
        return self.cards[:limit]

    async def get_incident_map(self, card_id: str) -> tuple[bytes, str] | None:
        return self.maps.get(card_id)


def _series(*values: float) -> list[MetricSeries]:
    return [MetricSeries(metric="m", labels={}, query="q",
                         points=[MetricPoint(float(i), v) for i, v in enumerate(values)])]


class FakePrometheus:
    def __init__(self, *, fail: bool = False, empty: bool = False) -> None:
        self.fail = fail
        self.empty = empty
        self.calls = 0

    async def _answer(self, *values: float) -> list[MetricSeries]:
        self.calls += 1
        if self.fail:
            raise SourceUnavailable("prometheus unavailable: ConnectError")
        return [] if self.empty else _series(*values)

    async def latency_p99(self, service: str, window_s: int = 900) -> list[MetricSeries]:
        return await self._answer(0.1, 0.9 if service == "checkout" else 0.12)

    async def error_rate(self, service: str, window_s: int = 900) -> list[MetricSeries]:
        return await self._answer(0.0, 0.05 if service == "checkout" else 0.0)

    async def pool_saturation(self, service: str, window_s: int = 900) -> list[MetricSeries]:
        return await self._answer(0.2, 0.9 if service == "checkout" else 0.1)

    async def query_range(self, promql: str, *, start: float, end: float,
                          step: float = 15.0) -> list[MetricSeries]:
        assert "connection_pool_in_use" in promql
        return await self._answer(0.3, 0.9)


class FakeAdapter:
    available = True
    unavailable_reason = ""

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.deploys: list[tuple[str, str, str]] = []

    async def get_service(self, service_id: str) -> Any:
        return SimpleNamespace(version="1.4.1")

    async def list_services(self) -> list[Any]:
        return [SimpleNamespace(ref=SimpleNamespace(name="checkout"), version="1.4.2")]

    async def rollback_deployment(self, service_id: str, to_version: str, *,
                                  idempotency_key: str) -> Any:
        if self.fail:
            raise ExternalServiceError("docker daemon unreachable")
        self.deploys.append((service_id, to_version, idempotency_key))
        return SimpleNamespace(no_op=False, performed="POST /containers/create",
                               detail="replaced a->b")


class FakeDeployments:
    def __init__(self) -> None:
        self.started: list[dict[str, Any]] = []
        self.finished: list[dict[str, Any]] = []

    async def start(self, **kwargs: Any) -> Any:
        self.started.append(kwargs)
        return SimpleNamespace(id=f"dep_{len(self.started)}")

    async def finish(self, deployment_id: str, **kwargs: Any) -> Any:
        self.finished.append({"id": deployment_id, **kwargs})
        return SimpleNamespace(id=deployment_id)


class FakeIncidents:
    async def get(self, incident_id: str) -> Any:
        return SimpleNamespace(id=incident_id, title="checkout p99 regression",
                               severity="P2", state="DIAGNOSING")


class FakeWorkloadAdmin:
    def __init__(self) -> None:
        self.cleared: list[str] = []

    async def clear_fault(self, service: str) -> bool:
        self.cleared.append(service)
        return True


class FakePubSub:
    def __init__(self, messages: list[dict[str, Any]]) -> None:
        self.messages = messages
        self.subscribed: list[str] = []
        self.closed = False

    async def subscribe(self, channel: str) -> None:
        self.subscribed.append(channel)

    async def unsubscribe(self, channel: str) -> None:
        self.subscribed.remove(channel)

    async def aclose(self) -> None:
        self.closed = True

    async def get_message(
        self, *, ignore_subscribe_messages: bool, timeout: float  # noqa: ASYNC109 - redis-py's signature
    ) -> Any:
        if self.messages:
            return self.messages.pop(0)
        await asyncio.sleep(0.01)
        return None


class FakeRedis:
    def __init__(self, messages: list[dict[str, Any]] | None = None, *,
                 receivers: int = 1, fail: bool = False) -> None:
        self.published: list[tuple[str, str]] = []
        self.receivers = receivers
        self.fail = fail
        self.pubsub_obj = FakePubSub(messages or [])

    async def publish(self, channel: str, body: str) -> int:
        if self.fail:
            raise ConnectionError("redis down")
        self.published.append((channel, body))
        return self.receivers

    def pubsub(self) -> FakePubSub:
        return self.pubsub_obj


def _settings(env: Environment = Environment.LOCAL) -> Settings:
    # model_copy rather than a constructor argument: production settings refuse
    # to build without real secrets, and the refusal is not what is under test.
    base = Settings(_env_file=None)
    return base.model_copy(update={"aegis_env": env})


def build_app(
    *,
    role: Role = Role.ADMIN,
    env: Environment = Environment.LOCAL,
    store: FakeStore | None = None,
    prometheus: FakePrometheus | None = None,
    adapter: FakeAdapter | None = None,
    redis: FakeRedis | None = None,
    with_store: bool = True,
) -> tuple[FastAPI, SimpleNamespace]:
    app = FastAPI()
    app.include_router(war_room.router, prefix="/v1")

    @app.exception_handler(AegisError)
    async def _typed(request: Request, exc: AegisError) -> JSONResponse:
        del request
        return JSONResponse(status_code=exc.http_status, content={"error": exc.to_dict()})

    async def _principal() -> Principal:
        return Principal(uid="uid_op", email="op@example.com", roles=frozenset({role.value}))

    settings = _settings(env)
    app.dependency_overrides[current_principal] = _principal
    app.dependency_overrides[settings_dep] = lambda: settings
    container = SimpleNamespace(
        prometheus=prometheus or FakePrometheus(),
        runtime_adapter=adapter or FakeAdapter(),
        deployments=FakeDeployments(),
        incidents=FakeIncidents(),
    )
    app.state.container = container
    if with_store:
        app.state.horizon_store = store or FakeStore()
    app.state.redis = redis
    app.state.workload_admin = FakeWorkloadAdmin()
    return app, container


# --------------------------------------------------------------------------- #
# reads                                                                        #
# --------------------------------------------------------------------------- #


def test_state_has_the_contract_shape_and_no_secret_values() -> None:
    app, _ = build_app()
    with TestClient(app) as client:
        body = client.get("/v1/war-room/state").json()

    assert set(body) == {"mode", "run", "incident", "brain", "integrations",
                         "memory_cards", "stats", "last_seq"}
    assert body["mode"] in ("live", "scripted")
    assert body["run"]["run_id"] == "run_1"
    assert body["incident"] == {"id": "inc_1", "title": "checkout p99 regression",
                                "severity": "P2", "state": "DIAGNOSING", "service": "checkout"}
    assert set(body["integrations"]) == {"bedrock", "gemini", "rawtree", "nimble", "flux",
                                         "prometheus"}
    for entry in body["integrations"].values():
        assert set(entry) == {"configured", "reason"}
    assert body["stats"]["compression_ratio"] == 10.0
    assert body["stats"]["steps"] == 3
    assert body["stats"]["fallbacks_used"] == 1
    assert body["memory_cards"][0]["incident_id"] == "INC-042"
    assert body["last_seq"] == 4
    assert body["brain"]["available"] is False


def test_state_with_no_run_is_explicitly_empty() -> None:
    store = FakeStore()
    store.run = None
    app, _ = build_app(store=store)
    with TestClient(app) as client:
        body = client.get("/v1/war-room/state").json()
    assert body["run"] is None and body["incident"] is None
    assert body["stats"]["steps"] == 0


def test_missing_store_is_a_typed_503_not_an_empty_state() -> None:
    app, _ = build_app(with_store=False)
    with TestClient(app) as client:
        response = client.get("/v1/war-room/state")
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "SOURCE_UNAVAILABLE"


def test_events_are_scoped_ordered_and_paged() -> None:
    app, _ = build_app()
    with TestClient(app) as client:
        default = client.get("/v1/war-room/events").json()["events"]
        after = client.get("/v1/war-room/events?incident_id=inc_1&after=1&limit=1").json()

    assert [e["seq"] for e in default] == [1, 2, 3]  # latest run's incident only
    assert default[0]["event"]["event_type"] == "step_completed"
    assert [e["seq"] for e in after["events"]] == [2]


def test_context_series_shape() -> None:
    app, _ = build_app()
    with TestClient(app) as client:
        body = client.get("/v1/war-room/context-series?incident_id=inc_1").json()
    assert body == {"points": [{"step": 1, "context_tokens": 1700, "naive_tokens": 2000}]}


def test_rawtree_queries_shape() -> None:
    app, _ = build_app()
    with TestClient(app) as client:
        queries = client.get("/v1/war-room/rawtree-queries?incident_id=inc_1").json()["queries"]
    assert queries == [{
        "ts": TS.isoformat(), "name": "anomaly_detect", "sql": "SELECT 1", "rows": 2,
        "source": "rawtree", "duration_ms": 40, "error": None,
    }]


def test_incident_map_serves_stored_bytes_and_404s_otherwise() -> None:
    app, _ = build_app()
    with TestClient(app) as client:
        found = client.get("/v1/war-room/incident-maps/mem_1")
        missing = client.get("/v1/war-room/incident-maps/mem_none")
    assert found.status_code == 200
    assert found.headers["content-type"] == "image/jpeg"
    assert found.headers["x-content-type-options"] == "nosniff"
    assert found.content == b"\xff\xd8jpeg"
    assert missing.status_code == 404


# --------------------------------------------------------------------------- #
# health                                                                       #
# --------------------------------------------------------------------------- #


def test_health_reports_measured_values_with_their_source() -> None:
    app, _ = build_app()
    with TestClient(app) as client:
        body = client.get("/v1/war-room/health").json()

    by_name = {s["service"]: s for s in body["services"]}
    assert list(by_name) == ["gateway", "checkout", "payment", "db-pool"]
    checkout = by_name["checkout"]
    assert checkout["source"] == "prometheus"
    assert checkout["p99_ms"] == 900.0  # seconds scaled to milliseconds
    assert checkout["error_rate"] == 0.05
    assert checkout["status"] == "degraded"
    assert checkout["version"] == "1.4.2"
    assert checkout["series"]["p99_ms"] == [100.0, 900.0]
    assert by_name["gateway"]["status"] == "healthy"
    pool = by_name["db-pool"]
    assert pool["pool_utilisation"] == 0.9 and pool["p99_ms"] is None
    assert pool["status"] == "degraded"


def test_prometheus_down_is_unavailable_with_nulls_never_zeros() -> None:
    app, _ = build_app(prometheus=FakePrometheus(fail=True))
    with TestClient(app) as client:
        body = client.get("/v1/war-room/health").json()

    for card in body["services"]:
        assert card["source"] == "unavailable"
        assert card["status"] == "unknown"
        assert card["p99_ms"] is None
        assert card["error_rate"] is None
        assert card["pool_utilisation"] is None
        assert card["series"] == {"p99_ms": [], "error_rate": [], "pool_utilisation": []}


def test_no_data_is_not_the_same_as_unavailable() -> None:
    app, _ = build_app(prometheus=FakePrometheus(empty=True))
    with TestClient(app) as client:
        body = client.get("/v1/war-room/health").json()
    gateway = body["services"][0]
    assert gateway["source"] == "prometheus"
    assert gateway["p99_ms"] is None and gateway["status"] == "unknown"


def test_health_is_cached_across_clients() -> None:
    prom = FakePrometheus()
    app, _ = build_app(prometheus=prom)
    with TestClient(app) as client:
        client.get("/v1/war-room/health")
        first = prom.calls
        client.get("/v1/war-room/health")
    assert prom.calls == first


# --------------------------------------------------------------------------- #
# demo controls                                                                #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("path", ["/v1/war-room/inject", "/v1/war-room/reset",
                                  "/v1/war-room/kill-worker"])
@pytest.mark.parametrize("env", [Environment.STAGING, Environment.PRODUCTION])
def test_controls_are_refused_outside_local_even_for_admin(path: str, env: Environment) -> None:
    adapter = FakeAdapter()
    redis = FakeRedis()
    app, container = build_app(env=env, adapter=adapter, redis=redis)
    with TestClient(app) as client:
        response = client.post(path, json={"scenario": "INC-043"})
    assert response.status_code == 403
    assert adapter.deploys == []
    assert container.deployments.started == []
    assert redis.published == []


def test_inject_requires_an_operator_role() -> None:
    adapter = FakeAdapter()
    app, _ = build_app(role=Role.VIEWER, adapter=adapter)
    with TestClient(app) as client:
        response = client.post("/v1/war-room/inject", json={"scenario": "INC-043"})
    assert response.status_code == 403
    assert adapter.deploys == []


def test_inject_rejects_unknown_scenarios() -> None:
    app, _ = build_app()
    with TestClient(app) as client:
        response = client.post("/v1/war-room/inject", json={"scenario": "rm -rf"})
    assert response.status_code == 422


def test_inject_deploys_the_bad_build_as_a_recorded_operator_action() -> None:
    adapter = FakeAdapter()
    app, container = build_app(adapter=adapter)
    with TestClient(app) as client:
        body = client.post("/v1/war-room/inject", json={"scenario": "INC-043"}).json()

    assert body["ok"] is True
    assert body["deployment_id"] == "dep_1"
    assert "fault injection" in body["detail"]
    assert adapter.deploys[0][:2] == ("checkout", "1.4.2")
    started = container.deployments.started[0]
    assert started["to_version"] == "1.4.2" and started["from_version"] == "1.4.1"
    assert started["service_id"].endswith(":checkout")
    assert started["detail"]["actor"] == "operator:uid_op"
    assert started["detail"]["kind"] == "deploy"
    assert container.deployments.finished[0]["state"].value == "DEPLOYED"


def test_inject_failure_closes_the_attempt_as_failed() -> None:
    app, container = build_app(adapter=FakeAdapter(fail=True))
    with TestClient(app) as client:
        body = client.post("/v1/war-room/inject", json={"scenario": "INC-043"}).json()
    assert body["ok"] is False
    assert body["deployment_id"] == "dep_1"
    assert container.deployments.finished[0]["state"].value == "FAILED"


def test_reset_restores_the_build_clears_faults_and_idles_the_run() -> None:
    adapter = FakeAdapter()
    store = FakeStore()
    app, container = build_app(adapter=adapter, store=store)
    with TestClient(app) as client:
        body = client.post("/v1/war-room/reset").json()

    assert body["ok"] is True
    assert adapter.deploys[0][:2] == ("checkout", "1.4.1")
    assert container.deployments.started[0]["detail"]["kind"] == "reset"
    assert sorted(app.state.workload_admin.cleared) == ["checkout", "gateway", "payment"]
    assert store.idled == ["run_1"]


def test_kill_worker_publishes_crash_on_the_control_channel() -> None:
    redis = FakeRedis()
    app, _ = build_app(redis=redis)
    with TestClient(app) as client:
        body = client.post("/v1/war-room/kill-worker").json()
    assert body["ok"] is True
    assert redis.published == [(CONTROL_CHANNEL, json.dumps({"command": "crash"}))]


def test_kill_worker_without_a_listener_says_so() -> None:
    app, _ = build_app(redis=FakeRedis(receivers=0))
    with TestClient(app) as client:
        body = client.post("/v1/war-room/kill-worker").json()
    assert body["ok"] is False and "no worker" in body["detail"]


def test_kill_worker_without_redis_is_not_ok() -> None:
    app, _ = build_app(redis=None)
    with TestClient(app) as client:
        body = client.post("/v1/war-room/kill-worker").json()
    assert body == {"ok": False, "detail": "redis unavailable; no control channel"}


# --------------------------------------------------------------------------- #
# the event sink and the SSE generator                                         #
# --------------------------------------------------------------------------- #


async def test_redis_sink_publishes_the_wire_shape() -> None:
    redis = FakeRedis()
    await RedisEventSink(redis).publish(7, _event(2))
    channel, body = redis.published[0]
    assert channel == WAR_ROOM_CHANNEL
    decoded = json.loads(body)
    assert decoded["seq"] == 7
    assert decoded["event"]["event_type"] == "step_completed"


async def test_redis_sink_never_raises() -> None:
    await RedisEventSink(FakeRedis(fail=True)).publish(1, _event(1))
    await RedisEventSink(None).publish(1, _event(1))


def _disconnect_after(n: int) -> Any:
    calls = {"n": 0}

    async def _check() -> bool:
        calls["n"] += 1
        return calls["n"] > n

    return _check


async def _collect(gen: Any, limit: int = 50) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    async for item in gen:
        out.append(item)
        if len(out) >= limit:
            break
    await gen.aclose()
    return out


async def _snapshot() -> dict[str, Any]:
    return {"last_seq": 3, "run": None}


async def _health() -> dict[str, Any]:
    return {"ts": "t", "services": []}


def _replay_from(events: list[int]) -> Any:
    async def _replay(after: int, limit: int) -> list[dict[str, Any]]:
        return [{"seq": s, "event": {"step": s}} for s in events if s > after][:limit]

    return _replay


async def test_stream_opens_with_a_snapshot_then_live_events_deduplicated() -> None:
    live = [
        {"type": "message", "data": json.dumps({"seq": 2, "event": {}})},  # already in snapshot
        {"type": "message", "data": json.dumps({"seq": 4, "event": {"step": 4}})},
        {"type": "message", "data": "not json"},
        {"type": "message", "data": json.dumps({"seq": 4, "event": {"step": 4}})},  # duplicate
    ]
    redis = FakeRedis(live)
    frames = await _collect(war_room_stream(
        snapshot=_snapshot, health=_health, replay=_replay_from([]), redis=redis,
        is_disconnected=_disconnect_after(40), health_interval_s=60,
    ))
    assert frames[0]["event"] == "snapshot" and frames[0]["id"] == "3"
    assert frames[1]["event"] == "health" and "id" not in frames[1]
    horizon = [f for f in frames if f["event"] == "horizon"]
    assert [f["id"] for f in horizon] == ["4"]
    # The subscription is released when the stream ends.
    assert redis.pubsub_obj.subscribed == [] and redis.pubsub_obj.closed


async def test_stream_replays_after_last_event_id() -> None:
    frames = await _collect(war_room_stream(
        snapshot=_snapshot, health=_health, replay=_replay_from([1, 2, 3]), redis=FakeRedis(),
        is_disconnected=_disconnect_after(0), last_event_id=1,
    ))
    assert frames[0]["event"] == "snapshot"
    assert [f["id"] for f in frames if f["event"] == "horizon"] == ["2", "3"]


async def test_stream_without_redis_degrades_to_periodic_snapshots() -> None:
    frames = await _collect(war_room_stream(
        snapshot=_snapshot, health=_health, replay=_replay_from([5]), redis=None,
        is_disconnected=_disconnect_after(6), snapshot_interval_s=0.0, health_interval_s=60,
    ))
    names = [f["event"] for f in frames]
    assert names[0] == "snapshot"
    assert names.count("snapshot") >= 2
    assert "horizon" in names  # polled from the record, not from Redis
