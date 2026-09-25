"""Tempo client: TraceQL safety and the CallEdge contract.

``CallEdge`` is consumed by the graph package to build CALLS edges, so its shape
and its derivation are asserted here rather than left to integration testing.
"""

from __future__ import annotations

import dataclasses

import httpx
import pytest

from aegis.core.config import Settings
from aegis.core.errors import SourceUnavailable, ValidationError
from aegis.core.resilience import reset_breakers
from aegis.telemetry.tempo import CallEdge, TempoClient, escape_traceql


@pytest.fixture(autouse=True)
def _clean_breakers():
    reset_breakers()
    yield
    reset_breakers()


def settings() -> Settings:
    return Settings(_env_file=None, tempo_url="http://tempo.test")


def client_with(handler) -> TempoClient:
    client = TempoClient(settings())
    client._client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://tempo.test"
    )
    return client


def span(span_id: str, parent: str | None, name: str, start_ns: int, end_ns: int, error=False):
    return {
        "spanId": span_id,
        "parentSpanId": parent or "",
        "name": name,
        "startTimeUnixNano": str(start_ns),
        "endTimeUnixNano": str(end_ns),
        "status": {"code": "STATUS_CODE_ERROR" if error else "STATUS_CODE_OK", "message": ""},
        "attributes": [{"key": "http.method", "value": {"stringValue": "GET"}}],
    }


def batch(service: str, *spans):
    return {
        "resource": {"attributes": [{"key": "service.name", "value": {"stringValue": service}}]},
        "scopeSpans": [{"spans": list(spans)}],
    }


# --- query safety -----------------------------------------------------------


def test_escape_traceql_strips_selector_terminators():
    assert escape_traceql('gateway"}|{\\') == "gateway"


async def test_service_name_is_bound_as_a_quoted_literal():
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["q"] = request.url.params["q"]
        return httpx.Response(200, json={"traces": []})

    await client_with(handler).search_traces('gateway"} && true', start=1, end=2, limit=5)
    assert captured["q"] == '{ resource.service.name = "gateway && true" }'


async def test_unsafe_tag_key_is_refused():
    def handler(_request: httpx.Request) -> httpx.Response:  # pragma: no cover
        return httpx.Response(200, json={"traces": []})

    with pytest.raises(ValidationError):
        await client_with(handler).search_traces(
            "gateway", start=1, end=2, tags={'x" = "y" && span.z': "1"}
        )


async def test_trace_id_must_be_hex():
    def handler(_request: httpx.Request) -> httpx.Response:  # pragma: no cover
        return httpx.Response(200, json={})

    with pytest.raises(ValidationError):
        await client_with(handler).get_trace("../../etc/passwd")


async def test_outage_is_source_unavailable_not_an_empty_search():
    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    with pytest.raises(SourceUnavailable) as exc:
        await client_with(handler).search_traces("gateway", start=1, end=2)
    assert exc.value.context["dependency"] == "tempo"


async def test_empty_search_is_a_valid_answer():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"traces": []})

    assert await client_with(handler).search_traces("gateway", start=1, end=2) == []


# --- CallEdge ---------------------------------------------------------------


def test_call_edge_shape_is_frozen_and_stable():
    """Other subsystems depend on exactly these four fields."""
    fields = {f.name: f.type for f in dataclasses.fields(CallEdge)}
    assert list(fields) == ["caller_service", "callee_service", "count", "p99_ms"]
    edge = CallEdge(caller_service="a", callee_service="b", count=1, p99_ms=2.0)
    with pytest.raises(dataclasses.FrozenInstanceError):
        edge.count = 2  # type: ignore[misc]


async def test_service_call_edges_derives_cross_service_edges():
    traces = {
        "aa11aa11": [
            batch("gateway", span("s1", None, "GET /work", 0, 30_000_000)),
            batch("checkout", span("s2", "s1", "GET /pay", 1_000_000, 21_000_000)),
            # An internal child of the same service is not a topology edge.
            batch("checkout", span("s3", "s2", "db.query", 2_000_000, 5_000_000)),
        ],
        "bb22bb22": [
            batch("gateway", span("t1", None, "GET /work", 0, 40_000_000)),
            batch("checkout", span("t2", "t1", "GET /pay", 1_000_000, 11_000_000)),
        ],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/search":
            return httpx.Response(200, json={"traces": [{"traceID": t} for t in traces]})
        trace_id = request.url.path.rsplit("/", 1)[-1]
        return httpx.Response(200, json={"batches": traces[trace_id]})

    edges = await client_with(handler).service_call_edges(1.0, 2.0, limit=10)

    assert edges == [
        CallEdge(caller_service="gateway", callee_service="checkout", count=2, p99_ms=20.0)
    ]


async def test_call_edges_use_nearest_rank_p99():
    """p99 must be a value that really occurred, not an interpolation."""
    spans = [span(f"c{i}", "root", "call", 0, (i + 1) * 1_000_000) for i in range(10)]
    payload = {
        "batches": [
            batch("gateway", span("root", None, "in", 0, 50_000_000)),
            batch("checkout", *spans),
        ]
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/search":
            return httpx.Response(200, json={"traces": [{"traceID": "abcd1234"}]})
        return httpx.Response(200, json=payload)

    edges = await client_with(handler).service_call_edges(1.0, 2.0)
    assert edges[0].count == 10
    assert edges[0].p99_ms == 10.0


async def test_get_trace_flattens_and_counts_errors():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "batches": [
                    batch("gateway", span("s1", None, "GET /work", 0, 5_000_000)),
                    batch("payment", span("s2", "s1", "charge", 0, 4_000_000, error=True)),
                ]
            },
        )

    detail = await client_with(handler).get_trace("abcd1234")
    assert detail.services == ("gateway", "payment")
    assert detail.error_count == 1
    assert detail.duration_ms == 5.0
    assert detail.spans[1].attributes["http.method"] == "GET"


async def test_error_spans_filters_to_the_requested_service():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/search":
            return httpx.Response(200, json={"traces": [{"traceID": "abcd1234"}]})
        return httpx.Response(
            200,
            json={
                "batches": [
                    batch("gateway", span("s1", None, "GET /work", 0, 5_000_000, error=True)),
                    batch("payment", span("s2", "s1", "charge", 0, 4_000_000, error=True)),
                ]
            },
        )

    spans = await client_with(handler).error_spans("payment", 1.0, 2.0, limit=5)
    assert [s.service for s in spans] == ["payment"]


async def test_malformed_batch_degrades_to_the_spans_that_parsed():
    """A partial trace is still evidence; raising would look like an outage."""

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "batches": ["not-a-batch", batch("gateway", span("s1", None, "x", 0, 1_000_000))]
            },
        )

    detail = await client_with(handler).get_trace("abcd1234")
    assert len(detail.spans) == 1
