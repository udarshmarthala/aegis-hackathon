"""Assemble ``HorizonDeps`` from the composition root.

One function so that the worker - and any test that wants the production
wiring - builds the step loop exactly one way. Optional collaborators that the
container could not build arrive as ``None``; the orchestrator records each as
an evidence gap rather than failing, and refuses every proposal when the write
path (gate, execution service, runtime ports) is incomplete.
"""

from __future__ import annotations

from typing import Any

from aegis.agents.horizon.compactor import Compactor
from aegis.agents.horizon.orchestrator import HorizonDeps
from aegis.agents.horizon.scripted import ScriptedBrain
from aegis.agents.horizon.tools_observe import PrometheusHealthProbe
from aegis.core.config import Settings


def build_horizon_deps(container: Any, settings: Settings, bus: Any) -> HorizonDeps:
    # The brain router is built by the container; if it could not be, the
    # scripted policy still decides every step - labelled as such in the UI.
    brain = container.brain if container.brain is not None else ScriptedBrain()
    return HorizonDeps(
        settings=settings,
        store=container.horizon_store,
        bus=bus,
        brain=brain,
        compactor=Compactor(container.compactor_llm),
        tools=container.tools,
        gate=container.gate,
        execution=container.execution,
        ports=container.ports,
        actions=container.actions,
        incidents=container.incidents,
        evidence=container.evidence,
        memory_store=container.memory_store,
        memory_recall=container.memory_recall,
        rawtree=container.rawtree,
        rawtree_tools=container.rawtree_tools,
        known_issues=container.known_issues,
        incident_map=container.incident_map,
        health_probe=PrometheusHealthProbe(container.prometheus),
    )


__all__ = ["build_horizon_deps"]
