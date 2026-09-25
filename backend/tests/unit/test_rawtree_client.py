"""RawTree client: key separation, batching, loss accounting, injection, fallback.

Zero network: every request goes to an ``httpx.MockTransport``.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest

from aegis.core.config import Settings
from aegis.core.errors import ValidationError
from aegis.core.resilience import reset_breakers
from aegis.domain.horizon import (
    HorizonEvent,
    HorizonEventType,
    HorizonPhase,
    MemoryCard,
    Source,
)
from aegis.integrations.rawtree import (
    TABLE_EVENTS,
    TABLE_METRICS,
    RawTreeClient,
    event_row,
    render_named_query,
)

WRITE = "rt_write_TESTKEY_0001"
READ = "rt_read_TESTKEY_0002"
NOW = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _breakers() -> None:
    reset_breakers()


def _settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "rawtree_write_key": WRITE,
        "rawtree_read_key": READ,
        "rawtree_database": "",
        "rawtree_queue_max": 100,
        "rawtree_flush_interval_ms": 10,
        "rawtree_timeout_s": 2.0,
    }
    base.update(overrides)
    return Settings(_env_file=None, **base)  # type: ignore[call-arg]


class Recorder:
    """MockTransport handler that records (method, path, auth, params, body)."""

    def __init__(
        self, *, query_rows: list[dict[str, Any]] | None = None, status: int = 200
    ) -> None:
        self.calls: list[dict[str, Any]] = []
        self.query_rows = query_rows or []
        self.status = status

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        self.calls.append(
            {
                "method": request.method,
                "path": request.url.path,
                "auth": request.headers.get("authorization"),
                "params": dict(request.url.params),
                "body": body,
            }
        )
        if self.status != 200:
            return httpx.Response(self.status, json={"error": "boom"})
        if request.url.path == "/v1/query":
            return httpx.Response(200, json={"meta": [], "data": self.query_rows, "rows": 0})
        return httpx.Response(200, json={"inserted": len(body) if isinstance(body, list) else 1})


def _client(rec: Recorder, **overrides: Any) -> RawTreeClient:
    return RawTreeClient(_settings(**overrides), transport=httpx.MockTransport(rec))


def _event(step: int = 1, **kw: Any) -> HorizonEvent:
    data: dict[str, Any] = {
        "ts": NOW,
        "run_id": "run_1",
        "incident_id": "inc_1",
        "step": step,
        "phase": HorizonPhase.INVESTIGATING,
        "event_type": HorizonEventType.STEP_COMPLETED,
        "context_tokens": 100 * step,
        "naive_tokens": 300 * step,
    }
    data.update(kw)
    return HorizonEvent(**data)


# --------------------------------------------------------------------------- #
# key separation                                                               #
# --------------------------------------------------------------------------- #


async def test_write_key_only_for_inserts_and_read_key_only_for_queries() -> None:
    rec = Recorder()
    client = _client(rec)
    client.enqueue_event(_event())
    client.enqueue_metrics([{"ts": NOW, "service": "checkout", "metric": "p99_ms", "value": 12}])
    await client.flush()
    await client.named_query("incident_timeline", {"incident_id": "inc_1"})
    await client.aclose()

    inserts = [c for c in rec.calls if c["path"].startswith("/v1/tables/")]
    queries = [c for c in rec.calls if c["path"] == "/v1/query"]
    assert inserts and queries
    assert all(c["auth"] == f"Bearer {WRITE}" for c in inserts)
    assert all(c["auth"] == f"Bearer {READ}" for c in queries)
    # The property itself: neither key ever appears on the other path.
    assert not any(READ in (c["auth"] or "") for c in inserts)
    assert not any(WRITE in (c["auth"] or "") for c in queries)


async def test_write_only_configuration_never_queries_with_write_key() -> None:
    rec = Recorder()
    client = _client(rec, rawtree_read_key="")
    result = await client.named_query("incident_timeline", {"incident_id": "inc_1"})
    assert not client.read_configured
    assert [c for c in rec.calls if c["path"] == "/v1/query"] == []
    assert result.source is Source.SYSTEM and result.error and result.rows == []


async def test_database_selected_by_query_parameter() -> None:
    rec = Recorder()
    client = _client(rec, rawtree_database="aegis")
    client.enqueue_event(_event())
    await client.flush()
    await client.named_query("context_tokens", {"run_id": "run_1"})
    assert rec.calls and all(c["params"] == {"database": "aegis"} for c in rec.calls)


async def test_insert_body_is_json_array_to_namespaced_table() -> None:
    rec = Recorder()
    client = _client(rec)
    client.enqueue_event(_event())
    await client.flush()
    call = next(c for c in rec.calls if c["path"] == f"/v1/tables/{TABLE_EVENTS}")
    assert TABLE_EVENTS.startswith("aegis_")
    assert isinstance(call["body"], list) and call["body"][0]["incident_id"] == "inc_1"


# --------------------------------------------------------------------------- #
# batching and loss accounting                                                 #
# --------------------------------------------------------------------------- #


async def test_full_metric_queue_drops_oldest_batch_and_counts_it() -> None:
    rec = Recorder()
    client = _client(rec, rawtree_queue_max=2)
    for value in (1.0, 2.0, 3.0):
        client.enqueue_metrics(
            [{"ts": NOW, "service": "checkout", "metric": "p99_ms", "value": value}] * 2
        )
    assert client.dropped_metric_batches == 1
    assert client.dropped_metric_rows == 2
    await client.flush()
    sent = [
        r["value"]
        for c in rec.calls
        if c["path"] == f"/v1/tables/{TABLE_METRICS}"
        for r in c["body"]
    ]
    assert sent == [2.0, 2.0, 3.0, 3.0]  # the oldest batch is the one that went
    assert client.stats()["dropped_metric_batches"] == 1


async def test_full_event_queue_defers_and_never_drops_silently() -> None:
    rec = Recorder()
    client = _client(rec, rawtree_queue_max=2)
    for step in (1, 2, 3):
        client.enqueue_event(_event(step))
    assert client.deferred[TABLE_EVENTS] == 1
    # Metrics being full must never evict an event, and the reverse.
    assert client.dropped_metric_batches == 0
    stats = client.stats()
    assert stats["deferred"][TABLE_EVENTS] == 1
    assert stats["queue_depths"][TABLE_EVENTS] == 2


async def test_insert_failure_defers_events_and_counts_failed_metrics() -> None:
    rec = Recorder(status=503)
    client = _client(rec)
    client.enqueue_event(_event())
    client.enqueue_observation(incident_id="inc_1", evidence_id="ev_1", tool="t", raw="x", ts=NOW)
    client.enqueue_memory_card(
        MemoryCard(id="m1", incident_id="inc_1", symptoms="s", root_cause="r")
    )
    client.enqueue_metrics([{"ts": NOW, "service": "s", "metric": "m", "value": 1}])
    await client.flush()
    assert sum(client.deferred.values()) == 3
    assert client.failed_metric_rows == 1
    assert client.last_error and "503" in client.last_error
    # One attempt per insert: a retried non-idempotent write duplicates rows.
    assert len([c for c in rec.calls if c["path"] == f"/v1/tables/{TABLE_EVENTS}"]) == 1


async def test_unconfigured_writer_enqueues_nothing_and_says_so() -> None:
    rec = Recorder()
    client = _client(rec, rawtree_write_key="")
    client.enqueue_event(_event())
    client.enqueue_metrics([{"ts": NOW, "service": "s", "metric": "m", "value": 1}])
    await client.start()
    await client.aclose()
    assert rec.calls == []
    assert client.stats()["skipped_unconfigured"] == 2


async def test_start_and_aclose_flush_pending_rows() -> None:
    rec = Recorder()
    client = _client(rec, rawtree_flush_interval_ms=10_000)
    await client.start()
    client.enqueue_event(_event())
    await client.aclose()
    assert client.inserted[TABLE_EVENTS] == 1
    assert client.stats()["running"] is False


async def test_non_finite_metric_values_are_not_shipped() -> None:
    rec = Recorder()
    client = _client(rec)
    client.enqueue_metrics(
        [
            {"ts": NOW, "service": "s", "metric": "m", "value": float("nan")},
            {"ts": NOW, "service": "s", "metric": "m", "value": True},
            {"ts": NOW, "service": "s", "metric": "m", "value": 3},
        ]
    )
    await client.flush()
    body = next(c["body"] for c in rec.calls if c["path"] == f"/v1/tables/{TABLE_METRICS}")
    assert body == [
        {"ts": "2026-09-26 12:00:00.000", "run_id": "", "service": "s", "metric": "m", "value": 3.0}
    ]


def test_verification_event_row_carries_success_rate_columns() -> None:
    row = event_row(
        _event(
            event_type=HorizonEventType.VERIFICATION_RESULT,
            payload={
                "action_type": "restart_instance",
                "symptom": "pool_leak",
                "verified": False,
                "recovery_s": 12,
            },
        )
    )
    assert row["action_type"] == "restart_instance"
    assert row["verified"] == 0 and row["recovery_s"] == 12.0
    assert "payload" not in row


# --------------------------------------------------------------------------- #
# named queries                                                                #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("name", "params"),
    [
        ("incident_timeline", {"incident_id": "inc_1' OR 1=1 --"}),
        ("incident_timeline", {"incident_id": "inc_1'; DROP TABLE x"}),
        ("incident_timeline", {"incident_id": "inc_1", "limit": "5; DROP TABLE x"}),
        ("incident_timeline", {"incident_id": "inc_1", "limit": True}),
        ("incident_timeline", {"incident_id": "inc_1", "limit": 10_000_000}),
        ("context_tokens", {"run_id": "run\\' UNION SELECT 1"}),
        ("context_tokens", {}),
        ("anomaly_detect", {"threshold": float("nan")}),
        ("anomaly_detect", {"threshold": "3 OR 1=1"}),
        ("anomaly_detect", {"service": "checkout' OR '1'='1"}),
        ("action_success_rate", {"symptom": "leak'); DROP TABLE t; --"}),
        ("action_success_rate", {"action_type": "restart instance"}),
    ],
)
async def test_injection_attempts_are_rejected_before_any_request(
    name: str, params: dict[str, Any]
) -> None:
    rec = Recorder()
    client = _client(rec)
    with pytest.raises(ValidationError):
        await client.named_query(name, params)
    assert rec.calls == []


async def test_unknown_named_query_raises() -> None:
    client = _client(Recorder())
    with pytest.raises(ValidationError):
        await client.named_query("drop_everything", {})


def test_rendered_sql_quotes_validated_literals_and_uses_namespaced_tables() -> None:
    sql = render_named_query("incident_timeline", {"incident_id": "inc_01H", "limit": 50})
    assert "= 'inc_01H'" in sql and "LIMIT 50" in sql and f"FROM {TABLE_EVENTS}" in sql
    anomaly = render_named_query("anomaly_detect", {"threshold": 3.5})
    assert f"FROM {TABLE_METRICS}" in anomaly and "z > 3.5" in anomaly
    assert "INTERVAL 60 SECOND" in anomaly and "INTERVAL 660 SECOND" in anomaly


async def test_rawtree_result_is_labelled_with_sql_and_duration() -> None:
    rec = Recorder(query_rows=[{"step": 1, "context_tokens": 10, "naive_tokens": 30}])
    client = _client(rec)
    result = await client.named_query("context_tokens", {"run_id": "run_1"})
    assert result.source is Source.RAWTREE
    assert result.sql == rec.calls[0]["body"]["sql"]
    assert result.rows[0]["context_tokens"] == 10
    assert result.duration_ms >= 0 and result.error is None
    assert client.recent_queries()[-1]["name"] == "context_tokens"


# --------------------------------------------------------------------------- #
# fallbacks                                                                    #
# --------------------------------------------------------------------------- #


class FakeStore:
    def __init__(self, events: list[HorizonEvent]) -> None:
        self._events = list(enumerate(events, start=1))

    async def events(
        self, incident_id: str, *, after_seq: int = 0, limit: int = 500
    ) -> list[tuple[int, HorizonEvent]]:
        return [(s, e) for s, e in self._events if e.incident_id == incident_id and s > after_seq][
            :limit
        ]


class FakeDb:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
        self.calls.append((query, args))
        return [
            {
                "action_type": "rollback_deployment",
                "symptom": "*",
                "attempts": 3,
                "verified_successes": 3,
                "mean_recovery_s": 41.5,
            }
        ]


async def test_timeline_and_tokens_fall_back_to_postgres_store_when_unconfigured() -> None:
    store = FakeStore([_event(1), _event(2), _event(3, incident_id="inc_other")])
    client = RawTreeClient(_settings(rawtree_read_key=""), fallback_store=store)  # type: ignore[arg-type]
    timeline = await client.named_query("incident_timeline", {"incident_id": "inc_1"})
    assert timeline.source is Source.POSTGRES and [r["step"] for r in timeline.rows] == [1, 2]
    tokens = await client.named_query("context_tokens", {"incident_id": "inc_1", "run_id": "run_1"})
    assert tokens.source is Source.POSTGRES
    assert tokens.rows == [
        {"step": 1, "context_tokens": 100, "naive_tokens": 300},
        {"step": 2, "context_tokens": 200, "naive_tokens": 600},
    ]


async def test_rawtree_error_falls_back_and_is_counted() -> None:
    rec = Recorder(status=500)
    db = FakeDb()
    client = RawTreeClient(_settings(), db=db, transport=httpx.MockTransport(rec))  # type: ignore[arg-type]
    result = await client.named_query("action_success_rate", {"action_type": "rollback_deployment"})
    assert result.source is Source.POSTGRES
    assert result.rows[0]["verified_successes"] == 3
    assert db.calls[0][1] == (30, "rollback_deployment")  # bound, never interpolated
    assert client.query_failures == 1 and client.fallbacks_served == 1


async def test_anomaly_detect_failure_reports_error_for_heartbeat_fallback() -> None:
    client = _client(Recorder(status=500))
    result = await client.named_query("anomaly_detect", {})
    assert result.rows == [] and result.error is not None


def test_stats_never_contain_keys() -> None:
    client = _client(Recorder())
    client.last_error = "something"
    text = json.dumps(client.stats())
    assert WRITE not in text and READ not in text
