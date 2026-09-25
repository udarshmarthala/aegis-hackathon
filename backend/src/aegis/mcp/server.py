"""The registry, exposed over the Model Context Protocol on stdio.

Same registry, same invoker, same enforcement as the internal agents get. An
external MCP client is not a privileged caller: it is subject to the identical
schema validation, scope check, environment check, budget and timeout, and its
calls land in ``tool_calls`` like everyone else's.

Two deliberate restrictions:

* **Write tools are never advertised and never reachable.** A write tool
  requires an ``execution.ValidatedAction``, which only the gate chain can mint
  and which cannot cross a JSON transport. The catalogue therefore lists reads
  only, and the invoker would refuse a write even if a client guessed the name.
* **Scopes come from the operator, not from the client.** The identity a session
  runs as is constructed by whoever starts the server. Nothing a client sends -
  a tool argument, a header, a prompt - can widen it.

The ``mcp`` package is an optional dependency. If it is absent this module still
imports cleanly and reports the server unavailable with the reason, because an
optional transport must never be able to stop the control plane from booting.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import timedelta
from importlib import import_module
from typing import Any, Final

from aegis.core.clock import SYSTEM_CLOCK, Clock
from aegis.core.errors import ConfigError
from aegis.core.ids import correlation_id as new_correlation_id
from aegis.core.logging import get_logger
from aegis.mcp.invoker import ToolInvoker
from aegis.mcp.registry import ToolRegistry
from aegis.mcp.types import (
    CallerIdentity,
    ToolBudget,
    ToolContext,
    ToolResultEnvelope,
    ToolSpec,
)

log = get_logger(__name__)

SERVER_NAME: Final = "aegis"

# Imported dynamically rather than with a plain ``import mcp``: the package is
# optional, and a static import would put an unresolvable module in the import
# graph of a control plane that must boot without it.
_server_mod: Any
_stdio_mod: Any
_types_mod: Any

try:
    _server_mod = import_module("mcp.server")
    _stdio_mod = import_module("mcp.server.stdio")
    _types_mod = import_module("mcp.types")
except ImportError as exc:  # the default in this deployment
    _server_mod = _stdio_mod = _types_mod = None
    MCP_AVAILABLE = False
    MCP_UNAVAILABLE_REASON = (
        "the optional 'mcp' package is not installed; the internal tool boundary "
        f"is unaffected ({exc})"
    )
else:  # pragma: no cover - exercised only where the optional dep is installed
    MCP_AVAILABLE = True
    MCP_UNAVAILABLE_REASON = ""


@dataclass(frozen=True, slots=True)
class SessionLimits:
    """Hard caps for one external session.

    An external client has no ``BudgetGuard`` behind it, so it gets one of its
    own. An unbounded external caller is an unbounded agent in a different coat.
    """

    max_tool_calls: int = 200
    max_seconds: float = 900.0
    per_call_deadline_s: float = 60.0

    def __post_init__(self) -> None:
        if self.max_tool_calls < 1 or self.max_seconds <= 0 or self.per_call_deadline_s <= 0:
            raise ConfigError("MCP session limits must be positive")


class AegisMCPServer:
    """Serves the read side of the tool registry to MCP clients."""

    __slots__ = (
        "_budget", "_clock", "_environment", "_identity", "_invoker", "_limits", "_registry",
    )

    def __init__(
        self,
        registry: ToolRegistry,
        invoker: ToolInvoker,
        *,
        identity: CallerIdentity,
        environment: str,
        limits: SessionLimits | None = None,
        clock: Clock = SYSTEM_CLOCK,
    ) -> None:
        self._registry = registry
        self._invoker = invoker
        self._identity = identity
        self._environment = environment
        self._limits = limits or SessionLimits()
        self._clock = clock
        self._budget = ToolBudget(
            max_tool_calls=self._limits.max_tool_calls,
            max_seconds=self._limits.max_seconds,
            clock=clock,
        )

    # ---- availability ---------------------------------------------------- #

    @property
    def available(self) -> bool:
        return MCP_AVAILABLE

    @property
    def unavailable_reason(self) -> str:
        return MCP_UNAVAILABLE_REASON

    def status(self) -> dict[str, Any]:
        """Shape the integration-health surface renders."""
        return {
            "server": SERVER_NAME,
            "available": self.available,
            "reason": self.unavailable_reason,
            "environment": self._environment,
            "tools_exposed": len(self.exposed_tools()),
            "write_tools_exposed": 0,
        }

    # ---- catalogue ------------------------------------------------------- #

    def exposed_tools(self) -> tuple[ToolSpec, ...]:
        """Read tools this session's identity may call, and nothing else.

        The write filter is belt and braces - the invoker refuses a write
        without a ``ValidatedAction`` regardless - but advertising a tool that
        can never succeed here would be a lie in the catalogue.
        """
        return tuple(
            spec
            for spec in self._registry.permitted_for(self._context())
            if spec.access == "read"
        )

    def tool_descriptors(self) -> list[dict[str, Any]]:
        """Transport-independent descriptors, usable without the mcp package."""
        return [
            {
                "name": spec.name,
                "description": spec.description,
                "inputSchema": spec.json_schema(),
            }
            for spec in self.exposed_tools()
        ]

    # ---- invocation ------------------------------------------------------ #

    def _context(self, incident_id: str | None = None) -> ToolContext:
        return ToolContext(
            environment=self._environment,
            caller=self._identity,
            budget=self._budget,
            deadline=self._clock.now() + timedelta(seconds=self._limits.per_call_deadline_s),
            correlation_id=new_correlation_id(),
            incident_id=incident_id,
        )

    async def call(self, name: str, arguments: dict[str, Any]) -> ToolResultEnvelope:
        """Invoke one tool for an external client.

        ``incident_id`` is accepted as an ordinary argument because a client may
        legitimately scope a query to an incident; it is pulled out of the
        arguments and into the context so evidence lands on the right incident
        rather than being passed to a tool schema that does not declare it.
        """
        payload = dict(arguments)
        incident_id = payload.pop("incident_id", None)
        result = await self._invoker.invoke(
            name,
            payload,
            self._context(incident_id if isinstance(incident_id, str) else None),
        )
        return ToolResultEnvelope.of(result)

    # ---- transport ------------------------------------------------------- #

    def build(self) -> Any:
        """Construct the MCP server object. Raises when the dep is missing."""
        if not MCP_AVAILABLE:
            raise ConfigError(MCP_UNAVAILABLE_REASON)

        server = _server_mod.Server(SERVER_NAME)

        async def _list_tools() -> list[Any]:
            return [
                _types_mod.Tool(
                    name=descriptor["name"],
                    description=descriptor["description"],
                    inputSchema=descriptor["inputSchema"],
                )
                for descriptor in self.tool_descriptors()
            ]

        async def _call_tool(name: str, arguments: dict[str, Any]) -> list[Any]:
            envelope = await self.call(name, arguments or {})
            # A failed call returns a structured error rather than raising: an
            # MCP client sees the same typed result an internal agent does.
            return [
                _types_mod.TextContent(
                    type="text",
                    text=json.dumps(envelope.model_dump(mode="json"), default=str),
                )
            ]

        # Registered by calling the decorator factories rather than with ``@``
        # syntax: the mcp package is loaded dynamically, so its decorators are
        # untyped here and decorator syntax would erase these handlers' types.
        server.list_tools()(_list_tools)
        server.call_tool()(_call_tool)
        return server

    async def run_stdio(self) -> None:  # pragma: no cover - needs a live transport
        """Serve on stdio until the client disconnects."""
        if not MCP_AVAILABLE:
            raise ConfigError(MCP_UNAVAILABLE_REASON)
        server = self.build()
        log.info(
            "mcp stdio server starting",
            environment=self._environment,
            tools=len(self.exposed_tools()),
            caller=self._identity.subject,
        )
        async with _stdio_mod.stdio_server() as (read_stream, write_stream):
            await server.run(
                read_stream, write_stream, server.create_initialization_options()
            )


def server_status() -> dict[str, str | bool]:
    """Whether this process could serve MCP at all, for /health."""
    return {"available": MCP_AVAILABLE, "reason": MCP_UNAVAILABLE_REASON}


__all__ = [
    "MCP_AVAILABLE",
    "MCP_UNAVAILABLE_REASON",
    "SERVER_NAME",
    "AegisMCPServer",
    "SessionLimits",
    "server_status",
]
