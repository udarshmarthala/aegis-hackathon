"""Embedding client: configuration reporting, batching, key failover, failure typing.

Two behaviours are load-bearing here.

The first keeps degraded retrieval honest: an unconfigured or unreachable
provider raises, it never returns a placeholder vector. A zero vector would be
equidistant from every query and would make semantic search silently useless
while still appearing to work.

The second keeps failover honest. Ingestion is the heaviest consumer of a
free-tier quota, so the client must tell apart a fault another key would survive
(429, rejected credential) from one every key shares (a malformed request). The
tests below would fail if either half of that distinction were removed.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
import respx

from aegis.core.clock import FrozenClock
from aegis.core.config import Settings
from aegis.core.errors import ConfigError, SourceUnavailable
from aegis.core.resilience import reset_breakers
from aegis.retrieval.embeddings import MAX_BATCH_SIZE, EmbeddingClient

GOOGLE_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    "gemini-embedding-001:batchEmbedContents"
)

# Synthetic. Shaped like a Google AI Studio key so that a regression which
# leaked one into a log or an error context would be visible in the assertion.
KEY1 = "AIzaTESTKEY-1"
KEY2 = "AIzaTESTKEY-2"
KEY3 = "AIzaTESTKEY-3"
KEY4 = "AIzaTESTKEY-4"


def settings(**over: Any) -> Settings:
    base: dict[str, Any] = {
        "google_api_key": "",
        "google_api_key_2": "",
        "google_api_key_3": "",
        "google_api_key_4": "",
        # Pinned rather than inherited: Settings reads .env, and a unit test
        # whose mocked URL depends on the developer's local model choice fails
        # for a reason that has nothing to do with the behaviour under test.
        "llm_embedding_model": "gemini-embedding-001",
        "llm_embedding_dim": 4,
        "llm_max_retries": 0,
        "llm_request_timeout_s": 2.0,
    }
    base.update(over)
    return Settings(**base)


def four_keys(**over: Any) -> Settings:
    return settings(
        google_api_key=KEY1,
        google_api_key_2=KEY2,
        google_api_key_3=KEY3,
        google_api_key_4=KEY4,
        **over,
    )


def google_body(count: int, dim: int = 4) -> dict[str, Any]:
    return {"embeddings": [{"values": [float(i)] * dim} for i in range(count)]}


@pytest.fixture(autouse=True)
def _clean_breakers() -> None:
    reset_breakers()


# --------------------------------------------------------------------------- #
# configuration                                                                #
# --------------------------------------------------------------------------- #


async def test_unconfigured_client_reports_itself_and_refuses_to_guess() -> None:
    client = EmbeddingClient(settings())
    assert client.configured is False
    assert client.provider is None
    with pytest.raises(SourceUnavailable) as exc:
        await client.embed(["anything"])
    assert exc.value.code == "SOURCE_UNAVAILABLE"
    # Names configuration, so the degraded reason cannot be mistaken for an outage.
    assert exc.value.context["setting"] == "GOOGLE_API_KEY"


async def test_one_key_is_enough_and_order_is_preserved() -> None:
    client = EmbeddingClient(settings(google_api_key=KEY1))
    assert client.configured is True
    assert client.provider == "google"

    async with respx.mock(assert_all_called=True) as mock:
        route = mock.post(GOOGLE_URL).mock(
            return_value=httpx.Response(200, json=google_body(3))
        )
        vectors = await client.embed(["a", "b", "c"])
        await client.aclose()

    assert [v[0] for v in vectors] == [0.0, 1.0, 2.0]
    assert route.calls[0].request.headers["x-goog-api-key"] == KEY1


async def test_the_key_travels_in_a_header_never_in_the_url() -> None:
    """A key in a query string leaks through every proxy and access log."""
    client = EmbeddingClient(settings(google_api_key=KEY1))

    async with respx.mock(assert_all_called=True) as mock:
        route = mock.post(GOOGLE_URL).mock(
            return_value=httpx.Response(
                200, json={"embeddings": [{"values": [0.1, 0.2, 0.3, 0.4]}]}
            )
        )
        vectors = await client.embed_one("pool exhausted")
        await client.aclose()

    request = route.calls[0].request
    assert request.headers["x-goog-api-key"] == KEY1
    assert KEY1 not in str(request.url)
    assert len(vectors) == 4


async def test_output_dimensionality_is_requested_explicitly() -> None:
    """gemini-embedding-001 returns 3072 by default; the columns are vector(1536).

    Without this field every insert would fail on a type error whose message
    names Postgres rather than the request that caused it.
    """
    import json

    client = EmbeddingClient(settings(google_api_key=KEY1))
    async with respx.mock(assert_all_called=True) as mock:
        route = mock.post(GOOGLE_URL).mock(
            return_value=httpx.Response(200, json=google_body(1))
        )
        await client.embed(["a"])
        await client.aclose()

    sent = json.loads(route.calls[0].request.content)
    assert sent["requests"][0]["outputDimensionality"] == 4


# --------------------------------------------------------------------------- #
# batching and validation                                                      #
# --------------------------------------------------------------------------- #


async def test_batching_splits_oversized_input() -> None:
    client = EmbeddingClient(settings(google_api_key=KEY1))
    total = MAX_BATCH_SIZE + 5

    async with respx.mock(assert_all_called=True) as mock:
        route = mock.post(GOOGLE_URL)
        route.side_effect = [
            httpx.Response(200, json=google_body(MAX_BATCH_SIZE)),
            httpx.Response(200, json=google_body(5)),
        ]
        vectors = await client.embed([f"text-{i}" for i in range(total)])
        await client.aclose()

    assert len(vectors) == total
    assert route.call_count == 2


async def test_a_dimension_mismatch_is_a_config_error_not_a_stored_row() -> None:
    """Catching it here names the real cause instead of a Postgres type error."""
    client = EmbeddingClient(settings(google_api_key=KEY1))
    async with respx.mock(assert_all_called=True) as mock:
        mock.post(GOOGLE_URL).mock(
            return_value=httpx.Response(200, json=google_body(1, dim=1536))
        )
        with pytest.raises(ConfigError) as exc:
            await client.embed(["a"])
        await client.aclose()
    assert exc.value.context["expected_dim"] == 4
    assert exc.value.context["returned_dim"] == 1536


async def test_embedding_no_texts_is_a_no_op_not_a_call() -> None:
    client = EmbeddingClient(settings(google_api_key=KEY1))
    async with respx.mock(assert_all_called=False) as mock:
        route = mock.post(GOOGLE_URL)
        assert await client.embed([]) == []
        assert route.call_count == 0


# --------------------------------------------------------------------------- #
# failure typing                                                               #
# --------------------------------------------------------------------------- #


async def test_a_server_error_becomes_source_unavailable() -> None:
    client = EmbeddingClient(settings(google_api_key=KEY1))
    async with respx.mock(assert_all_called=True) as mock:
        mock.post(GOOGLE_URL).mock(
            return_value=httpx.Response(503, json={"error": "down"})
        )
        with pytest.raises(SourceUnavailable) as exc:
            await client.embed(["a"])
        await client.aclose()
    assert exc.value.context["provider"] == "google"


async def test_a_transport_failure_becomes_source_unavailable() -> None:
    client = EmbeddingClient(settings(google_api_key=KEY1))
    async with respx.mock(assert_all_called=True) as mock:
        mock.post(GOOGLE_URL).mock(side_effect=httpx.ConnectError("refused"))
        with pytest.raises(SourceUnavailable):
            await client.embed(["a"])
        await client.aclose()


async def test_no_key_material_reaches_the_error_context() -> None:
    """The failure report must be safe to log, ship and paste into a ticket."""
    client = EmbeddingClient(four_keys())
    async with respx.mock(assert_all_called=True) as mock:
        mock.post(GOOGLE_URL).mock(
            return_value=httpx.Response(429, json={"error": "quota"})
        )
        with pytest.raises(SourceUnavailable) as exc:
            await client.embed(["a"])
        await client.aclose()

    rendered = f"{exc.value.message} {exc.value.context}"
    for key in (KEY1, KEY2, KEY3, KEY4):
        assert key not in rendered
    assert exc.value.context["keys_tried"] == ["key1", "key2", "key3", "key4"]


# --------------------------------------------------------------------------- #
# key failover                                                                 #
# --------------------------------------------------------------------------- #


async def test_a_quota_exhausted_key_fails_over_to_the_next() -> None:
    """429 is the key's problem, not the request's. The batch must still land."""
    client = EmbeddingClient(four_keys())

    async with respx.mock(assert_all_called=True) as mock:
        route = mock.post(GOOGLE_URL)
        route.side_effect = [
            httpx.Response(429, json={"error": "quota"}),
            httpx.Response(200, json=google_body(1)),
        ]
        vectors = await client.embed(["a"])
        await client.aclose()

    assert len(vectors) == 1
    assert route.call_count == 2
    assert route.calls[0].request.headers["x-goog-api-key"] == KEY1
    assert route.calls[1].request.headers["x-goog-api-key"] == KEY2


async def test_a_rejected_credential_fails_over_and_parks_that_key() -> None:
    clock = FrozenClock()
    client = EmbeddingClient(four_keys(), clock)

    async with respx.mock(assert_all_called=True) as mock:
        route = mock.post(GOOGLE_URL)
        route.side_effect = [
            httpx.Response(403, json={"error": "invalid key"}),
            httpx.Response(200, json=google_body(1)),
        ]
        await client.embed(["a"])
        await client.aclose()

    assert route.call_count == 2
    status = {row["key"]: row for row in client.key_status()}
    assert status["key1"]["state"] == "parked"
    assert status["key1"]["last_fault"] == "auth"
    assert status["key2"]["state"] == "ready"


async def test_a_parked_key_is_skipped_until_its_window_expires() -> None:
    clock = FrozenClock()
    client = EmbeddingClient(four_keys(), clock)

    async with respx.mock(assert_all_called=True) as mock:
        route = mock.post(GOOGLE_URL)
        route.side_effect = [
            httpx.Response(429, json={"error": "quota"}),
            httpx.Response(200, json=google_body(1)),
            # Second call: key1 is still parked, so key2 is tried first.
            httpx.Response(200, json=google_body(1)),
        ]
        await client.embed(["a"])
        await client.embed(["b"])
        await client.aclose()

    assert route.call_count == 3
    assert route.calls[2].request.headers["x-goog-api-key"] == KEY2

    # Once the quota window passes the key returns on its own; a permanently
    # parked key would silently shrink a four-key ring to a three-key ring.
    clock.advance(61.0)
    assert client.key_status()[0]["state"] == "ready"


async def test_a_malformed_request_does_not_burn_the_whole_ring() -> None:
    """A 400 is identical on every key.

    Retrying it three more times would report an outage where the cause is a bad
    model id or an oversized input.
    """
    client = EmbeddingClient(four_keys())

    async with respx.mock(assert_all_called=True) as mock:
        route = mock.post(GOOGLE_URL).mock(
            return_value=httpx.Response(400, json={"error": "bad request"})
        )
        with pytest.raises(SourceUnavailable) as exc:
            await client.embed(["a"])
        await client.aclose()

    assert route.call_count == 1
    assert exc.value.context["keys_tried"] == ["key1"]
    # And the healthy keys stay healthy - the fault was never theirs.
    assert all(row["state"] == "ready" for row in client.key_status())
