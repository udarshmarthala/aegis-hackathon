"""What the tool layer needs from the rest of the platform.

Every dependency is optional. That is not laziness - it is invariant 9: a
Prometheus, Neo4j or GitHub that is absent or unreachable degrades a tool's
answer and records an evidence gap, and never removes the tool from the
catalogue or halts the workflow. A tool whose dependency is ``None`` returns
``degraded=True`` with a reason, which is a different answer from "found
nothing" and is rendered differently all the way into the UI.

Nothing in this container is a write path on its own. ``execution`` is present
because ``execute_validated_action`` hands a ``ValidatedAction`` back to the
service that owns the gate chain - the tool layer never calls a runtime write
method itself.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from aegis.core.clock import SYSTEM_CLOCK, Clock
from aegis.evidence.store import EvidenceStore
from aegis.execution.approvals import ApprovalStore
from aegis.execution.executors import ExecutionPorts
from aegis.execution.sandbox import SandboxRunner
from aegis.execution.service import ExecutionService
from aegis.graph.graphrag import GraphRAG
from aegis.graph.traversal import GraphTraversal
from aegis.integrations.github import GitHubClient
from aegis.integrations.runtime import RuntimeAdapter
from aegis.memory.recall import IncidentMemoryRecall
from aegis.persistence.actions import ActionRepository
from aegis.retrieval.code import CodeRetriever
from aegis.retrieval.hybrid import HybridRetriever
from aegis.telemetry.loki import LokiClient
from aegis.telemetry.prometheus import PrometheusClient
from aegis.telemetry.tempo import TempoClient


@dataclass(frozen=True, slots=True)
class ToolDeps:
    """Injected collaborators for the tool boundary."""

    evidence: EvidenceStore | None = None

    # observation
    prometheus: PrometheusClient | None = None
    tempo: TempoClient | None = None
    loki: LokiClient | None = None

    # structure
    traversal: GraphTraversal | None = None
    graphrag: GraphRAG | None = None

    # knowledge
    retriever: HybridRetriever | None = None
    code: CodeRetriever | None = None
    memory: IncidentMemoryRecall | None = None
    github: GitHubClient | None = None

    # environment (reads only; every write goes through ``execution``)
    runtime: RuntimeAdapter | None = None

    # remediation
    actions: ActionRepository | None = None
    approvals: ApprovalStore | None = None
    execution: ExecutionService | None = None
    ports: ExecutionPorts | None = None

    # sandbox
    sandbox: SandboxRunner | None = None
    sandbox_image: str = ""

    clock: Clock = field(default=SYSTEM_CLOCK)


__all__ = ["ToolDeps"]
