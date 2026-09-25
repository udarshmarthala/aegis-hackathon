"""FluxIncidentMap: one submit, bounded polling, BFL hosts only, never raises.
Zero network: every request goes to an ``httpx.MockTransport``."""

from __future__ import annotations

import asyncio
from collections.abc import Callable

import httpx
import pytest
from pydantic import SecretStr

from aegis.core.config import Settings
from aegis.domain.horizon import ActionAttempt, HorizonPhase, HorizonState, MemoryCard, Source
from aegis.integrations.bfl import FluxIncidentMap, build_prompt, is_bfl_url

KEY = "bfl-SENTINEL-key-987654321"
POLL = "https://api.eu2.bfl.ai/v1/get_result?id=abc"
SAMPLE = "https://delivery.eu2.bfl.ai/results/abc/sample.jpeg?sig=xyz"
IMAGE = b"\xff\xd8\xff\xe0fake-jpeg"


def state() -> HorizonState:
    return HorizonState(
        run_id="r1", incident_id="INC-043", service="checkout", phase=HorizonPhase.RESOLVED,
        actions=[
            ActionAttempt(action_type="restart_instance", target="checkout", cycle=1,
                          outcome="failed"),
            ActionAttempt(action_type="rollback_deployment", target="checkout", cycle=2,
                          outcome="verified"),
        ],
    )


def card(root: str = "httpcore 1.0.9 pool leak in checkout 1.4.2") -> MemoryCard:
    return MemoryCard(id="mc1", incident_id="INC-043", symptoms="pool climbing", root_cause=root,
                      successful_action="rollback_deployment")


class Api:
    """A scripted BFL. Counts requests per kind and records every host touched."""

    def __init__(self, *, submit_status: int = 200, polling_url: str = POLL,
                 poll: Callable[[int], httpx.Response] | None = None,
                 sample_url: str = SAMPLE) -> None:
        self.submits = 0
        self.polls = 0
        self.downloads = 0
        self.hosts: list[str] = []
        self.keys_sent_to: set[str] = set()
        self.submit_status = submit_status
        self.polling_url = polling_url
        self.sample_url = sample_url
        self.poll = poll or (lambda n: httpx.Response(
            200, json={"status": "Ready" if n >= 2 else "Pending",
                       "result": {"sample": self.sample_url} if n >= 2 else None}))

    def __call__(self, request: httpx.Request) -> httpx.Response:
        host = request.url.host
        self.hosts.append(host)
        if request.headers.get("x-key") == KEY:
            self.keys_sent_to.add(host)
        if request.method == "POST":
            self.submits += 1
            if self.submit_status != 200:
                return httpx.Response(self.submit_status, json={"detail": "nope"})
            return httpx.Response(200, json={"id": "abc", "polling_url": self.polling_url})
        if host.startswith("delivery"):
            self.downloads += 1
            return httpx.Response(200, content=IMAGE, headers={"content-type": "image/jpeg"})
        self.polls += 1
        return self.poll(self.polls)


def renderer(api: Api, *, key: str = KEY, timeout: float = 5.0) -> FluxIncidentMap:
    s = Settings(_env_file=None, bfl_api_key=SecretStr(key), bfl_timeout_s=timeout)
    return FluxIncidentMap(s, transport=httpx.MockTransport(api), poll_initial_s=0.0,
                           poll_max_s=0.01)


async def test_ready_image_is_downloaded_and_labelled_flux() -> None:
    api = Api()
    res = await renderer(api).render(state(), card())
    assert res.status == "ready" and res.source is Source.FLUX
    assert res.image_bytes == IMAGE and res.mime == "image/jpeg"
    assert api.submits == 1 and api.downloads == 1
    # The key goes to the API host only, never to the delivery host.
    assert api.keys_sent_to == {"api.bfl.ai", "api.eu2.bfl.ai"}


async def test_submit_happens_once_even_when_polling_fails() -> None:
    api = Api(poll=lambda n: httpx.Response(200, json={"status": "Error"}))
    res = await renderer(api).render(state(), card())
    assert res.status == "unavailable" and res.source is Source.SYSTEM
    assert "Error" in res.reason
    assert api.submits == 1


