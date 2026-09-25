"""Loki client: query construction, injection refusal and pattern normalisation.

The LogQL builder is a security boundary - a service name can arrive from an
alert payload - so these tests attack it rather than exercising it.
"""

from __future__ import annotations

import httpx
import pytest

from aegis.core.config import Settings
from aegis.core.errors import SourceUnavailable, ValidationError
from aegis.core.resilience import reset_breakers
from aegis.domain.models import UntrustedText
from aegis.telemetry.loki import (
    LogSelector,
    LokiClient,
    build_logql,
    escape_label,
    normalise_line,
)


@pytest.fixture(autouse=True)
def _clean_breakers():
    """Breaker state is process-wide; a failing test must not fail the next one."""
    reset_breakers()
    yield
    reset_breakers()


def settings() -> Settings:
    # _env_file=None so a developer's real .env cannot change what is asserted.
    return Settings(_env_file=None, loki_url="http://loki.test")


def client_with(handler) -> LokiClient:
    client = LokiClient(settings())
    client._client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://loki.test"
    )
    return client


def stream(*lines: tuple[str, str]) -> dict:
    return {
        "status": "success",
        "data": {
            "resultType": "streams",
            "result": [
                {
                    "stream": {"service": "checkout", "level": "error"},
                    "values": [list(pair) for pair in lines],
                }
            ],
        },
    }


# --- query construction -----------------------------------------------------


def test_build_logql_renders_a_single_selector():
    assert build_logql(LogSelector(service="checkout")) == '{service="checkout"}'


def test_build_logql_includes_level_and_extra_labels():
    query = build_logql(
        LogSelector(service="checkout", level="error", extra_labels={"pod": "checkout-1"})
    )
    assert query == '{service="checkout", level="error", pod="checkout-1"}'


@pytest.mark.parametrize(
    "hostile",
    [
        'checkout"} |= "password',
        "checkout\"} or {service=~\".+\"}",
        "checkout\n{service=\"payment\"}",
        "checkout`whoami`",
        "checkout|gateway",
    ],
)
def test_label_injection_is_refused_not_escaped(hostile: str):
    """A label value that is not label-shaped is an attack, so it is rejected."""
    with pytest.raises(ValidationError):
        build_logql(LogSelector(service=hostile))


def test_level_injection_is_refused():
    with pytest.raises(ValidationError):
        build_logql(LogSelector(service="checkout", level='error"} |= "'))


def test_unsafe_label_name_is_refused():
    with pytest.raises(ValidationError):
        build_logql(LogSelector(service="checkout", extra_labels={'pod"} |= "x': "1"}))


def test_substring_is_escaped_and_cannot_add_a_second_filter():
    """Free-text search is escaped rather than refused, but it cannot break out."""
    query = build_logql(LogSelector(service="checkout", contains='boom" } |= "secret'))
    assert query.count("|=") == 1
    assert query.startswith('{service="checkout"} |= "')
    assert query.count('"') == 4
    assert "}" not in query.split("|=")[1]


def test_substring_that_sanitises_to_nothing_is_refused():
    with pytest.raises(ValidationError):
        build_logql(LogSelector(service="checkout", contains='"""'))


def test_escape_label_strips_every_terminator():
    assert escape_label('a"b{c}d|e`f\\g\nh') == "abcdefgh"


# --- pattern normalisation --------------------------------------------------


def test_variable_ids_collapse_to_one_pattern():
    assert normalise_line("user 123 not found") == normalise_line("user 456 not found")
    assert normalise_line("user 123 not found") == "user <num> not found"


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("trace 3f2a9b8c7d6e5f40 failed", "trace <hex> failed"),
        ("id 6f1e2d3c-4b5a-6978-8a9b-0c1d2e3f4a5b gone", "id <uuid> gone"),
        ("peer 10.0.3.14:8080 reset", "peer <ip> reset"),
        ("2026-09-20T10:11:12.345Z started", "<ts> started"),
        ("offset 0xdeadbeef bad", "offset <hex> bad"),
        ("incident inc_01J8Z3ABCDEFGHJKMNPQRSTVWX closed", "incident <id> closed"),
        ("took 250ms overall", "took <num> overall"),
    ],
)
def test_normaliser_collapses_each_variable_token(line: str, expected: str):
    assert normalise_line(line) == expected


def test_normalisation_is_deterministic_and_whitespace_stable():
    assert normalise_line("  slow   query   took 12 ms  ") == normalise_line(
        "slow query took 99 ms"
    )


def test_distinct_messages_stay_distinct():
    assert normalise_line("cache miss for key 1") != normalise_line("cache hit for key 1")


# --- client behaviour -------------------------------------------------------


async def test_query_range_wraps_bodies_as_untrusted():
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["query"] = request.url.params["query"]
        return httpx.Response(200, json=stream(("1700000000000000000", "boom happened")))

    lines = await client_with(handler).query_range(
        LogSelector(service="checkout"), start=1.0, end=2.0, limit=10
    )

    assert captured["query"] == '{service="checkout"}'
    assert len(lines) == 1
    assert isinstance(lines[0].line, UntrustedText)
    assert lines[0].line.text == "boom happened"
    # The envelope is what stops a log line being read as an instruction.
    assert "<untrusted origin=\"log\">" in lines[0].line.as_prompt_block()
    assert lines[0].service == "checkout"


async def test_empty_result_is_an_answer_not_an_outage():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "success", "data": {"result": []}})

    assert await client_with(handler).query_range(
        LogSelector(service="checkout"), start=1.0, end=2.0
    ) == []


async def test_transport_failure_is_source_unavailable():
    """'We could not look' must never be representable as 'we found nothing'."""

    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    with pytest.raises(SourceUnavailable) as exc:
        await client_with(handler).query_range(LogSelector(service="checkout"), start=1.0, end=2.0)
    assert exc.value.context["dependency"] == "loki"


async def test_http_error_status_is_source_unavailable():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="unavailable")

    with pytest.raises(SourceUnavailable):
        await client_with(handler).query_range(LogSelector(service="checkout"), start=1.0, end=2.0)


async def test_error_logs_uses_the_constant_regex_filter():
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["query"] = request.url.params["query"]
        return httpx.Response(200, json=stream(("1700000000000000000", "ERROR boom")))

    await client_with(handler).error_logs("checkout", 1.0, 2.0, limit=5)
    assert captured["query"].startswith('{service="checkout"} |~ "')
    assert "error" in captured["query"]


async def test_pattern_counts_groups_similar_lines():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=stream(
                ("1700000000000000000", "user 1 not found"),
                ("1700000001000000000", "user 22 not found"),
                ("1700000002000000000", "user 333 not found"),
                ("1700000003000000000", "cache warmed"),
            ),
        )

    patterns = await client_with(handler).pattern_counts("checkout", 1.0, 2.0)

    assert [(p.pattern, p.count) for p in patterns] == [
        ("user <num> not found", 3),
        ("cache warmed", 1),
    ]
    assert isinstance(patterns[0].sample, UntrustedText)
    assert patterns[0].first_seen_s < patterns[0].last_seen_s


async def test_limit_must_be_positive():
    def handler(_request: httpx.Request) -> httpx.Response:  # pragma: no cover - never called
        return httpx.Response(200, json=stream())

    with pytest.raises(ValidationError):
        await client_with(handler).query_range(
            LogSelector(service="checkout"), start=1.0, end=2.0, limit=0
        )
