"""RemoteMCPClient: the allowlist is the catalogue, every await is bounded, the
bearer stays in the header. Zero network: sessions are injected fakes."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import mcp_types as t
import pytest

from aegis.agents.horizon.ports import ToolSpec
from aegis.core.errors import ExternalServiceError
from aegis.mcp.remote import (
    RawSession,
    RemoteCallResult,
    RemoteMCPClient,
    ToolNotAllowed,
    call_result_from_sdk,
)

BEARER = "nimble-SENTINEL-bearer-0123456789"
ALLOW = frozenset({"nimble_search", "nimble_extract"})
ADVERTISED = [
    "nimble_search", "nimble_extract", "nimble_crawl_run", "nimble_agents_create",
    "nimble_agents_delete", "nimble_map",
]


class FakeSession:
    def __init__(self, *, delay: float = 0.0, fail: Exception | None = None) -> None:
        self.calls: list[str] = []
        self.delay = delay
        self.fail = fail

    async def list_tools(self) -> list[ToolSpec]:
        return [ToolSpec(n, f"{n} tool", {"type": "object"}) for n in ADVERTISED]

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> RemoteCallResult:
        self.calls.append(name)
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.fail:
            raise self.fail
        return RemoteCallResult(text=f"ok {name}", structured={"n": 1}, is_error=False)


class Factory:
    def __init__(self, session: FakeSession, *, connect_error: Exception | None = None,
                 connect_delay: float = 0.0) -> None:
        self.session = session
        self.opens = 0
        self.connect_error = connect_error
        self.connect_delay = connect_delay

    def __call__(self) -> Any:
        @asynccontextmanager
        async def cm() -> AsyncIterator[RawSession]:
            self.opens += 1
            if self.connect_delay:
                await asyncio.sleep(self.connect_delay)
            if self.connect_error:
                raise self.connect_error
            yield self.session

        return cm()


def client(factory: Factory, *, timeout_s: float = 2.0, bearer: str = BEARER) -> RemoteMCPClient:
    return RemoteMCPClient(
        "https://mcp.example.test/mcp", bearer, allowlist=ALLOW, timeout_s=timeout_s,
        name="nimble", session_factory=factory,
    )


def assert_no_bearer(exc: BaseException) -> None:
    assert BEARER not in str(exc)
    assert BEARER not in repr(exc)
    if isinstance(exc, ExternalServiceError):
        assert BEARER not in repr(exc.to_dict())
    # Raised ``from None``: no chained cause can carry the secret into a traceback.
    assert exc.__cause__ is None
    assert exc.__suppress_context__


async def test_list_tools_returns_only_allowlisted_names() -> None:
    f = Factory(FakeSession())
    names = {s.name for s in await client(f).list_tools()}
    assert names == ALLOW
    assert "nimble_crawl_run" not in names and "nimble_agents_create" not in names


async def test_list_tools_is_cached() -> None:
    f = Factory(FakeSession())
    c = client(f)
    await c.list_tools()
    await c.list_tools()
    assert f.opens == 1


@pytest.mark.parametrize("tool", ["nimble_crawl_run", "nimble_agents_delete", "anything"])
async def test_call_tool_refuses_non_allowlisted_before_any_io(tool: str) -> None:
    session = FakeSession()
    f = Factory(session)
    with pytest.raises(ToolNotAllowed) as info:
        await client(f).call_tool(tool, {})
    assert f.opens == 0  # no connection was ever opened
    assert session.calls == []
    assert info.value.retryable is False
    assert info.value.code == "REMOTE_TOOL_NOT_ALLOWED"


async def test_session_call_refuses_non_allowlisted_even_when_server_offers_it() -> None:
    session = FakeSession()
    async with client(Factory(session)).session() as s:
        with pytest.raises(ToolNotAllowed):
            await s.call_tool("nimble_agents_create", {"name": "x"})
    assert session.calls == []


async def test_allowlisted_call_returns_result() -> None:
    res = await client(Factory(FakeSession())).call_tool("nimble_search", {"query": "q"})
    assert res.text == "ok nimble_search" and res.structured == {"n": 1} and not res.is_error


async def test_slow_call_times_out_as_external_service_error() -> None:
    f = Factory(FakeSession(delay=5.0))
    with pytest.raises(ExternalServiceError) as info:
        await client(f, timeout_s=0.05).call_tool("nimble_search", {})
    assert_no_bearer(info.value)


async def test_slow_connect_times_out() -> None:
    f = Factory(FakeSession(), connect_delay=5.0)
    with pytest.raises(ExternalServiceError):
        await client(f, timeout_s=0.05).list_tools()


async def test_transport_error_is_typed_and_scrubbed_of_bearer() -> None:
    boom = RuntimeError(f"401 for Authorization: Bearer {BEARER}")
    f = Factory(FakeSession(), connect_error=boom)
    with pytest.raises(ExternalServiceError) as info:
        await client(f).list_tools()
    assert_no_bearer(info.value)
    assert "***" in str(info.value)


async def test_tool_call_failure_is_scrubbed_and_not_retryable() -> None:
    f = Factory(FakeSession(fail=ConnectionError(f"reset while sending {BEARER}")))
    with pytest.raises(ExternalServiceError) as info:
        await client(f).call_tool("nimble_extract", {"url": "https://x"})
    assert_no_bearer(info.value)
    assert info.value.retryable is False


async def test_exception_group_is_unwrapped_to_its_cause() -> None:
    group = ExceptionGroup("tg", [OSError(f"refused {BEARER}")])
    f = Factory(FakeSession(), connect_error=group)
    with pytest.raises(ExternalServiceError) as info:
        await client(f).list_tools()
    assert "OSError" in str(info.value)
    assert_no_bearer(info.value)


async def test_unconfigured_client_fails_closed_without_io() -> None:
    f = Factory(FakeSession())
    with pytest.raises(ExternalServiceError):
        await client(f, bearer="").list_tools()
    assert f.opens == 0


def test_repr_never_contains_bearer() -> None:
    c = client(Factory(FakeSession()))
    assert BEARER not in repr(c) and BEARER not in str(c)


def test_sdk_result_flattening() -> None:
    result = t.CallToolResult(
        content=[
            t.TextContent(type="text", text="first"),
            t.ImageContent(type="image", data="AAAA", mime_type="image/png"),
            t.TextContent(type="text", text="second"),
        ],
        structured_content={"results": []},
        is_error=False,
    )
    flat = call_result_from_sdk(result)
    assert flat.text == "first\n[image content omitted]\nsecond"
    assert flat.structured == {"results": []}
    assert flat.is_error is False
