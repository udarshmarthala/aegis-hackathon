"""The metric catalogue: what it asks Prometheus, and what "no data" means.

Two properties are defended here, and both were broken in ways that looked fine.

**Zero is not absent.** PromQL binary operators return an empty vector when one
side has no series, so `sum(5xx) / sum(all)` produced *no data* for a service
with no errors at all. A perfectly healthy service therefore reported an
evidence gap instead of a zero error rate - the "found nothing" versus "could
not look" confusion, living inside a query string where no amount of reading the
Python would reveal it.

**Absent is not zero either.** A service with no cache emits no cache series,
and defaulting that to 0.0 would claim a hit ratio of zero - "every lookup
missed" - about a service that simply does not cache. That one stays absent.

The line between those two is drawn per metric and on purpose, so it is pinned
per metric here rather than asserted once in general.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import parse_qs, unquote_plus, urlparse

import httpx
import pytest
import respx

from aegis.core.config import Settings
from aegis.core.resilience import reset_breakers
from aegis.mcp.tools.telemetry import MetricName
from aegis.telemetry.prometheus import PrometheusClient

PROM = "http://prometheus.test:9090"

# Every template on the client, and therefore everything the closed catalogue
# may name. Kept as one list so a new template cannot be added without deciding
# which side of the zero/absent line it falls on.
ALL_METRICS: tuple[str, ...] = (
    "error_rate",
    "latency_p99",
    "request_rate",
    "cpu_utilisation",
    "memory_bytes",
    "pool_saturation",
    "queue_depth",
    "cache_hit_ratio",
    "restart_count",
)


def settings(**over: Any) -> Settings:
    base: dict[str, Any] = {"prometheus_url": PROM, "google_api_key": ""}
    base.update(over)
    return Settings(**base)


def series_response(value: float) -> dict[str, Any]:
    return {
        "status": "success",
        "data": {
            "resultType": "matrix",
            "result": [
                {
                    "metric": {"__name__": "synthetic"},
                    "values": [[1_700_000_000, str(value)]],
                }
            ],
        },
    }


EMPTY_RESPONSE: dict[str, Any] = {
    "status": "success",
    "data": {"resultType": "matrix", "result": []},
}


@pytest.fixture(autouse=True)
def _clean_breakers() -> None:
    reset_breakers()


def sent_query(route: Any) -> str:
    """The PromQL a template actually sent, decoded.

    The client sends the query in the URL, so it arrives percent-encoded. An
    assertion against the raw URL silently matches nothing, which would make
    every check here pass for the wrong reason.
    """
    request = route.calls[0].request
    if request.content:
        raw = parse_qs(request.content.decode()).get("query", [""])[0]
    else:
        raw = parse_qs(urlparse(str(request.url)).query).get("query", [""])[0]
    return unquote_plus(raw)


# --------------------------------------------------------------------------- #
# the catalogue is closed and complete                                         #
# --------------------------------------------------------------------------- #


def test_every_catalogue_name_has_a_template() -> None:
    """A name the tool boundary offers but the client cannot answer is a crash.

    The dispatch resolves by attribute, so a catalogue entry with no matching
    method fails at call time rather than at import - the worst moment to find
    out, mid-investigation.
    """
    catalogue = set(MetricName.__args__)  # type: ignore[attr-defined]
    assert catalogue == set(ALL_METRICS)

    client = PrometheusClient(settings())
    for name in ALL_METRICS:
        assert callable(getattr(client, name)), name


# --------------------------------------------------------------------------- #
# zero is not absent                                                           #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("metric", ["error_rate", "queue_depth", "pool_saturation"])
async def test_a_metric_that_can_be_zero_defaults_to_zero(metric: str) -> None:
    """These three read 0 when the thing they count has not happened.

    A service with no 5xx, no queue and an idle pool is healthy, and must report
    that it is healthy - not that nothing could be measured. Prometheus is told
    so with ``or vector(0)`` on the side that can legitimately have no series.
    """
    client = PrometheusClient(settings())
    async with respx.mock(assert_all_called=True) as mock:
        route = mock.route(method__in=["GET", "POST"], host="prometheus.test").mock(
            return_value=httpx.Response(200, json=series_response(0.0))
        )
        result = await getattr(client, metric)("checkout", 300)
        await client.close()

    assert result, f"{metric} returned nothing for a healthy service"
    assert result[0].points[-1].value == 0.0
    assert "or vector(0)" in sent_query(route), (
        f"{metric} has no empty-vector default, so a service with no series "
        "reports an evidence gap instead of a zero measurement"
    )


async def test_cache_hit_ratio_stays_absent_when_there_is_no_cache() -> None:
    """Absent is a different claim from zero, and this one must stay absent.

    Defaulting here would report "every lookup missed" about a service that does
    not cache at all - an alarming finding invented out of nothing.
    """
    client = PrometheusClient(settings())
    async with respx.mock(assert_all_called=True) as mock:
        route = mock.route(method__in=["GET", "POST"], host="prometheus.test").mock(
            return_value=httpx.Response(200, json=EMPTY_RESPONSE)
        )
        result = await client.cache_hit_ratio("checkout", 300)
        await client.close()

    assert result == []
    query = sent_query(route)
    assert "cache_hits_total" in query
    # The misses side may default; the hits side must not, or an absent cache
    # would resolve to 0 / 0.001 = 0 and become a finding.
    assert query.count("or vector(0)") == 1


# --------------------------------------------------------------------------- #
# queries are templated, never caller-supplied                                 #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("metric", ALL_METRICS)
async def test_a_service_name_cannot_escape_its_label_matcher(metric: str) -> None:
    """A service name is data.

    It must not be able to close a label matcher and append its own selector -
    the PromQL equivalent of an injection, and the reason no caller is ever
    allowed to supply raw PromQL.
    """
    client = PrometheusClient(settings())
    hostile = 'checkout"} or up{job="'

    async with respx.mock(assert_all_called=True) as mock:
        route = mock.route(method__in=["GET", "POST"], host="prometheus.test").mock(
            return_value=httpx.Response(200, json=EMPTY_RESPONSE)
        )
        await getattr(client, metric)(hostile, 300)
        await client.close()

    query = sent_query(route)
    # The guarantee is that the name stays *inside* one matcher, not that its
    # characters vanish. `escape_label` strips quotes and backslashes, so the
    # hostile value survives as literal text within `service="..."` and cannot
    # close the matcher to start a new selector. Asserting that the substring is
    # absent would be testing the wrong thing - and would pass for a template
    # that dropped the label entirely.
    assert 'checkout"' not in query, f"{metric} left a quote in the label value"
    assert query.count('"') % 2 == 0, (
        f"{metric} produced an unbalanced quote, so the matcher is not closed "
        "where the template intended"
    )


@pytest.mark.parametrize("metric", ALL_METRICS)
async def test_no_series_is_an_empty_list_not_an_invented_value(metric: str) -> None:
    """Every template returns [] when Prometheus knows nothing.

    Never a zero-filled placeholder: downstream, an empty list becomes a
    recorded evidence gap, and a fabricated zero becomes a finding.
    """
    client = PrometheusClient(settings())
    async with respx.mock(assert_all_called=True) as mock:
        mock.route(method__in=["GET", "POST"], host="prometheus.test").mock(
            return_value=httpx.Response(200, json=EMPTY_RESPONSE)
        )
        result = await getattr(client, metric)("checkout", 300)
        await client.close()

    assert result == []
