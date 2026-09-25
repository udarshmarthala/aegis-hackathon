"""The controlled tool boundary.

Everything an agent can reach outside its own reasoning is declared here, and
every call goes through ``ToolInvoker``, which validates, authorises, charges,
bounds, records and audits it.

The shape of the package:

``types``       the contract - ToolSpec, ToolResult, ToolContext, ToolError
``registry``    the catalogue, with read/write sets proven disjoint at boot
``invoker``     the enforcement point every call passes through
``tools/``      the implementations, grouped by the question they answer
``server``      the same registry exposed over MCP stdio for external clients

``default_registry(deps)`` builds the full catalogue from an injected dependency
set and freezes it. Freezing is what makes the catalogue a security boundary
rather than a convention: after boot no code path can add a tool, so the set of
things an agent can do is fixed for the life of the process.
"""

from __future__ import annotations

from aegis.mcp.deps import ToolDeps
from aegis.mcp.invoker import ToolInvoker, require_scopes
from aegis.mcp.registry import (
    RegisteredTool,
    ToolNotFound,
    ToolRegistry,
    known_scopes,
)
from aegis.mcp.tools import (
    knowledge,
    remediation,
    runtime,
    sandbox,
    telemetry,
    topology,
)
from aegis.mcp.types import (
    ENVIRONMENTS,
    SCOPES,
    TOOL_WRITE_EVENT,
    BudgetLedger,
    BudgetView,
    CallerIdentity,
    ToolBudget,
    ToolContext,
    ToolContractError,
    ToolError,
    ToolInput,
    ToolOutcome,
    ToolOutput,
    ToolResult,
    ToolResultEnvelope,
    ToolSpec,
    untrusted,
)

# Read scopes an investigating agent is normally granted. Deliberately excludes
# every remediation scope: reading is broad, writing is narrow, and an
# investigator that can also execute is not an investigator.
INVESTIGATION_SCOPES = frozenset(
    {
        "telemetry:metrics",
        "telemetry:traces",
        "telemetry:logs",
        "topology:read",
        "knowledge:search",
        "code:read",
        "memory:read",
        "runtime:read",
    }
)


def default_registry(deps: ToolDeps) -> ToolRegistry:
    """Build and freeze the complete Aegis tool catalogue.

    Registration order is fixed and the result is frozen, so two processes built
    from the same code expose exactly the same surface. A tool that fails its
    contract - a duplicate name, an unknown scope, an output model that would
    hand a model raw log text - raises here, at boot, where it is cheap.
    """
    registry = ToolRegistry()
    telemetry.register(registry, deps)
    topology.register(registry, deps)
    knowledge.register(registry, deps)
    runtime.register(registry, deps)
    remediation.register(registry, deps)
    sandbox.register(registry, deps)
    return registry.freeze()


__all__ = [
    "ENVIRONMENTS",
    "INVESTIGATION_SCOPES",
    "SCOPES",
    "TOOL_WRITE_EVENT",
    "BudgetLedger",
    "BudgetView",
    "CallerIdentity",
    "RegisteredTool",
    "ToolBudget",
    "ToolContext",
    "ToolContractError",
    "ToolDeps",
    "ToolError",
    "ToolInput",
    "ToolInvoker",
    "ToolNotFound",
    "ToolOutcome",
    "ToolOutput",
    "ToolRegistry",
    "ToolResult",
    "ToolResultEnvelope",
    "ToolSpec",
    "default_registry",
    "known_scopes",
    "require_scopes",
    "untrusted",
]
