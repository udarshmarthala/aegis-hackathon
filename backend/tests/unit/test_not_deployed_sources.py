"""An empty connection setting means "not deployed", not "down".

The AWS deployment leaves ``NEO4J_URI``, ``REDIS_HOST``, ``PROMETHEUS_URL``,
``TEMPO_URL`` and ``LOKI_URL`` empty for the dependencies it does not run. Each
guarantee below would fail if that empty value were treated as an address:

* nothing reaches the network - every transport here fails the test if touched;
* nothing is retried, so boot does not pay ~15 s of backoff for Neo4j;
* the reason names the missing setting, so an operator can tell the two apart;
* every read still raises ``SourceUnavailable`` (invariant 6) - an undeployed
  source is an evidence gap, never an empty result that reads as "all clear".
"""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from aegis.api.routers import health as health_router
from aegis.api.routers.war_room import HealthProbe
from aegis.container import Container, build_container
from aegis.core.config import Settings
from aegis.core.errors import SourceNotConfigured, SourceUnavailable
from aegis.core.resilience import breaker_states, reset_breakers
from aegis.domain.enums import EvidenceStatus, EvidenceType, SourceType
from aegis.domain.models import EvidenceItem
from aegis.graph.client import Neo4jClient
from aegis.graph.traversal import GraphTraversal
from aegis.mcp import ToolDeps, default_registry
from aegis.mcp.invoker import ToolInvoker
from aegis.mcp.types import SCOPES, CallerIdentity, ToolBudget, ToolContext
from aegis.telemetry.loki import LokiClient
from aegis.telemetry.prometheus import PrometheusClient
from aegis.telemetry.tempo import TempoClient

UNSET = ("", "   ")


def settings(**over: Any) -> Settings:
    base: dict[str, Any] = {
        "_env_file": None,
        "aegis_mode": "scripted",
        "neo4j_uri": "",
        "redis_host": "",
        "prometheus_url": "",
        "tempo_url": "",
        "loki_url": "",
    }
    base.update(over)
    return Settings(**base)


@pytest.fixture(autouse=True)
def _clean_breakers() -> Any:
    reset_breakers()
    yield
    reset_breakers()


