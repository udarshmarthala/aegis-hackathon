"""A client for third-party MCP servers, reduced to an allowlist.

The in-process tool boundary (``mcp.invoker``) governs tools Aegis implements.
This module governs tools *someone else* implements - Nimble's web search,
RawTree's query server - where the catalogue is whatever the vendor decided to
ship this week. Three rules follow from that.

**The allowlist is the catalogue.** ``list_tools`` returns only allowlisted
names; everything else the server advertises (``nimble_crawl``,
``delete-table``, ``create-api-key`` ...) is removed before any caller can see
it, so a brain cannot be offered a tool nobody reviewed. ``call_tool`` checks
the allowlist *before* opening a connection: a refused name costs nothing and
reaches nobody.

**Every await has a deadline.** A remote session is a handshake, one or more
requests and a teardown, over a transport that may hold a stream open. The
whole of it runs inside one ``timeout_s`` bound; a server that stops answering
becomes an ``ExternalServiceError`` rather than a stuck step.

**The bearer never travels further than the header.** It is not in ``repr``,
not in logs, and error messages are scrubbed of it and raised ``from None`` so
a chained transport exception cannot carry it into a traceback either.

Tool calls are never retried here. Vendors meter them, and some (an agent
run, a crawl) are not idempotent; the caller owns any decision to try again.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from typing import Any, Final, Protocol

from aegis.agents.horizon.ports import ToolSpec
from aegis.core.clock import SYSTEM_CLOCK, Clock
from aegis.core.errors import AegisError, ExternalServiceError
from aegis.core.logging import get_logger
from aegis.core.resilience import Bulkhead, with_timeout

log = get_logger(__name__)

# Bounds on what a remote server can make us hold. A tool result is page text
# at most; anything larger is a server bug or an attempt to exhaust memory.
MAX_RESULT_CHARS: Final = 200_000
MAX_LIST_PAGES: Final = 5
DEFAULT_LIST_TTL_S: Final = 60.0
_MAX_ERROR_CHARS: Final = 300

_BEARER_RE = re.compile(r"(?i)bearer\s+\S+")


# --------------------------------------------------------------------------- #
# public types                                                                 #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class RemoteCallResult:
    """One tool call's answer in a transport-neutral shape.

    ``text`` is the concatenated text content, bounded. It is whatever the
    vendor's server said, so callers treat it as untrusted data.
    ``is_error`` is the tool reporting failure in-band, which is distinct from
    the transport failing (that raises).
    """

    text: str
    structured: dict[str, Any] | None
    is_error: bool


class ToolNotAllowed(AegisError):
    """The name is not on this connection's allowlist. Raised before any I/O."""

    code = "REMOTE_TOOL_NOT_ALLOWED"
    http_status = 403
    retryable = False


class RawSession(Protocol):
    """An open, initialised session with no allowlist of its own.

    The SDK-backed implementation lives below; tests inject fakes. Nothing
    outside this module ever holds one - callers get a ``RemoteMCPSession``.
    """

    async def list_tools(self) -> list[ToolSpec]: ...

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> RemoteCallResult: ...


SessionFactory = Callable[[], AbstractAsyncContextManager[RawSession]]


# --------------------------------------------------------------------------- #
# the client                                                                   #
# --------------------------------------------------------------------------- #


