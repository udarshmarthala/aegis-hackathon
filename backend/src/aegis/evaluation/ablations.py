"""Named ablations: which parts of the architecture actually earn their place?

Aegis claims that a knowledge graph, hybrid retrieval, an evidence verifier,
change analysis, specialised agents and incident memory each make it better.
That is a testable claim, and the only honest test is to remove one piece at a
time and re-run the same benchmark.

Each ablation is a *configuration*, not a code path. The harness hands it to the
system under test, which applies it when it builds its dependencies - so an
ablation cannot drift away from what production actually runs, and there is no
``if ablation == ...`` scattered through the workflow.

Ablations only ever remove capability. None of them can disable a safety
control: the gate chain, the policy engine and the evidence validator are not
ablatable, because a benchmark run that turns off the brakes is measuring a
system nobody would ship. ``no_verifier`` removes the *investigation-side*
grounding check, not the execution gate that blocks ungrounded actions.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Any, Final

from aegis.core.errors import ValidationError
from aegis.core.logging import get_logger

log = get_logger(__name__)

FULL: Final = "full"


@dataclass(frozen=True, slots=True)
class AblationConfig:
    """One benchmark configuration of the system under test."""

    name: str
    description: str
    use_graph: bool = True
    use_retrieval: bool = True
    use_evidence_verifier: bool = True
    use_change_analysis: bool = True
    multi_agent: bool = True
    use_incident_memory: bool = True

    @property
    def is_full(self) -> bool:
        return self.name == FULL

    def disabled_components(self) -> tuple[str, ...]:
        flags = {
            "graph": self.use_graph,
            "retrieval": self.use_retrieval,
            "evidence_verifier": self.use_evidence_verifier,
            "change_analysis": self.use_change_analysis,
            "multi_agent": self.multi_agent,
            "incident_memory": self.use_incident_memory,
        }
        return tuple(sorted(name for name, enabled in flags.items() if not enabled))

    def as_json(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "disabled": list(self.disabled_components()),
        }

    def apply(self, deps: Any) -> Any:
        """Return a copy of ``WorkflowDeps`` with the ablated components removed.

        Typed loosely and imported nowhere: the evaluation package must not pull
        the agent runtime (and LangGraph with it) into a report-only process.
        Every field cleared here is already optional on ``WorkflowDeps`` - the
        workflow degrades to evidence gaps rather than failing, which is exactly
        the behaviour an ablation is meant to measure.

        Every flag on this config maps to something the workflow reads. A flag
        with no reader is a decorative entry that makes two identical arms look
        like an experiment, so there is deliberately no such flag left here.
        """
        changes: dict[str, Any] = {}
        if not self.use_graph:
            changes.update(graphrag=None, topology=None, neo4j=_NullGraph())
        if not self.use_retrieval:
            changes.update(retriever=None, code=None)
        if not self.use_change_analysis:
            changes.update(github=None)
        if not self.use_incident_memory:
            changes.update(memory_recall=None, memory_store=None)
        if not self.multi_agent:
            # Read by build_workflow: the enrichment nodes are never registered
            # on the graph, so the fan-out cannot happen.
            changes.update(single_agent=True)
        if not self.use_evidence_verifier:
            # Read by the diagnose node: the investigation-side grounding gate
            # is skipped. The execution gate chain is untouched and still
            # refuses to let an ungrounded action run.
            changes.update(skip_evidence_verification=True)
        if not changes:
            return deps
        try:
            return replace(deps, **changes)
        except TypeError as exc:
            # A renamed dependency field must fail the run loudly: silently
            # running "no_graph" with the graph still attached would produce a
            # comparison that looks valid and means nothing.
            raise ValidationError(
                f"ablation {self.name!r} cannot be applied to {type(deps).__name__}: {exc}",
                context={"ablation": self.name},
            ) from exc


class _NullGraph:
    """A Neo4j client that is always unreachable.

    ``WorkflowDeps.neo4j`` is required, so ``no_graph`` supplies one that
    reports itself down. The workflow then records an evidence gap, which is the
    honest representation of "this deployment has no topology graph" - and is
    also exactly what happens in production when Neo4j is down.
    """

    __slots__ = ()

    async def healthy(self) -> bool:
        return False

    async def run(self, cypher: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        from aegis.core.errors import SourceUnavailable

        del cypher, params
        raise SourceUnavailable("graph disabled by ablation", context={"ablation": "no_graph"})

    async def write(self, cypher: str, params: dict[str, Any]) -> None:
        from aegis.core.errors import SourceUnavailable

        del cypher, params
        raise SourceUnavailable("graph disabled by ablation", context={"ablation": "no_graph"})

    async def close(self) -> None:
        return None


ABLATIONS: Final[Mapping[str, AblationConfig]] = {
    FULL: AblationConfig(
        name=FULL,
        description="the complete architecture; the baseline every ablation is compared to",
    ),
    "no_graph": AblationConfig(
        name="no_graph",
        description="no Neo4j topology: blast radius and causal paths from telemetry alone",
        use_graph=False,
    ),
    "no_rag": AblationConfig(
        name="no_rag",
        description="no hybrid retrieval: no runbooks, postmortems or code context",
        use_retrieval=False,
    ),
    "no_verifier": AblationConfig(
        name="no_verifier",
        description=(
            "no investigation-side evidence verification; the execution gate chain "
            "still enforces grounding before any write"
        ),
        use_evidence_verifier=False,
    ),
    "no_change_analysis": AblationConfig(
        name="no_change_analysis",
        description="no deployment or commit correlation",
        use_change_analysis=False,
    ),
    "single_agent": AblationConfig(
        name="single_agent",
        description="one agent with every tool instead of specialised agents",
        multi_agent=False,
    ),
    "no_incident_memory": AblationConfig(
        name="no_incident_memory",
        description="no recall of previous incidents; every incident is the first one",
        use_incident_memory=False,
    ),
}
# There is deliberately no ``no_execution_verification`` arm. Post-execution
# verification is not a component the workflow can decline to use: the execution
# service captures a baseline, verifies, and then commits or rolls back inside
# one state machine, and the verdict is what decides which. An arm that removed
# it would also remove the rollback decision - a safety control, and this module
# does not ablate those. It existed as a flag nothing read, which made an
# identical arm look like a measurement; removing it is the honest fix.


def get_ablation(name: str | None) -> AblationConfig:
    """Resolve an ablation by name. Unknown names fail closed, with the options."""
    key = (name or FULL).strip().lower()
    config = ABLATIONS.get(key)
    if config is None:
        raise ValidationError(
            f"unknown ablation {name!r}; available: {', '.join(sorted(ABLATIONS))}",
            context={"ablation": name, "available": sorted(ABLATIONS)},
        )
    return config


def ablation_names() -> tuple[str, ...]:
    return tuple(sorted(ABLATIONS))


__all__ = ["ABLATIONS", "FULL", "AblationConfig", "ablation_names", "get_ablation"]
