"""Ablation arms must differ from the baseline, or the study measures nothing.

An ablation whose arm runs the same code as ``full`` produces a comparison
table of identical numbers and an unearned conclusion. Three of these arms were
in exactly that state: the flags existed, the names were asserted by a test, and
``apply()`` read none of them.

So nothing here asserts that a name exists. Each test asserts the *effect*: that
the enrichment nodes do not run under ``single_agent``, that the grounding gate
does not fire under ``no_verifier``, and - generically - that no flag on
``AblationConfig`` can be added again without something reading it.

Every collaborator is a fake; nothing here touches a live service or a model.
"""

from __future__ import annotations

from dataclasses import fields
from datetime import UTC, datetime
from typing import Any

import pytest

from aegis.agents.llm import LLMUnavailable
from aegis.agents.schemas import DiagnosisOut
from aegis.agents.state import BudgetGuard, IncidentState
from aegis.agents.workflow import (
    ENRICHMENT_NODES,
    WorkflowDeps,
    build_workflow,
    make_nodes,
    run_investigation,
)
from aegis.core.config import Settings
from aegis.core.errors import SourceUnavailable
from aegis.core.resilience import reset_breakers
from aegis.domain.enums import EvidenceStatus, EvidenceType, SourceType, TrustClass
from aegis.domain.models import EvidenceItem
from aegis.evaluation.ablations import AblationConfig, ablation_names, get_ablation
from aegis.mcp.types import ToolResult

INCIDENT = "inc_ablation_0001"


@pytest.fixture(autouse=True)
def _clean_breakers() -> None:
    """Breaker state is process-global; one failing fake must not leak onward."""
    reset_breakers()


# --------------------------------------------------------------------------- #
# fakes                                                                        #
# --------------------------------------------------------------------------- #


class FakeDB:
    """Swallows the writes the workflow makes for the timeline."""

    def __init__(self) -> None:
        self.statements: list[str] = []

    async def execute(self, query: str, *args: Any) -> str:
        del args
        self.statements.append(query)
        return "INSERT 0 1"


class FakeEvidenceStore:
    """In-memory store that records which sources were declared unavailable."""

    def __init__(self) -> None:
        self.items: dict[str, EvidenceItem] = {}
        self.unavailable_sources: list[str] = []
        self._seq = 0

    async def record(
        self,
        *,
        incident_id: str,
        source: str,
        source_type: SourceType,
        evidence_type: EvidenceType,
        summary: str,
        structured_value: dict[str, Any] | None = None,
        content: str | None = None,
        untrusted: bool = False,
        provenance_uri: str = "",
        resource_id: str | None = None,
        observed_at: datetime | None = None,
        status: EvidenceStatus = EvidenceStatus.UNVALIDATED,
    ) -> EvidenceItem:
        del content, untrusted
        self._seq += 1
        item = EvidenceItem(
            id=f"ev_{self._seq:04d}",
            incident_id=incident_id,
            source=source,
            source_type=source_type,
            evidence_type=evidence_type,
            retrieved_at=datetime.now(UTC),
            observed_at=observed_at,
            resource_id=resource_id,
            summary=summary,
            structured_value=structured_value or {},
            provenance_uri=provenance_uri,
            trust_class=TrustClass.TIER_B,
            status=status,
        )
        self.items[item.id] = item
        return item

    async def record_unavailable(
        self, *, incident_id: str, source: str, source_type: SourceType, reason: str
    ) -> EvidenceItem:
        self.unavailable_sources.append(source)
        return await self.record(
            incident_id=incident_id,
            source=source,
            source_type=source_type,
            evidence_type=EvidenceType.EVIDENCE_GAP,
            summary=f"{source} unavailable: {reason}",
            provenance_uri=f"gap://{source}",
            status=EvidenceStatus.SOURCE_UNAVAILABLE,
        )

    async def get_many(self, evidence_ids: list[str]) -> dict[str, EvidenceItem]:
        return {eid: self.items[eid] for eid in evidence_ids if eid in self.items}


class FakePrometheus:
    """Knows one service and answers nothing else; investigation reads it first."""

    async def known_services(self) -> list[str]:
        return ["checkout"]


