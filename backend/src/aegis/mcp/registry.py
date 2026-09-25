"""The tool catalogue.

A registry is built once, at import time, and then read. Everything that could
be wrong with a tool declaration - a duplicate name, an unknown scope, an
output model that would hand a model raw log text, a write tool that does not
demand a ``ValidatedAction`` - fails here, loudly, at startup rather than at 3am
during an incident.

Two invariants are asserted rather than assumed:

* ``read_tools()`` and ``write_tools()`` are disjoint and together account for
  every registered tool. A name in both sets would mean the write path could be
  reached through a read permission.
* ``permitted_for`` fails closed. An unknown environment, an unknown scope or a
  caller without the scope yields nothing - never "everything", never a
  best-effort subset chosen by the caller's name.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterator
from dataclasses import dataclass
from typing import Any

from aegis.core.errors import NotFoundError
from aegis.core.logging import get_logger
from aegis.execution.validated import ValidatedAction
from aegis.mcp.types import (
    SCOPES,
    ToolContext,
    ToolContractError,
    ToolInput,
    ToolOutcome,
    ToolSpec,
)

log = get_logger(__name__)

# A read handler is given the context and its validated arguments. A write
# handler is additionally given the ``ValidatedAction`` that authorises it -
# there is no overload that lets a write handler run without one.
ReadHandler = Callable[[ToolContext, Any], Awaitable[ToolOutcome]]
WriteHandler = Callable[[ToolContext, Any, ValidatedAction], Awaitable[ToolOutcome]]


class ToolNotFound(NotFoundError):
    """Lookup of an unregistered tool name.

    A typed error rather than ``None`` so a caller cannot accidentally treat a
    missing tool as an empty result and carry on.
    """

    code = "TOOL_NOT_FOUND"


@dataclass(frozen=True, slots=True)
class RegisteredTool:
    """A spec bound to the callable that implements it."""

    spec: ToolSpec
    handler: ReadHandler | WriteHandler

    @property
    def name(self) -> str:
        return self.spec.name


class ToolRegistry:
    """The set of tools this process is willing to expose."""

    __slots__ = ("_tools", "_frozen")

    def __init__(self) -> None:
        self._tools: dict[str, RegisteredTool] = {}
        self._frozen = False

    # ---- construction ---------------------------------------------------- #

    def register(
        self, spec: ToolSpec, handler: ReadHandler | WriteHandler
    ) -> RegisteredTool:
        """Add one tool. Raises on any contract violation.

        Duplicate names are rejected outright rather than overwritten: a silent
        overwrite means the tool an operator reads in the catalogue is not the
        code that runs, which is the worst possible failure for an audit.
        """
        if self._frozen:
            raise ToolContractError(
                "the registry is frozen; tools are declared at import time only",
                context={"tool": spec.name},
            )
        if spec.name in self._tools:
            raise ToolContractError(
                f"duplicate tool name {spec.name!r}", context={"tool": spec.name}
            )
        if not callable(handler):
            raise ToolContractError(
                f"tool {spec.name} has no callable handler", context={"tool": spec.name}
            )
        if not issubclass(spec.input_model, ToolInput):
            raise ToolContractError(
                f"tool {spec.name} input model must derive from ToolInput",
                context={"tool": spec.name},
            )
        # Building the JSON schema here is not decoration: a model that cannot
        # be described cannot be offered to an MCP client, and finding that out
        # at import time is much cheaper than finding it out mid-incident.
        spec.json_schema()

        entry = RegisteredTool(spec=spec, handler=handler)
        self._tools[spec.name] = entry
        return entry

    def freeze(self) -> ToolRegistry:
        """Close the registry and assert its global invariants.

        Called by ``default_registry``. Once frozen the catalogue cannot grow,
        so a plugin loaded later cannot quietly add a write tool.
        """
        reads = self.read_tools()
        writes = self.write_tools()
        overlap = reads & writes
        if overlap:
            raise ToolContractError(
                "read and write tool sets must be disjoint",
                context={"tools": sorted(overlap)},
            )
        if reads | writes != set(self._tools):
            raise ToolContractError("every tool must be classified read or write")
        for entry in self._tools.values():
            spec = entry.spec
            if spec.access == "write" and not spec.requires_validated_action:
                raise ToolContractError(
                    f"write tool {spec.name} does not require a ValidatedAction",
                    context={"tool": spec.name},
                )
        self._frozen = True
        log.info(
            "tool registry frozen",
            tools=len(self._tools),
            read_tools=len(reads),
            write_tools=len(writes),
        )
        return self

    @property
    def frozen(self) -> bool:
        return self._frozen

    # ---- lookup ---------------------------------------------------------- #

    def get(self, name: str) -> RegisteredTool:
        entry = self._tools.get(name)
        if entry is None:
            raise ToolNotFound(
                f"no tool named {name!r}",
                context={"tool": name, "known": len(self._tools)},
            )
        return entry

    def spec(self, name: str) -> ToolSpec:
        return self.get(name).spec

    def has(self, name: str) -> bool:
        return name in self._tools

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._tools))

    def specs(self) -> tuple[ToolSpec, ...]:
        return tuple(self._tools[n].spec for n in sorted(self._tools))

    def __len__(self) -> int:
        return len(self._tools)

    def __iter__(self) -> Iterator[RegisteredTool]:
        return iter(self._tools[n] for n in sorted(self._tools))

    def read_tools(self) -> set[str]:
        return {n for n, t in self._tools.items() if t.spec.access == "read"}

    def write_tools(self) -> set[str]:
        return {n for n, t in self._tools.items() if t.spec.access == "write"}

    def scopes_in_use(self) -> set[str]:
        return {t.spec.scope for t in self._tools.values()}

    # ---- permission filter ----------------------------------------------- #

    def permitted_for(self, context: ToolContext) -> tuple[ToolSpec, ...]:
        """The tools this caller may invoke, in this environment.

        An unknown environment yields nothing at all. It is tempting to treat an
        unrecognised environment name as "probably a new staging cluster"; that
        assumption is how a production-shaped environment inherits local-only
        permissions.
        """
        if not context.environment_known:
            log.warning(
                "tool listing denied: unknown environment",
                environment=context.environment,
                caller=context.caller.subject,
            )
            return ()
        return tuple(
            spec
            for spec in self.specs()
            if context.environment in spec.environments and context.caller.may(spec.scope)
        )

    def catalogue(self, context: ToolContext | None = None) -> list[dict[str, Any]]:
        """Serialisable catalogue, optionally filtered to one caller."""
        specs = self.specs() if context is None else self.permitted_for(context)
        return [{**spec.as_dict(), "input_schema": spec.json_schema()} for spec in specs]


def known_scopes() -> frozenset[str]:
    """The closed permission vocabulary, re-exported for the API layer."""
    return SCOPES


__all__ = [
    "ReadHandler",
    "RegisteredTool",
    "ToolNotFound",
    "ToolRegistry",
    "WriteHandler",
    "known_scopes",
]