@pytest.fixture
def no_network(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Record - and fail on - any attempt to reach a dependency.

    ``pytest.fail`` raises a ``BaseException``, so a client's broad
    ``except Exception`` cannot swallow it; the list is asserted as well in
    case some path does.
    """
    attempts: list[str] = []

    def refuse(what: str) -> Any:
        def _refuse(*_a: Any, **_k: Any) -> Any:
            attempts.append(what)
            pytest.fail(f"{what} was attempted for an undeployed dependency")

        return _refuse

    async def _refuse_send(*_a: Any, **_k: Any) -> Any:
        attempts.append("httpx")
        pytest.fail("an HTTP request was attempted for an undeployed dependency")

    import neo4j
    import redis.asyncio as aioredis

    monkeypatch.setattr(httpx.AsyncClient, "send", _refuse_send)
    monkeypatch.setattr(neo4j.AsyncGraphDatabase, "driver", refuse("neo4j driver"))
    monkeypatch.setattr(aioredis, "from_url", refuse("redis connection"))
    return attempts


def assert_not_deployed(exc: SourceUnavailable, setting: str) -> None:
    assert isinstance(exc, SourceNotConfigured)
    # Still coded SOURCE_UNAVAILABLE, so the UI renders an evidence gap.
    assert exc.code == "SOURCE_UNAVAILABLE"
    assert exc.retryable is False
    assert f"not deployed ({setting} is empty)" in exc.message
    assert exc.context["not_configured"] is True


# --------------------------------------------------------------------------- #
# clients                                                                      #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("unset", UNSET)
async def test_prometheus_reads_raise_not_deployed_without_a_request(
    unset: str, no_network: list[str]
) -> None:
    client = PrometheusClient(settings(prometheus_url=unset))
    assert client.configured is False

    for read in (
        lambda: client.query_range("up", start=0, end=1),
        lambda: client.instant("up"),
        lambda: client.error_rate("payment"),
        lambda: client.known_services(),
    ):
        with pytest.raises(SourceUnavailable) as caught:
            await read()
        assert_not_deployed(caught.value, "PROMETHEUS_URL")

    assert no_network == []
    # Not an outage, so the breaker guarding a real Prometheus never heard of it.
    assert "prometheus" not in breaker_states()


@pytest.mark.parametrize("unset", UNSET)
async def test_tempo_reads_raise_not_deployed_without_a_request(
    unset: str, no_network: list[str]
) -> None:
    client = TempoClient(settings(tempo_url=unset))
    assert client.configured is False

    for read in (
        lambda: client.search_traces("payment", start=0, end=1, limit=5),
        lambda: client.get_trace("0123456789abcdef0123456789abcdef"),
        lambda: client.error_spans("payment", 0, 1),
        lambda: client.service_call_edges(start=0, end=1),
    ):
        with pytest.raises(SourceUnavailable) as caught:
            await read()
        assert_not_deployed(caught.value, "TEMPO_URL")

    assert no_network == []
    assert "tempo" not in breaker_states()


@pytest.mark.parametrize("unset", UNSET)
async def test_loki_reads_raise_not_deployed_without_a_request(
    unset: str, no_network: list[str]
) -> None:
    client = LokiClient(settings(loki_url=unset))
    assert client.configured is False

    for read in (
        lambda: client.error_logs("payment", 0, 1),
        lambda: client.pattern_counts("payment", 0, 1),
    ):
        with pytest.raises(SourceUnavailable) as caught:
            await read()
        assert_not_deployed(caught.value, "LOKI_URL")

    assert no_network == []
    assert "loki" not in breaker_states()


@pytest.mark.parametrize("unset", UNSET)
async def test_neo4j_raises_not_deployed_without_building_a_driver(
    unset: str, no_network: list[str]
) -> None:
    client = Neo4jClient(settings(neo4j_uri=unset))
    assert client.configured is False

    with pytest.raises(SourceUnavailable) as read:
        await client.run("RETURN 1 AS ok", {})
    assert_not_deployed(read.value, "NEO4J_URI")
    with pytest.raises(SourceUnavailable) as write:
        await client.write("MERGE (n:Service {id: $id})", {"id": "x"})
    assert_not_deployed(write.value, "NEO4J_URI")
    assert await client.healthy() is False

    assert client._driver is None
    assert no_network == []
    assert "neo4j" not in breaker_states()


async def test_a_configured_url_still_reaches_the_source() -> None:
    """The guard must not swallow a deployed source: control for the above."""
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(200, json={"status": "success", "data": ["payment"]})

    client = PrometheusClient(settings(prometheus_url="http://prometheus.test"))
    client._client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://prometheus.test"
    )

    assert client.configured is True
    assert await client.known_services() == ["payment"]
    assert calls == ["/api/v1/label/service/values"]


# --------------------------------------------------------------------------- #
# the tool boundary                                                            #
# --------------------------------------------------------------------------- #


NOW = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)


class _FixedClock:
    def now(self) -> datetime:
        return NOW

    def monotonic(self) -> float:
        return 0.0


class _Gaps:
    """Evidence store that records gaps and refuses to record findings."""

    def __init__(self) -> None:
        self.gaps: list[dict[str, str]] = []

    async def record(self, **_: Any) -> EvidenceItem:
        pytest.fail("an undeployed source produced a finding")

    async def record_unavailable(
        self, *, incident_id: str, source: str, source_type: SourceType, reason: str
    ) -> EvidenceItem:
        self.gaps.append({"source": source, "reason": reason})
        return EvidenceItem(
            id=f"ev_gap_{len(self.gaps):04d}",
            incident_id=incident_id,
            source=source,
            source_type=source_type,
            evidence_type=EvidenceType.EVIDENCE_GAP,
            retrieved_at=NOW,
            summary=f"{source} unavailable: {reason}",
            provenance_uri=f"gap://{source}",
            status=EvidenceStatus.SOURCE_UNAVAILABLE,
        )


@pytest.mark.parametrize(
    ("tool", "args", "source", "setting"),
    [
        ("service_error_rate", {"service": "payment"}, "prometheus", "PROMETHEUS_URL"),
        ("search_traces", {"service": "payment"}, "tempo", "TEMPO_URL"),
        ("error_logs", {"service": "payment"}, "loki", "LOKI_URL"),
        ("blast_radius", {"service_id": "local:demo:payment"}, "neo4j", "NEO4J_URI"),
    ],
)
async def test_an_undeployed_source_is_an_evidence_gap_not_an_empty_result(
    tool: str, args: dict[str, Any], source: str, setting: str, no_network: list[str]
) -> None:
    s = settings()
    gaps = _Gaps()
    deps = ToolDeps(
        evidence=gaps,  # type: ignore[arg-type]
        clock=_FixedClock(),  # type: ignore[arg-type]
        prometheus=PrometheusClient(s),
        tempo=TempoClient(s),
        loki=LokiClient(s),
        traversal=GraphTraversal(Neo4jClient(s)),
    )
    invoker = ToolInvoker(default_registry(deps), clock=_FixedClock())  # type: ignore[arg-type]
    context = ToolContext(
        environment="local",
        caller=CallerIdentity(
            subject="agent:evidence_investigator", actor_type="agent", scopes=frozenset(SCOPES)
        ),
        budget=ToolBudget(max_tool_calls=5, max_seconds=60.0, clock=_FixedClock()),
        deadline=NOW + timedelta(seconds=60),
        correlation_id="corr_not_deployed",
        incident_id="inc_NOTDEPLOYED",
    )

    result = await invoker.invoke(tool, args, context)

    assert result.degraded is True
    assert result.found_nothing is False
    assert f"not deployed ({setting} is empty)" in result.degraded_reason
    assert [g["source"] for g in gaps.gaps] == [source]
    assert no_network == []


# --------------------------------------------------------------------------- #
# the composition root                                                         #
# --------------------------------------------------------------------------- #


def test_capabilities_name_the_missing_setting() -> None:
    container = build_container(settings())
    caps = container.capability_report()

    for name, setting in (
        ("prometheus", "PROMETHEUS_URL"),
        ("tempo", "TEMPO_URL"),
        ("loki", "LOKI_URL"),
        ("graph", "NEO4J_URI"),
    ):
        assert caps[name]["configured"] is False, name
        assert caps[name]["reason"] == f"not deployed ({setting} is empty)", name
    # The clients still exist, so a tool call records a reasoned gap.
    assert container.tempo is not None
    assert container.loki is not None
    assert container.topology is not None


def test_deployed_sources_are_reported_configured() -> None:
    container = build_container(
        settings(
            neo4j_uri="bolt://neo4j:7687",
            prometheus_url="http://prometheus:9090",
            tempo_url="http://tempo:3200",
            loki_url="http://loki:3100",
        )
    )
    caps = container.capability_report()

    for name in ("prometheus", "tempo", "loki", "graph"):
        assert caps[name] == {"configured": True, "reason": ""}, name


class _CountingIngestor:
    def __init__(self) -> None:
        self.calls = 0

    async def ensure_schema(self) -> int:
        self.calls += 1
        raise ConnectionError("must not be reached for an undeployed graph")


async def test_graph_schema_is_not_retried_when_neo4j_is_not_deployed() -> None:
    container = Container(settings=settings())
    ingestor = _CountingIngestor()
    container.graph_ingest = ingestor

    started = time.monotonic()
    assert await container.ensure_graph_schema() is False
    elapsed = time.monotonic() - started

    assert ingestor.calls == 0
    # The retry budget is ~15 s; this must cost nothing like it.
    assert elapsed < 0.5
    cap = container.capabilities["graph"]
    assert cap.configured is False
    assert cap.reason == "not deployed (NEO4J_URI is empty)"


async def test_redis_is_not_contacted_when_not_deployed(no_network: list[str]) -> None:
    container = Container(settings=settings())

    await container.connect_redis()

    assert container.redis is None
    cap = container.capabilities["redis"]
    assert cap.configured is False
    assert cap.reason == "not deployed (REDIS_HOST is empty)"
    assert no_network == []


# --------------------------------------------------------------------------- #
# health surfaces                                                              #
# --------------------------------------------------------------------------- #


class _HealthyDb:
    async def healthy(self) -> bool:
        return True


async def test_health_reports_unconfigured_without_probing(no_network: list[str]) -> None:
    payload = await health_router.health(db=_HealthyDb(), settings=settings())  # type: ignore[arg-type]

    for name, setting in (
        ("neo4j", "NEO4J_URI"),
        ("redis", "REDIS_HOST"),
        ("prometheus", "PROMETHEUS_URL"),
        ("tempo", "TEMPO_URL"),
        ("loki", "LOKI_URL"),
    ):
        component = payload["components"][name]
        # Distinct from "unavailable": nothing crashed, nothing was deployed.
        assert component["status"] == "unconfigured", name
        assert component["detail"] == f"not deployed ({setting} is empty)", name
        assert name in payload["degraded_components"]
    assert no_network == []


def _assert_every_card_unavailable(payload: dict[str, Any]) -> None:
    assert payload["services"]
    for card in payload["services"]:
        assert card["source"] == "unavailable"
        # Nulls, never zeros: an undeployed Prometheus is not a healthy one.
        assert card["p99_ms"] is None
        assert card["error_rate"] is None
        assert card["pool_utilisation"] is None
        assert card["status"] == "unknown"


async def test_war_room_health_is_unavailable_with_nulls(no_network: list[str]) -> None:
    probe = HealthProbe(PrometheusClient(settings()), None)

    _assert_every_card_unavailable(await probe.payload())
    assert no_network == []


async def test_war_room_raise_path_also_yields_nulls(no_network: list[str]) -> None:
    """Without the ``configured`` shortcut, the per-query raise path agrees."""
    client = PrometheusClient(settings())
    # No ``configured`` attribute, so every query runs and raises.
    bare = SimpleNamespace(
        latency_p99=client.latency_p99,
        error_rate=client.error_rate,
        pool_saturation=client.pool_saturation,
        query_range=client.query_range,
    )
    probe = HealthProbe(bare, None)

    _assert_every_card_unavailable(await probe.payload())
    assert no_network == []