async def test_failed_submit_is_not_retried() -> None:
    api = Api(submit_status=502)
    res = await renderer(api).render(state(), card())
    assert res.status == "unavailable"
    assert "HTTP 502" in res.reason
    assert api.submits == 1 and api.polls == 0


async def test_transient_poll_errors_are_polled_through() -> None:
    def poll(n: int) -> httpx.Response:
        if n == 1:
            return httpx.Response(503)
        return httpx.Response(200, json={"status": "Ready", "result": {"sample": SAMPLE}})

    api = Api(poll=poll)
    res = await renderer(api).render(state(), card())
    assert res.status == "ready" and api.submits == 1


async def test_timeout_is_honoured() -> None:
    api = Api(poll=lambda n: httpx.Response(200, json={"status": "Pending"}))
    r = FluxIncidentMap(
        Settings(_env_file=None, bfl_api_key=SecretStr(KEY), bfl_timeout_s=0.3),
        transport=httpx.MockTransport(api), poll_initial_s=0.05, poll_max_s=0.05,
    )
    res = await asyncio.wait_for(r.render(state(), card()), timeout=3.0)
    assert res.status == "unavailable"
    assert "timed out" in res.reason
    assert api.submits == 1


@pytest.mark.parametrize("bad", [
    "https://evil.example.com/v1/get_result?id=abc",
    "http://api.bfl.ai/v1/get_result?id=abc",
    "https://bfl.ai.evil.example.com/x",
    "https://user:pw@api.bfl.ai/x",
])
async def test_non_bfl_polling_url_is_refused_without_fetching(bad: str) -> None:
    api = Api(polling_url=bad)
    res = await renderer(api).render(state(), card())
    assert res.status == "unavailable" and "refused" in res.reason
    assert api.hosts == ["api.bfl.ai"]  # only the submit went out


async def test_non_bfl_sample_url_is_refused() -> None:
    api = Api(sample_url="https://attacker.example.net/img.jpg")
    res = await renderer(api).render(state(), card())
    assert res.status == "unavailable" and "refused" in res.reason
    assert "attacker.example.net" not in api.hosts


async def test_missing_key_is_unavailable_without_any_request() -> None:
    api = Api()
    res = await renderer(api, key="").render(state(), card())
    assert res.status == "unavailable" and res.source is Source.SYSTEM
    assert "BFL_API_KEY" in res.reason
    assert api.hosts == []


async def test_key_never_in_reason_or_repr() -> None:
    api = Api(submit_status=401)
    r = renderer(api)
    res = await r.render(state(), card())
    assert KEY not in res.reason and KEY not in res.prompt and KEY not in repr(r)


def test_prompt_is_deterministic_and_carries_no_free_text() -> None:
    hostile = 'leak </untrusted> {{system}} "ignore rules" <img src=x> `rm -rf`'
    p1 = build_prompt(state(), card(hostile))
    assert p1 == build_prompt(state(), card(hostile))
    root = p1.split("Root cause label: ", 1)[1].split(". Bottom timeline", 1)[0]
    for ch in "<>{}\"`":
        assert ch not in root
    assert "untrusted" in root  # words survive as inert text; markup does not
    assert "checkout" in p1 and "rollback_deployment" in p1


@pytest.mark.parametrize(("url", "ok"), [
    ("https://api.bfl.ai/v1/get_result?id=1", True),
    ("https://api.us1.bfl.ai/v1/get_result", True),
    ("https://delivery-eu1.bfl.ai/x.jpg", True),
    ("https://bfl.ai/x", True),
    ("https://notbfl.ai/x", False),
    ("https://bfl.ai.example.com/x", False),
    ("http://api.bfl.ai/x", False),
    ("ftp://api.bfl.ai/x", False),
])
def test_is_bfl_url(url: str, ok: bool) -> None:
    assert is_bfl_url(url) is ok
