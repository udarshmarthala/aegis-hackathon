"""The ``recall_memory`` node.

This node previously read ``result.items`` while ``RecallResult`` exposes
``matches``. Guarded by ``getattr(result, "items", None)``, that mismatch never
raised: precedent was never surfaced, evidence was never recorded, and the
investigation reported "no matching prior incident" - the exact conflation of
"found nothing" with "could not look" that CLAUDE.md invariant 6 forbids.

So these tests assert the two outcomes that were indistinguishable:

* memory answered and held precedent -> it reaches ``history`` and evidence
* memory could not be searched       -> an evidence gap, and nothing that reads
                                        as an absence of precedent

They drive the real node through the real tool registry and invoker, with fakes
only at the edges, so a future rename of a field on either side of that boundary
fails here rather than silently returning nothing again.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from aegis.agents.state import BudgetGuard, IncidentState
from aegis.agents.workflow import WorkflowDeps, make_nodes
from aegis.core.config import Settings
from aegis.core.errors import SourceUnavailable
from aegis.core.resilience import reset_breakers
from aegis.domain.enums import EvidenceStatus, EvidenceType, SourceType, TrustClass
from aegis.domain.models import EvidenceItem
from aegis.evidence.store import trust_for
from aegis.mcp import ToolDeps, ToolInvoker, default_registry
from aegis.memory.recall import MemoryMatch, RecallResult
from aegis.memory.store import IncidentMemory

INCIDENT = "inc_recall_0001"


@pytest.fixture(autouse=True)
def _clean_breakers() -> None:
    reset_breakers()


# --------------------------------------------------------------------------- #
# fakes                                                                        #
# --------------------------------------------------------------------------- #


class FakeDB:
    """Captures the tool_calls rows the invoker writes."""

    def __init__(self) -> None:
        self.tool_calls: list[tuple[Any, ...]] = []

    async def execute(self, query: str, *args: Any) -> str:
        if "INSERT INTO tool_calls" in query:
            self.tool_calls.append(args)
        return "INSERT 0 1"

    async def fetchrow(self, query: str, *args: Any) -> None:
        del query, args
        return

    async def fetch(self, query: str, *args: Any) -> list[Any]:
        del query, args
        return []


class FakeEvidenceStore:
    def __init__(self) -> None:
        self.items: list[EvidenceItem] = []

    def _add(
        self,
        *,
        source: str,
        source_type: SourceType,
        evidence_type: EvidenceType,
        summary: str,
        status: EvidenceStatus,
        structured_value: dict[str, Any] | None = None,
        provenance_uri: str = "",
    ) -> EvidenceItem:
        item = EvidenceItem(
            id=f"ev_{len(self.items):04d}",
            incident_id=INCIDENT,
            source=source,
            source_type=source_type,
            evidence_type=evidence_type,
            retrieved_at=datetime.now(UTC),
            summary=summary,
            structured_value=structured_value or {},
            provenance_uri=provenance_uri,
            trust_class=(
                TrustClass.TIER_D
                if status is EvidenceStatus.SOURCE_UNAVAILABLE
                else trust_for(source_type)
            ),
            status=status,
        )
        self.items.append(item)
        return item

    async def record(
        self,
        *,
        incident_id: str,
        source: str,
        source_type: SourceType,
        evidence_type: EvidenceType,
        summary: str,
        structured_value: dict[str, Any] | None = None,
        provenance_uri: str = "",
        **_: Any,
    ) -> EvidenceItem:
        del incident_id
        return self._add(
            source=source,
            source_type=source_type,
            evidence_type=evidence_type,
            summary=summary,
            status=EvidenceStatus.UNVALIDATED,
            structured_value=structured_value,
            provenance_uri=provenance_uri,
        )

    async def record_unavailable(
        self, *, incident_id: str, source: str, source_type: SourceType, reason: str
    ) -> EvidenceItem:
        del incident_id
        return self._add(
            source=source,
            source_type=source_type,
            evidence_type=EvidenceType.EVIDENCE_GAP,
            summary=f"{source} unavailable: {reason}",
            status=EvidenceStatus.SOURCE_UNAVAILABLE,
            structured_value={"reason": reason},
        )

    @property
    def gaps(self) -> list[EvidenceItem]:
        return [i for i in self.items if i.status is EvidenceStatus.SOURCE_UNAVAILABLE]

    @property
    def historical(self) -> list[EvidenceItem]:
        return [
            i for i in self.items if i.evidence_type is EvidenceType.HISTORICAL_INCIDENT
        ]


def _memory(memory_id: str, *, title: str, root_cause: str) -> IncidentMemory:
    return IncidentMemory(
        id=memory_id,
        incident_id="inc_prior_0001",
        title=title,
        symptoms="checkout returns 5xx",
        root_cause=root_cause,
        cause_category="dependency_failure",
        affected_services=("checkout", "payment"),
        contributing_factors=(),
        successful_fix="rolled payment back to v1.4.2",
        failed_attempts=(),
        verification="error rate returned to baseline",
        verification_passed=True,
        prevention="",
        follow_ups=(),
        related_commits=(),
        related_deployments=(),
        timeline=(),
        evidence_ids=(),
        fingerprint="v1:abc123",
        occurrences=3,
        approved=True,
        approved_by="user_alex",
        diagnosis_confidence=0.88,
    )


class FakeMemory:
    """Incident memory, scripted to answer, to be degraded, or to be down."""

    def __init__(
        self,
        *,
        matches: tuple[MemoryMatch, ...] = (),
        degraded: bool = False,
        degraded_reason: str = "",
        raises: Exception | None = None,
    ) -> None:
        self._result = RecallResult(
            matches=matches, degraded=degraded, degraded_reason=degraded_reason
        )
        self._raises = raises
        self.calls = 0

    async def similar(self, symptom: str, **kwargs: Any) -> RecallResult:
        del symptom, kwargs
        self.calls += 1
        if self._raises is not None:
            raise self._raises
        return self._result


class DeadGraph:
    async def run(self, cypher: str, params: dict[str, Any]) -> list[Any]:
        del cypher, params
        raise SourceUnavailable("neo4j is not available in this test")


class UnusedRouter:
    async def structured(self, **kwargs: Any) -> Any:
        del kwargs
        raise AssertionError("recall_memory must not call a model")


def build(
    *, memory: FakeMemory | None = None, with_tools: bool = True
) -> tuple[dict[str, Any], FakeDB, FakeEvidenceStore]:
    evidence = FakeEvidenceStore()
    db = FakeDB()
    invoker: ToolInvoker | None = None
    if with_tools:
        registry = default_registry(ToolDeps(evidence=evidence, memory=memory))
        invoker = ToolInvoker(registry, db=db)

    deps = WorkflowDeps(
        settings=Settings(postgres_password="x"),
        db=db,
        evidence=evidence,
        prometheus=None,
        neo4j=DeadGraph(),
        router=UnusedRouter(),
        budget=BudgetGuard(
            max_wall_seconds=300, max_llm_calls=10, max_tool_calls=50, max_tokens=100_000
        ),
        memory_recall=memory,
        tools=invoker,
    )
    return make_nodes(deps), db, evidence


def state() -> IncidentState:
    return {
        "incident_id": INCIDENT,
        "correlation_id": "corr_recall_0001",
        "environment": "local",
        "title": "checkout error rate elevated",
        "affected_services": ["checkout"],
        "evidence_ids": [],
        "evidence_summaries": [],
        "evidence_gaps": [],
        "loop_count": 0,
    }


MATCH = MemoryMatch(
    memory=_memory(
        "mem_0001",
        title="checkout 5xx after payment deploy",
        root_cause="payment v1.4.3 exhausted its connection pool",
    ),
    confidence=0.82,
    match_type="signature",
    reason="same recurrence fingerprint and shared service",
    provenance_uri="memory://mem_0001",
    shared_services=("checkout",),
)


# --------------------------------------------------------------------------- #
# precedent found                                                              #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_matches_reach_history_with_their_real_field_names() -> None:
    """The regression test for the bug: matches must actually arrive.

    Reading the fields explicitly is the point. A future rename on either side
    of the tool boundary must fail here rather than quietly producing an empty
    history again.
    """
    nodes, _db, _evidence = build(memory=FakeMemory(matches=(MATCH,)))

    result = await nodes["recall_memory"](state())

    assert len(result["history"]) == 1, "the match must reach the hypothesis prompt"
    entry = result["history"][0]
    assert entry["memory_id"] == "mem_0001"
    assert entry["title"] == "checkout 5xx after payment deploy"
    assert entry["root_cause"] == "payment v1.4.3 exhausted its connection pool"
    assert entry["cause_category"] == "dependency_failure"
    assert entry["successful_fix"] == "rolled payment back to v1.4.2"
    assert entry["match_type"] == "signature"
    assert entry["occurrences"] == 3
    assert entry["confidence"] == pytest.approx(0.82)
    assert entry["shared_services"] == ["checkout"]
    assert result["evidence_gaps"] == [], "a successful recall is not a gap"


@pytest.mark.asyncio
async def test_precedent_is_recorded_as_evidence() -> None:
    """A conclusion may only cite evidence that was actually stored."""
    nodes, _db, evidence = build(memory=FakeMemory(matches=(MATCH,)))

    result = await nodes["recall_memory"](state())

    assert result["evidence_ids"], "evidence ids must be returned for citation"
    recorded = evidence.historical
    assert len(recorded) == 1
    assert recorded[0].source_type is SourceType.MEMORY
    assert recorded[0].id in result["evidence_ids"]
    # Tier C: an approved human-authored lesson, never a direct observation.
    assert recorded[0].trust_class is TrustClass.TIER_C


@pytest.mark.asyncio
async def test_the_lookup_writes_a_tool_call_row() -> None:
    """Six evaluation metrics are computed from these rows."""
    nodes, db, _evidence = build(memory=FakeMemory(matches=(MATCH,)))

    await nodes["recall_memory"](state())

    tools = [row for row in db.tool_calls if "similar_incidents" in row]
    assert len(tools) == 1, "the node must reach memory through the tool boundary"


@pytest.mark.asyncio
async def test_an_empty_recall_is_not_reported_as_a_gap() -> None:
    """Memory answered and holds no precedent. That is a finding, not a failure."""
    nodes, _db, evidence = build(memory=FakeMemory(matches=()))

    result = await nodes["recall_memory"](state())

    assert result["history"] == []
    assert result["evidence_gaps"] == []
    assert evidence.gaps == [], "an empty answer must not masquerade as unavailability"


# --------------------------------------------------------------------------- #
# memory could not be searched                                                 #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_a_degraded_recall_produces_an_evidence_gap() -> None:
    """Degraded is not empty.

    The gap must reach the returned state, not only the evidence store: the
    hypothesis prompt is built from ``state["evidence_gaps"]``, so recording it
    in only one place leaves the model reading as though memory had been
    searched and found nothing.
    """
    nodes, _db, evidence = build(
        memory=FakeMemory(degraded=True, degraded_reason="vector index unavailable")
    )

    result = await nodes["recall_memory"](state())

    assert result["evidence_gaps"], "a degraded recall must surface a gap"
    gap = result["evidence_gaps"][0]
    assert gap["source"] == "incident_memory"
    assert "vector index unavailable" in gap["reason"]
    assert evidence.gaps, "the gap must also be recorded as evidence"
    assert evidence.gaps[0].status is EvidenceStatus.SOURCE_UNAVAILABLE


@pytest.mark.asyncio
async def test_an_unreachable_memory_store_produces_an_evidence_gap() -> None:
    nodes, _db, evidence = build(
        memory=FakeMemory(raises=SourceUnavailable("postgres is unreachable"))
    )

    result = await nodes["recall_memory"](state())

    assert result["history"] == []
    assert result["evidence_gaps"], "an unreachable store must surface a gap"
    assert evidence.gaps
    assert "unreachable" in result["evidence_gaps"][0]["reason"]


@pytest.mark.asyncio
async def test_without_a_tool_boundary_the_node_fails_closed() -> None:
    """No boundary is not a precedent of absence either."""
    nodes, _db, evidence = build(memory=FakeMemory(matches=(MATCH,)), with_tools=False)

    result = await nodes["recall_memory"](state())

    assert result["history"] == []
    assert result["evidence_gaps"], "an unconfigured boundary must surface a gap"
    assert evidence.gaps[0].status is EvidenceStatus.SOURCE_UNAVAILABLE