class DeadGraph:
    """Neo4j that is always down: topology contributes a gap and nothing more."""

    async def run(self, cypher: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        del cypher, params
        raise SourceUnavailable("neo4j is unreachable")


class _GraphContext:
    services: list[str] = []
    edges: list[str] = []
    causal_paths: list[str] = []
    blast_radius = None


class CountingGraphRag:
    """Tripwire for analyze_topology: it can only be called if the node ran."""

    def __init__(self) -> None:
        self.expansions = 0

    async def expand(self, services: list[str], **kwargs: Any) -> _GraphContext:
        del services, kwargs
        self.expansions += 1
        return _GraphContext()

    async def to_evidence(self, store: Any, incident_id: str, context: Any) -> list[Any]:
        del store, incident_id, context
        return []


class _RecallResult:
    matches: list[Any] = []
    degraded = False
    degraded_reason = ""


class CountingMemory:
    """Tripwire for the recall module itself.

    Kept because the tool boundary reaches memory through it in production, even
    though ``recall_memory`` no longer calls it directly.
    """

    def __init__(self) -> None:
        self.lookups = 0

    async def similar(self, title: str, **kwargs: Any) -> _RecallResult:
        del title, kwargs
        self.lookups += 1
        return _RecallResult()


class CountingTools:
    """Tripwire for the tool boundary.

    ``recall_memory`` reaches incident memory through the invoker now, so the
    node running is observable here rather than on the recall module. Returning
    an empty, non-degraded result keeps the arm's behaviour identical to the
    previous fake: memory answered and held no precedent.
    """

    def __init__(self) -> None:
        self.invocations: list[str] = []

    @property
    def similar_lookups(self) -> int:
        return self.invocations.count("similar_incidents")

    async def invoke(self, tool: str, arguments: Any, context: Any, **kwargs: Any) -> Any:
        del arguments, context, kwargs
        self.invocations.append(tool)
        from aegis.mcp.tools.knowledge import SimilarIncidentsOutput

        return ToolResult(
            ok=True,
            tool=tool,
            call_id=f"tc_{len(self.invocations)}",
            duration_ms=1,
            value=SimilarIncidentsOutput(symptom="checkout error rate elevated"),
        )


class DownRouter:
    """Every model call fails.

    The investigation is designed to degrade rather than crash, so this drives a
    complete run - triage, investigate, hypothesize, an abstaining diagnosis -
    without a model and without the enrichment nodes being needed for the graph
    to terminate. That keeps the tripwires below measuring one thing only.
    """

    async def structured(self, **kwargs: Any) -> tuple[Any, dict[str, Any]]:
        del kwargs
        raise LLMUnavailable("no model is configured for this test")


class FixedRouter:
    """Returns one prepared structured output, whatever it is asked for."""

    def __init__(self, payload: Any) -> None:
        self.payload = payload

    async def structured(self, **kwargs: Any) -> tuple[Any, dict[str, Any]]:
        del kwargs
        return self.payload, {"model": "fake", "prompt_version": "test"}


# --------------------------------------------------------------------------- #
# assembly                                                                     #
# --------------------------------------------------------------------------- #


def build_deps(
    *,
    router: Any = None,
    graphrag: Any = None,
    memory_recall: Any = None,
    tools: Any = None,
) -> tuple[WorkflowDeps, FakeDB, FakeEvidenceStore]:
    db = FakeDB()
    evidence = FakeEvidenceStore()
    deps = WorkflowDeps(
        settings=Settings(postgres_password="x", agent_hypothesis_loop_limit=1),
        db=db,
        evidence=evidence,
        prometheus=FakePrometheus(),
        neo4j=DeadGraph(),
        router=router or DownRouter(),
        budget=BudgetGuard(
            max_wall_seconds=300, max_llm_calls=20, max_tool_calls=80, max_tokens=100_000
        ),
        graphrag=graphrag,
        memory_recall=memory_recall,
        tools=tools,
        github=None,   # analyze_changes records a "github" gap when it runs
    )
    return deps, db, evidence


async def run_arm(ablation: str) -> tuple[CountingGraphRag, CountingTools, FakeEvidenceStore]:
    """Run one complete investigation under an ablation and return the tripwires."""
    graphrag = CountingGraphRag()
    tools = CountingTools()
    deps, _db, evidence = build_deps(
        graphrag=graphrag, memory_recall=CountingMemory(), tools=tools
    )
    deps = get_ablation(ablation).apply(deps)

    state = await run_investigation(
        deps,
        incident_id=INCIDENT,
        title="checkout error rate elevated",
        severity="P2",
        environment="local",
        workload="reference",
        correlation_id="corr_ablation_0001",
    )
    assert state.get("finished") is True
    return graphrag, tools, evidence


def hypothesis_state() -> IncidentState:
    return {
        "incident_id": INCIDENT,
        "correlation_id": "corr_ablation_0002",
        "environment": "local",
        "title": "checkout error rate elevated",
        "evidence_ids": [],
        "evidence_summaries": [],
        "evidence_gaps": [],
        "hypotheses": [
            {
                "label": "h1",
                "confidence": 0.8,
                "statement": "the payment pool is exhausted",
                "supporting": ["ev_ghost"],
                "contradicting": [],
                "missing": [],
            }
        ],
        "loop_count": 1,
    }


UNGROUNDED = DiagnosisOut(
    abstain=False,
    statement="payment exhausted its connection pool",
    root_cause_category="connection_pool_exhaustion",
    selected_hypothesis_label="h1",
    # No such evidence item exists in the store, so the grounding gate must
    # reject it - unless the gate has been ablated away.
    supporting_evidence=["ev_ghost"],
    causal_path=["payment", "gateway"],
    affected_services=["payment", "gateway"],
)


# --------------------------------------------------------------------------- #
# single_agent: the fan-out must not happen                                    #
# --------------------------------------------------------------------------- #


async def test_the_full_arm_runs_every_enrichment_node() -> None:
    """The baseline for the test below: without it, an arm that ran nothing at
    all would look like a successful ablation."""
    graphrag, tools, evidence = await run_arm("full")

    assert graphrag.expansions >= 1
    assert tools.similar_lookups >= 1
    assert "github" in evidence.unavailable_sources


async def test_single_agent_never_runs_the_enrichment_nodes() -> None:
    graphrag, tools, evidence = await run_arm("single_agent")

    assert graphrag.expansions == 0, "analyze_topology ran under single_agent"
    assert tools.similar_lookups == 0, "recall_memory ran under single_agent"
    assert "github" not in evidence.unavailable_sources, "analyze_changes ran"


def test_single_agent_removes_the_nodes_from_the_compiled_graph() -> None:
    """Structural, not conditional: an unregistered node has no reachable edge,
    so the arm cannot fan out even if a future edge is added by mistake."""
    deps, _db, _evidence = build_deps()
    full_nodes = set(build_workflow(deps).get_graph().nodes)
    assert set(ENRICHMENT_NODES) <= full_nodes

    ablated = get_ablation("single_agent").apply(deps)
    single_nodes = set(build_workflow(ablated).get_graph().nodes)
    assert set(ENRICHMENT_NODES).isdisjoint(single_nodes)
    # The core path is intact: the arm is smaller, not broken.
    assert {"triage", "investigate", "hypothesize", "diagnose"} <= single_nodes


# --------------------------------------------------------------------------- #
# no_verifier: the grounding gate must not fire                                #
# --------------------------------------------------------------------------- #


async def test_the_grounding_gate_downgrades_an_ungrounded_diagnosis() -> None:
    deps, _db, _evidence = build_deps(router=FixedRouter(UNGROUNDED))
    nodes = make_nodes(deps)

    result = await nodes["diagnose"](hypothesis_state())

    assert result["abstained"] is True
    assert result["confidence"] == 0.0
    assert result["diagnosis"]["supporting_evidence"] == []
    assert result["evidence_verification"] == "validated"


async def test_no_verifier_lets_an_ungrounded_diagnosis_stand() -> None:
    deps, _db, _evidence = build_deps(router=FixedRouter(UNGROUNDED))
    deps = get_ablation("no_verifier").apply(deps)
    nodes = make_nodes(deps)

    result = await nodes["diagnose"](hypothesis_state())

    # The same model output, the same empty evidence store, a different answer:
    # that difference is the thing the ablation measures.
    assert result["abstained"] is False
    assert result["confidence"] > 0.0
    assert result["diagnosis"]["root_cause_category"] == "connection_pool_exhaustion"
    assert result["diagnosis"]["supporting_evidence"] == ["ev_ghost"]
    # And the run says so, so an unverified arm is never mistaken for a verified
    # one when the outcome is scored.
    assert result["evidence_verification"] == "skipped"


# --------------------------------------------------------------------------- #
# the configuration itself                                                     #
# --------------------------------------------------------------------------- #


def test_no_ablation_flag_is_decorative() -> None:
    """Every flag must change the dependencies. This is the guard that failed.

    ``no_verifier``, ``single_agent`` and ``no_execution_verification`` all set
    flags ``apply()`` never read. Adding another one is now a test failure
    rather than a silently identical arm.
    """
    ignored = {"name", "description"}
    flags = [f.name for f in fields(AblationConfig) if f.name not in ignored]
    assert flags, "AblationConfig has no flags left"

    for flag in flags:
        deps, _db, _evidence = build_deps()
        probe = AblationConfig(name="probe", description="", **{flag: False})
        assert probe.apply(deps) is not deps, f"{flag} is read by nothing"


def test_every_named_ablation_removes_what_it_claims_to() -> None:
    expected: dict[str, Any] = {
        "no_graph": lambda d: d.graphrag is None and d.topology is None,
        "no_rag": lambda d: d.retriever is None and d.code is None,
        "no_change_analysis": lambda d: d.github is None,
        "no_incident_memory": lambda d: d.memory_recall is None and d.memory_store is None,
        "single_agent": lambda d: d.single_agent is True,
        "no_verifier": lambda d: d.skip_evidence_verification is True,
    }
    assert set(ablation_names()) == {"full", *expected}

    for name, holds in expected.items():
        deps, _db, _evidence = build_deps()
        assert holds(get_ablation(name).apply(deps)), f"{name} changed nothing it claims"

    # The baseline touches nothing at all, so a "full" run is production's shape.
    deps, _db, _evidence = build_deps()
    assert get_ablation("full").apply(deps) is deps
    assert deps.single_agent is False
    assert deps.skip_evidence_verification is False


async def test_no_graph_leaves_a_client_that_reports_itself_unavailable() -> None:
    """Removing the graph must produce an evidence gap, not a crash: that is the
    same state production is in when Neo4j is down."""
    deps, _db, _evidence = build_deps()
    ablated = get_ablation("no_graph").apply(deps)

    assert await ablated.neo4j.healthy() is False
    with pytest.raises(SourceUnavailable):
        await ablated.neo4j.run("MATCH (n) RETURN n", {})