class RemoteMCPClient:
    """Allowlisted access to one remote MCP server over Streamable HTTP.

    ``list_tools`` and ``call_tool`` each open a session, do one thing and
    close it. A caller making several calls in a row (search, then extract)
    uses ``session()`` to pay the handshake once; the allowlist and deadlines
    apply identically there.
    """

    def __init__(
        self,
        url: str,
        bearer: str,
        *,
        allowlist: frozenset[str],
        timeout_s: float,
        name: str,
        session_factory: SessionFactory | None = None,
        clock: Clock = SYSTEM_CLOCK,
        list_ttl_s: float = DEFAULT_LIST_TTL_S,
    ) -> None:
        if timeout_s <= 0:
            raise ValueError("timeout_s must be positive")
        self._url = url
        self._bearer = bearer
        self._allowlist = frozenset(allowlist)
        self._timeout_s = timeout_s
        self._name = name
        self._factory: SessionFactory = session_factory or self._sdk_session
        self._clock = clock
        self._list_ttl_s = list_ttl_s
        self._cached: tuple[float, list[ToolSpec]] | None = None
        # Remote vendors are rate-limited per key; four in flight is plenty for
        # one incident and stops a burst from queueing without bound.
        self._bulkhead = Bulkhead(f"mcp:{name}", 4, acquire_timeout=timeout_s)

    def __repr__(self) -> str:
        return (
            f"RemoteMCPClient(name={self._name!r}, url={self._url!r}, "
            f"allowlist={sorted(self._allowlist)!r}, configured={self.configured})"
        )

    @property
    def name(self) -> str:
        return self._name

    @property
    def configured(self) -> bool:
        return bool(self._url and self._bearer)

    @property
    def allowlist(self) -> frozenset[str]:
        return self._allowlist

    @property
    def timeout_s(self) -> float:
        return self._timeout_s

    def is_allowed(self, tool: str) -> bool:
        return tool in self._allowlist

    def check_allowed(self, tool: str) -> None:
        if tool not in self._allowlist:
            raise ToolNotAllowed(
                f"{self._name}: tool {tool!r} is not allowlisted",
                context={"server": self._name, "tool": tool},
            )

    # -- one-shot calls ------------------------------------------------------

    async def list_tools(self) -> list[ToolSpec]:
        """Allowlisted tools only; cached for ``list_ttl_s``."""
        cached = self._fresh_cache()
        if cached is not None:
            return cached
        async with self.session() as s:
            return await s.list_tools()

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> RemoteCallResult:
        self.check_allowed(name)  # before any I/O
        async with self.session() as s:
            return await s.call_tool(name, arguments)

    # -- multi-call session --------------------------------------------------

    @asynccontextmanager
    async def session(self) -> AsyncIterator[RemoteMCPSession]:
        """One connection for several calls. Connect and each call are bounded."""
        if not self.configured:
            raise ExternalServiceError(
                f"{self._name}: remote MCP server is not configured",
                context={"server": self._name},
                retryable=False,
            )
        async with self._bulkhead:
            cm = self._factory()
            try:
                raw = await with_timeout(cm.__aenter__(), self._timeout_s, what=self._what)
            except AegisError as exc:
                raise self._scrubbed(exc, retryable=True) from None
            except Exception as exc:  # noqa: BLE001 - every transport fault is typed below
                raise self._transport_error("connect", exc, retryable=True) from None

            failure: BaseException | None = None
            try:
                yield RemoteMCPSession(self, raw)
            except BaseException as exc:
                failure = exc
                raise
            finally:
                await self._close(cm, failure)

    async def _close(
        self, cm: AbstractAsyncContextManager[RawSession], failure: BaseException | None
    ) -> None:
        """Tear the session down within the deadline; never mask the real error."""
        try:
            exc_type = type(failure) if failure is not None else None
            tb = failure.__traceback__ if failure is not None else None
            await with_timeout(
                cm.__aexit__(exc_type, failure, tb), self._timeout_s, what=self._what
            )
        except Exception as exc:  # noqa: BLE001 - teardown after the answer is in hand
            if failure is None:
                # The result is already with the caller; a failed DELETE of the
                # server-side session is the vendor's garbage, not our error.
                log.warning(
                    "remote mcp teardown failed", server=self._name,
                    error=self._scrub(f"{type(exc).__name__}: {exc}"),
                )

    # -- internals used by RemoteMCPSession ---------------------------------

    @property
    def _what(self) -> str:
        return f"mcp:{self._name}"

    def _fresh_cache(self) -> list[ToolSpec] | None:
        if self._cached is None:
            return None
        at, tools = self._cached
        if self._clock.monotonic() - at > self._list_ttl_s:
            return None
        return list(tools)

    def _filter(self, advertised: list[ToolSpec]) -> list[ToolSpec]:
        kept = [t for t in advertised if t.name in self._allowlist]
        dropped = sorted(t.name for t in advertised if t.name not in self._allowlist)
        if dropped:
            log.info(
                "remote mcp tools filtered", server=self._name,
                kept=[t.name for t in kept], dropped=dropped[:50],
            )
        self._cached = (self._clock.monotonic(), kept)
        return list(kept)

    def _scrub(self, text: str) -> str:
        if self._bearer:
            text = text.replace(self._bearer, "***")
        return _BEARER_RE.sub("Bearer ***", text)[:_MAX_ERROR_CHARS]

    def _scrubbed(self, exc: AegisError, *, retryable: bool) -> AegisError:
        """Re-issue an Aegis error with its message scrubbed and its cause cut."""
        if isinstance(exc, ToolNotAllowed):
            return exc
        return type(exc)(
            self._scrub(exc.message),
            code=exc.code,
            context={"server": self._name, **{k: v for k, v in exc.context.items() if k != "url"}},
            retryable=retryable if isinstance(exc, ExternalServiceError) else exc.retryable,
        )

    def _transport_error(
        self, op: str, exc: BaseException, *, retryable: bool
    ) -> ExternalServiceError:
        leaf = _first_leaf(exc)
        detail = self._scrub(f"{type(leaf).__name__}: {leaf}")
        log.warning("remote mcp call failed", server=self._name, op=op, error=detail)
        return ExternalServiceError(
            f"{self._name}: {op} failed ({detail})",
            context={"server": self._name, "op": op},
            retryable=retryable,
        )

    # -- the real transport --------------------------------------------------

    @asynccontextmanager
    async def _sdk_session(self) -> AsyncIterator[RawSession]:
        # Imported lazily: the SDK pulls in a sizeable transport stack that a
        # process with no remote servers configured should never load.
        import httpx2
        from mcp.client.session import ClientSession
        from mcp.client.streamable_http import streamable_http_client
        from mcp.shared._httpx_utils import create_mcp_http_client

        http = create_mcp_http_client(
            headers={"Authorization": f"Bearer {self._bearer}"},
            timeout=httpx2.Timeout(self._timeout_s),
        )
        async with (
            http,
            streamable_http_client(self._url, http_client=http) as (read, write),
            ClientSession(read, write, read_timeout_seconds=self._timeout_s) as session,
        ):
            await session.initialize()
            yield _SdkRawSession(session)


