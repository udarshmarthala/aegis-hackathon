"""The composition root.

One place builds the object graph, and both the API and the worker use it. The
alternative - each process wiring its own components - is how an API ends up
enforcing a different policy from the worker that actually executes, which is
precisely the class of bug this system cannot afford.

Two principles:

* **Optional capabilities degrade, they do not fail.** Neo4j, GitHub, Slack,
  embeddings, Docker and LangSmith are all optional. A missing one produces a
  component that reports itself unavailable, and the investigation records an
  evidence gap. Only Postgres is required, because without a system of record
  there is nothing to be correct about.
* **Construction never performs I/O it can defer.** Clients connect lazily, so
  a slow or absent dependency delays its first use rather than blocking boot
  and failing a readiness probe that would otherwise have passed.
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass, field
from typing import Any

from aegis.agents.llm import ModelRouter
from aegis.core.config import Settings
from aegis.core.logging import get_logger
from aegis.core.resilience import retry_async
from aegis.evidence.store import EvidenceStore
from aegis.evidence.validator import EvidenceValidator
from aegis.execution.adapters import RedisCachePort, RuntimePortBridge
from aegis.execution.approvals import ApprovalStore
from aegis.execution.executors import ExecutionPorts
from aegis.execution.leases import LeaseManager
from aegis.execution.sandbox import SandboxRunner
from aegis.execution.service import ExecutionService, RecordingSandboxRunner
from aegis.execution.validated import ActionGate
from aegis.graph.client import Neo4jClient
from aegis.mcp import ToolDeps, ToolInvoker, ToolRegistry, default_registry
from aegis.persistence.actions import ActionRepository
from aegis.persistence.audit import AuditLog
from aegis.persistence.db import Database
from aegis.persistence.incidents import IncidentRepository
from aegis.persistence.patches import (
    DeploymentRepository,
    PatchRepository,
    SandboxRunRepository,
)
from aegis.policy.store import PolicyStore
from aegis.telemetry.prometheus import PrometheusClient
from aegis.verification.engine import VerificationEngine
from aegis.verification.store import VerificationStore

log = get_logger(__name__)


@dataclass
class Capability:
    """Whether one optional component is usable, and why not when it is not.

    Carried through to ``/health`` and to the Integrations page. An operator
    seeing degraded results needs to know which source was missing, not just
    that confidence was low.
    """

    name: str
    configured: bool
    reason: str = ""


@dataclass
class Container:
    """Every long-lived component, built once per process."""

    settings: Settings

    # required
    db: Database = field(init=False)
    incidents: IncidentRepository = field(init=False)
    evidence: EvidenceStore = field(init=False)
    validator: EvidenceValidator = field(init=False)
    audit: AuditLog = field(init=False)
    actions: ActionRepository = field(init=False)
    policy: PolicyStore = field(init=False)
    approvals: ApprovalStore = field(init=False)
    leases: LeaseManager = field(init=False)
    gate: ActionGate = field(init=False)
    verification: VerificationEngine = field(init=False)
    verification_store: VerificationStore = field(init=False)
    execution: ExecutionService = field(init=False)
    prometheus: PrometheusClient = field(init=False)
    router: ModelRouter = field(init=False)
    neo4j: Neo4jClient = field(init=False)
    # Declared as the base type and built as the recording subclass. Every
    # holder - the tool boundary included - therefore gets a runner that
    # persists what it ran, and there is no unrecorded runner to hand out.
    sandbox: SandboxRunner = field(init=False)
    sandbox_runs: SandboxRunRepository = field(init=False)
    patches: PatchRepository = field(init=False)
    deployments: DeploymentRepository = field(init=False)

    # optional - any of these may be None
    redis: Any = None
    tempo: Any = None
    loki: Any = None
    github: Any = None
    slack: Any = None
    langsmith: Any = None
    embeddings: Any = None
    documents: Any = None
    retriever: Any = None
    code: Any = None
    memory_store: Any = None
    memory_recall: Any = None
    graph_ingest: Any = None
    topology: Any = None
    graphrag: Any = None
    runtime: Any = None
    # The raw adapter behind ``runtime``. Kept separately because the bridge
    # deliberately hides the adapter's write surface behind two Protocols,
    # while the tool layer's read tools need the adapter's own identity
    # (``name``, ``available``, ``unavailable_reason``) to say *which*
    # environment could not be reached.
    runtime_adapter: Any = None
    ports: ExecutionPorts | None = None

    # The tool boundary. ``None`` means this process has no authorised way to
    # reach outside itself: every caller then records an evidence gap rather
    # than falling back to a direct client call, which is the fail-closed
    # reading of "the boundary could not be built".
    tool_registry: ToolRegistry | None = None
    tools: ToolInvoker | None = None

    # Long-horizon agent. Every sponsor integration is optional and has a
    # labelled fallback behind the same Protocol, so any of these may be None
    # without the step loop losing its ability to run the golden path.
    horizon_store: Any = None
    brain: Any = None
    compactor_llm: Any = None
    rawtree: Any = None
    rawtree_tools: Any = None
    known_issues: Any = None
    incident_map: Any = None

    capabilities: dict[str, Capability] = field(default_factory=dict)

    def _mark(self, name: str, configured: bool, reason: str = "") -> None:
        self.capabilities[name] = Capability(name, configured, reason)
        if not configured:
            log.info("capability unavailable", capability=name, reason=reason)

    def build_core(self) -> None:
        """Construct everything that only needs Postgres and configuration."""
        s = self.settings
        self.db = Database(s)
        self.incidents = IncidentRepository(self.db)
        self.evidence = EvidenceStore(self.db)
        self.validator = EvidenceValidator(self.evidence)
        self.audit = AuditLog(self.db)
        self.actions = ActionRepository(self.db)
        self.policy = PolicyStore(self.db)
        self.approvals = ApprovalStore(
            self.db, self.audit, ttl_seconds=s.approval_ttl_seconds
        )
        self.leases = LeaseManager(
            self.db, self.audit, default_ttl_seconds=s.resource_lease_ttl_seconds
        )
        self.gate = ActionGate(
            settings=s,
            actions=self.actions,
            approvals=self.approvals,
            leases=self.leases,
            policy_store=self.policy,
            evidence_validator=self.validator,
            audit=self.audit,
        )
        self.prometheus = PrometheusClient(s)
        self.router = ModelRouter(s)
        self.neo4j = Neo4jClient(s)
        self.sandbox_runs = SandboxRunRepository(self.db)
        self.patches = PatchRepository(self.db)
        self.deployments = DeploymentRepository(self.db)
        self.sandbox = RecordingSandboxRunner(s, self.sandbox_runs)
        self.verification_store = VerificationStore(self.db)

    def build_optional(self) -> None:
        """Construct every soft dependency, recording why each is or is not usable.

        Each block is isolated. One integration failing to construct must not
        prevent the others from being available - a broken Slack credential
        should not cost the operator their service graph.
        """
        s = self.settings

        with contextlib.suppress(Exception):
            from aegis.telemetry.tempo import TempoClient

            self.tempo = TempoClient(s)
        self._mark("tempo", self.tempo is not None, "" if self.tempo else "not constructed")

        with contextlib.suppress(Exception):
            from aegis.telemetry.loki import LokiClient

            self.loki = LokiClient(s)
        self._mark("loki", self.loki is not None, "" if self.loki else "not constructed")

        try:
            from aegis.integrations.github import GitHubClient

            self.github = GitHubClient(s)
            ok = bool(getattr(self.github, "configured", False))
            self._mark("github", ok, "" if ok else "no github token is configured")
        except Exception as exc:  # noqa: BLE001 - optional integration
            self._mark("github", False, f"{type(exc).__name__}: {exc}")

        try:
            from aegis.integrations.slack import SlackClient

            self.slack = SlackClient(s)
            ok = bool(getattr(self.slack, "configured", False))
            self._mark("slack", ok, "" if ok else "no slack token or webhook configured")
        except Exception as exc:  # noqa: BLE001
            self._mark("slack", False, f"{type(exc).__name__}: {exc}")

        try:
            from aegis.integrations.langsmith import LangSmithIntegration

            self.langsmith = LangSmithIntegration(s)
            ok = bool(getattr(self.langsmith, "configured", False))
            self._mark("langsmith", ok, "" if ok else "tracing is disabled")
        except Exception as exc:  # noqa: BLE001
            self._mark("langsmith", False, f"{type(exc).__name__}: {exc}")

        try:
            from aegis.retrieval.documents import DocumentStore
            from aegis.retrieval.embeddings import EmbeddingClient
            from aegis.retrieval.hybrid import HybridRetriever

            self.embeddings = EmbeddingClient(s)
            ok = bool(getattr(self.embeddings, "configured", False))
            self._mark(
                "embeddings",
                ok,
                "" if ok else "no embedding provider configured; retrieval is lexical-only",
            )
            usable = self.embeddings if ok else None
            self.documents = DocumentStore(self.db, usable)
            self.retriever = HybridRetriever(self.db, usable)
        except Exception as exc:  # noqa: BLE001
            self._mark("embeddings", False, f"{type(exc).__name__}: {exc}")

        try:
            from aegis.retrieval.code import CodeRetriever

            self.code = CodeRetriever(
                self.db, self.retriever, commit_source=self._commit_source()
            )
            self._mark("code_retrieval", self.retriever is not None)
        except Exception as exc:  # noqa: BLE001
            self._mark("code_retrieval", False, f"{type(exc).__name__}: {exc}")

        try:
            from aegis.memory.recall import IncidentMemoryRecall
            from aegis.memory.store import IncidentMemoryStore

            self.memory_store = IncidentMemoryStore(self.db, embeddings=self.embeddings)
            self.memory_recall = IncidentMemoryRecall(self.db, self.retriever)
            self._mark("incident_memory", True)
        except Exception as exc:  # noqa: BLE001
            self._mark("incident_memory", False, f"{type(exc).__name__}: {exc}")

        try:
            from aegis.graph.graphrag import GraphRAG
            from aegis.graph.ingest import TopologyIngestor
            from aegis.graph.traversal import GraphTraversal

            self.topology = GraphTraversal(self.neo4j)
            self.graphrag = GraphRAG(self.topology)
            self.graph_ingest = TopologyIngestor(self.neo4j)
            self._mark("graph", True)
        except Exception as exc:  # noqa: BLE001
            self._mark("graph", False, f"{type(exc).__name__}: {exc}")

        try:
            from aegis.integrations.runtime import get_adapter

            self.runtime_adapter = get_adapter(s)
            self.runtime = RuntimePortBridge(self.runtime_adapter, db=self.db)
            ok = bool(self.runtime.available)
            self._mark("runtime", ok, "" if ok else self.runtime.unavailable_reason)
        except Exception as exc:  # noqa: BLE001
            self._mark("runtime", False, f"{type(exc).__name__}: {exc}")

        self._mark(
            "sandbox",
            s.sandbox_enabled,
            "" if s.sandbox_enabled else "SANDBOX_ENABLED is false",
        )

    def build_horizon(self) -> None:
        """Construct the step loop's collaborators, each with its fallback.

        Built in both processes: the worker drives incidents with them, and the
        API reports their readiness on the war-room page. Construction performs
        no I/O - the RawTree writer task is started by whichever process owns
        the write path (the worker), never by the API.
        """
        s = self.settings

        try:
            from aegis.persistence.horizon import PostgresHorizonStore

            self.horizon_store = PostgresHorizonStore(self.db)
            self._mark("horizon_store", True)
        except Exception as exc:  # noqa: BLE001 - reported, the loop falls back
            self._mark("horizon_store", False, f"{type(exc).__name__}: {exc}")

        try:
            from aegis.agents.brain.router import BrainRouter
            from aegis.agents.horizon.scripted import ScriptedBrain

            self.brain = BrainRouter(s, scripted=ScriptedBrain())
            self._mark("brain", True, f"mode={s.aegis_mode}")
            bedrock_ok = bool(s.bedrock_model_id and s.aws_region)
            self._mark(
                "bedrock",
                bedrock_ok,
                "" if bedrock_ok else "BEDROCK_MODEL_ID or AWS_REGION is not set",
            )
            gemini_ok = bool(s.gemini_pool_keys("brain"))
            self._mark("gemini", gemini_ok, "" if gemini_ok else "brain key pool is empty")
        except Exception as exc:  # noqa: BLE001
            self._mark("brain", False, f"{type(exc).__name__}: {exc}")

        try:
            from aegis.agents.brain.compactor_llm import GeminiCompactorLLM

            self.compactor_llm = GeminiCompactorLLM(s)
            ok = bool(self.compactor_llm.configured)
            self._mark(
                "compactor_llm",
                ok,
                "" if ok else "compactor key pool is empty; rule compactor only",
            )
        except Exception as exc:  # noqa: BLE001
            self._mark("compactor_llm", False, f"{type(exc).__name__}: {exc}")

        try:
            from aegis.integrations.rawtree import RawTreeClient
            from aegis.integrations.rawtree_mcp import RawTreeAgentTools

            self.rawtree = RawTreeClient(s, fallback_store=self.horizon_store, db=self.db)
            self.rawtree_tools = RawTreeAgentTools(s)
            ok = bool(self.rawtree.write_configured and self.rawtree.read_configured)
            self._mark(
                "rawtree",
                ok,
                "" if ok else "RawTree keys missing; heartbeat uses z-score, history uses Postgres",
            )
        except Exception as exc:  # noqa: BLE001
            self._mark("rawtree", False, f"{type(exc).__name__}: {exc}")

        try:
            from aegis.integrations.nimble import NimbleKnownIssues

            self.known_issues = NimbleKnownIssues(s)
            ok = bool(s.nimble_api_key.get_secret_value())
            self._mark("nimble", ok, "" if ok else "NIMBLE_API_KEY missing; fixture is used")
        except Exception as exc:  # noqa: BLE001
            self._mark("nimble", False, f"{type(exc).__name__}: {exc}")

        try:
            from aegis.integrations.bfl import FluxIncidentMap

            self.incident_map = FluxIncidentMap(s)
            ok = bool(s.bfl_api_key.get_secret_value())
            self._mark("flux", ok, "" if ok else "BFL_API_KEY missing; incident map skipped")
        except Exception as exc:  # noqa: BLE001
            self._mark("flux", False, f"{type(exc).__name__}: {exc}")

    def _commit_source(self) -> Any:
        """Adapt the GitHub client onto the narrow interface code retrieval needs.

        Returns ``None`` when GitHub is unconfigured, which makes code
        localisation report itself degraded rather than silently searching an
        empty corpus and concluding that nothing changed.
        """
        if self.github is None or not getattr(self.github, "configured", False):
            return None
        from aegis.integrations.commit_source import GitHubCommitSource

        return GitHubCommitSource(self.github)

    def build_execution(self) -> None:
        """Assemble the verification engine, execution ports and service.

        Built after the optional pass because the ports depend on whichever
        runtime adapter and cache turned out to be available.
        """
        self.verification = VerificationEngine(
            prometheus=self.prometheus,
            evidence=self.evidence,
            runtime=self.runtime,
        )
        self.execution = ExecutionService(
            actions=self.actions,
            audit=self.audit,
            leases=self.leases,
            verification=self.verification,
            verification_store=self.verification_store,
            deployments=self.deployments,
        )
        if self.runtime is not None:
            self.ports = ExecutionPorts(
                runtime_read=self.runtime,
                runtime_write=self.runtime,
                cache=RedisCachePort(self.redis) if self.redis is not None else None,
                timeout_s=60.0,
            )
        self.build_tools()

    def build_tools(self) -> None:
        """Freeze the tool catalogue and build the one invoker every caller uses.

        Built last, because the catalogue binds the components the optional and
        execution passes produced: a tool whose dependency is ``None`` stays in
        the catalogue and answers ``degraded`` with a reason, which is a
        different answer from "found nothing" all the way into the UI.

        The registry is frozen by ``default_registry``, so after this returns no
        code path can add a tool. That is what makes the catalogue a security
        boundary rather than a convention.
        """
        try:
            deps = ToolDeps(
                evidence=self.evidence,
                prometheus=self.prometheus,
                tempo=self.tempo,
                loki=self.loki,
                traversal=self.topology,
                graphrag=self.graphrag,
                retriever=self.retriever,
                code=self.code,
                memory=self.memory_recall,
                github=self.github,
                runtime=self.runtime_adapter,
                actions=self.actions,
                approvals=self.approvals,
                execution=self.execution,
                ports=self.ports,
                sandbox=self.sandbox if self.settings.sandbox_enabled else None,
                sandbox_image=self.settings.sandbox_image,
            )
            registry = default_registry(deps)
        except Exception as exc:  # noqa: BLE001 - a broken catalogue must not crash boot
            # Fail closed rather than fail open: no invoker means no tool call
            # succeeds, and every caller records an evidence gap saying so.
            self.tool_registry = None
            self.tools = None
            self._mark("tools", False, f"{type(exc).__name__}: {exc}")
            log.error("tool catalogue could not be built", error=str(exc))
            return

        self.tool_registry = registry
        self.tools = ToolInvoker(registry, db=self.db, audit=self.audit)
        self._mark(
            "tools",
            True,
            f"{len(registry)} tools ({len(registry.read_tools())} read, "
            f"{len(registry.write_tools())} write)",
        )

    async def connect(self) -> None:
        """Open the connections that must exist before serving traffic.

        Postgres is the only hard requirement, so it is the only thing that
        raises. Redis and Neo4j failures are recorded and the process continues
        - losing the event stream degrades the live UI, it does not make the
        control plane unsafe.
        """
        await self.db.connect()

        try:
            import redis.asyncio as aioredis

            self.redis = aioredis.from_url(
                self.settings.redis_url, decode_responses=True
            )
            await self.redis.ping()
            self._mark("redis", True)
        except Exception as exc:  # noqa: BLE001 - redis is never authoritative
            log.warning("redis unavailable; live stream degraded", error=str(exc))
            self.redis = None
            self._mark("redis", False, f"{type(exc).__name__}: {exc}")

        self.build_execution()

        await self.ensure_graph_schema()

    async def ensure_graph_schema(self) -> bool:
        """Apply the graph schema and report whether topology is usable now.

        Neo4j accepts connections before it accepts writes, so a container
        that boots alongside it loses this race routinely. Retrying with
        bounded backoff covers that window.

        It is also safe to call again later. The capability is re-evaluated on
        every call rather than latched at boot: a process that runs for months
        must not report topology as permanently unavailable because Neo4j was
        slow to start once. Schema application is idempotent (every statement
        is IF NOT EXISTS), so repeating it costs nothing.
        """
        if self.graph_ingest is None:
            return False
        try:
            await retry_async(
                self.graph_ingest.ensure_schema,
                attempts=4,
                base_delay=1.0,
                max_delay=8.0,
                what="neo4j schema",
            )
        except Exception as exc:  # noqa: BLE001 - neo4j is a soft dependency
            log.warning("neo4j schema not applied", error=str(exc))
            self._mark("graph", False, f"{type(exc).__name__}: {exc}")
            return False
        self._mark("graph", True)
        return True

    async def aclose(self) -> None:
        """Release everything. Runs on shutdown and must never raise."""
        for closer in (
            getattr(self.prometheus, "close", None),
            getattr(self.neo4j, "close", None),
            getattr(self.tempo, "close", None),
            getattr(self.loki, "close", None),
            getattr(self.github, "close", None),
            getattr(self.embeddings, "aclose", None),
            getattr(self.sandbox, "close", None),
            getattr(self.compactor_llm, "aclose", None),
            getattr(self.brain, "aclose", None),
        ):
            if closer is None:
                continue
            with contextlib.suppress(Exception):
                await closer()
        if self.redis is not None:
            with contextlib.suppress(Exception):
                await self.redis.aclose()
        with contextlib.suppress(Exception):
            await self.db.close()

    def workflow_deps(self, budget: Any) -> Any:
        """Build the per-investigation dependency bundle.

        A fresh ``WorkflowDeps`` per run, because ``BudgetGuard`` and the
        pending-action handoff are both run-scoped. Sharing one across
        investigations would let a long incident consume another's budget.
        """
        from aegis.agents.workflow import WorkflowDeps

        return WorkflowDeps(
            settings=self.settings,
            db=self.db,
            evidence=self.evidence,
            prometheus=self.prometheus,
            neo4j=self.neo4j,
            router=self.router,
            budget=budget,
            redis=self.redis,
            graphrag=self.graphrag,
            topology=self.topology,
            retriever=self.retriever,
            code=self.code,
            memory_recall=self.memory_recall,
            memory_store=self.memory_store,
            github=self.github,
            gate=self.gate,
            executor=self.execution,
            ports=self.ports,
            sandbox=self.sandbox,
            audit=self.audit,
            # Without this the debug_remediation node has nowhere to record a
            # candidate patch, so it declines rather than generating one it
            # could not account for.
            patches=self.patches,
            slack=self.slack,
            tools=self.tools,
        )

    def capability_report(self) -> dict[str, dict[str, Any]]:
        """What is and is not available, for /health and the Integrations page."""
        return {
            name: {"configured": cap.configured, "reason": cap.reason}
            for name, cap in sorted(self.capabilities.items())
        }


def build_container(settings: Settings) -> Container:
    """Build the full object graph without opening any connection."""
    container = Container(settings=settings)
    container.build_core()
    container.build_optional()
    container.build_execution()
    container.build_horizon()
    return container


__all__ = ["Capability", "Container", "build_container"]
