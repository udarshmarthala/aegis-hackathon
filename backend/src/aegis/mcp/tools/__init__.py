"""Tool implementations, grouped by the question they answer.

Each module exposes one ``register(registry, deps)`` function. Nothing here is
imported for its side effects: a registry is always built explicitly, by
``aegis.mcp.default_registry``, from an injected dependency set. A module that
registered itself on import would make the catalogue depend on import order,
and an import-order-dependent permission surface is not a permission surface.
"""

from __future__ import annotations

from aegis.mcp.tools import (
    knowledge,
    remediation,
    runtime,
    sandbox,
    telemetry,
    topology,
)

__all__ = [
    "knowledge",
    "remediation",
    "runtime",
    "sandbox",
    "telemetry",
    "topology",
]