class RemoteMCPSession:
    """An open connection, allowlist-enforcing. Obtained from ``session()``."""

    __slots__ = ("_client", "_raw")

    def __init__(self, client: RemoteMCPClient, raw: RawSession) -> None:
        self._client = client
        self._raw = raw

    async def list_tools(self) -> list[ToolSpec]:
        c = self._client
        cached = c._fresh_cache()  # noqa: SLF001 - one unit with the client
        if cached is not None:
            return cached
        try:
            advertised = await with_timeout(self._raw.list_tools(), c.timeout_s, what=c._what)  # noqa: SLF001
        except AegisError as exc:
            raise c._scrubbed(exc, retryable=True) from None  # noqa: SLF001
        except Exception as exc:  # noqa: BLE001 - every transport fault is typed
            raise c._transport_error("list_tools", exc, retryable=True) from None  # noqa: SLF001
        return c._filter(advertised)  # noqa: SLF001

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> RemoteCallResult:
        c = self._client
        c.check_allowed(name)
        try:
            result = await with_timeout(
                self._raw.call_tool(name, dict(arguments)), c.timeout_s, what=c._what  # noqa: SLF001
            )
        except AegisError as exc:
            # A tool call may be metered or non-idempotent: never marked retryable.
            raise c._scrubbed(exc, retryable=False) from None  # noqa: SLF001
        except Exception as exc:  # noqa: BLE001 - every transport fault is typed
            raise c._transport_error(f"call {name}", exc, retryable=False) from None  # noqa: SLF001
        if len(result.text) > MAX_RESULT_CHARS:
            result = RemoteCallResult(
                text=result.text[:MAX_RESULT_CHARS],
                structured=result.structured,
                is_error=result.is_error,
            )
        return result


# --------------------------------------------------------------------------- #
# SDK adapter                                                                  #
# --------------------------------------------------------------------------- #


class _SdkRawSession:
    """``RawSession`` over the MCP SDK's ``ClientSession``."""

    __slots__ = ("_s",)

    def __init__(self, session: Any) -> None:
        self._s = session

    async def list_tools(self) -> list[ToolSpec]:
        from mcp_types import PaginatedRequestParams

        specs: list[ToolSpec] = []
        cursor: str | None = None
        for _ in range(MAX_LIST_PAGES):
            params = PaginatedRequestParams(cursor=cursor) if cursor else None
            page = await self._s.list_tools(params=params)
            specs.extend(tool_spec_from_sdk(t) for t in page.tools)
            cursor = page.next_cursor
            if not cursor:
                break
        return specs

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> RemoteCallResult:
        return call_result_from_sdk(await self._s.call_tool(name, arguments))


def tool_spec_from_sdk(tool: Any) -> ToolSpec:
    schema = tool.input_schema if isinstance(tool.input_schema, Mapping) else {}
    return ToolSpec(
        name=str(tool.name),
        description=str(tool.description or "")[:1000],
        input_schema=dict(schema),
    )


def call_result_from_sdk(result: Any) -> RemoteCallResult:
    """Flatten an SDK ``CallToolResult``: text parts joined, other parts noted."""
    parts: list[str] = []
    for item in getattr(result, "content", None) or []:
        kind = getattr(item, "type", "")
        if kind == "text":
            parts.append(str(item.text))
        elif kind == "resource":
            text = getattr(getattr(item, "resource", None), "text", None)
            parts.append(str(text) if text is not None else "[binary resource omitted]")
        else:
            parts.append(f"[{kind or 'unknown'} content omitted]")
    structured = getattr(result, "structured_content", None)
    return RemoteCallResult(
        text="\n".join(parts)[:MAX_RESULT_CHARS],
        structured=dict(structured) if isinstance(structured, Mapping) else None,
        is_error=bool(getattr(result, "is_error", False)),
    )


def _first_leaf(exc: BaseException) -> BaseException:
    """The SDK's task groups wrap failures in ExceptionGroups; report the cause."""
    seen = 0
    while isinstance(exc, BaseExceptionGroup) and exc.exceptions and seen < 8:
        exc = exc.exceptions[0]
        seen += 1
    return exc


__all__ = [
    "MAX_RESULT_CHARS",
    "RawSession",
    "RemoteCallResult",
    "RemoteMCPClient",
    "RemoteMCPSession",
    "SessionFactory",
    "ToolNotAllowed",
    "call_result_from_sdk",
    "tool_spec_from_sdk",
]
