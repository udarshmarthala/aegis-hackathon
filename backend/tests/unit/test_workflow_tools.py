"""The investigation reads telemetry through the tool boundary.

These tests exist because the difference between "the workflow called
Prometheus" and "the workflow called Prometheus *through the invoker*" is
invisible in a green demo and decides six evaluation metrics. What is asserted
is the trail the invoker leaves and the accounting it does:

* a ``tool_calls`` row per metric read, with the tool name, scope and access
  class recorded;
* the run's ``BudgetGuard`` charged exactly once per call - the invoker charges,
  the node does not charge again;
* a dead Prometheus still producing the same evidence gap the direct client call
  produced, with ``degraded`` recorded rather than an empty result;
* no invoker at all producing a gap rather than a silent skip.

Every collaborator is a fake; nothing here touches a live service.
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
from aegis.telemetry.prometheus import MetricPoint, MetricSeries

INCIDENT = "inc_test_0001"


@pytest.fixture(autouse=True)
def _clean_breakers() -> None:
    """Breaker state is process-global; one failing fake must not leak onward."""
    reset_breakers()


# --------------------------------------------------------------------------- #
# fakes                                                                        #
# --------------------------------------------------------------------------- #


class FakeDB:
    """Captures every insert so the tool_calls rows can be inspected."""

    def __init__(self) -> None:
        self.statements: list[tuple[str, tuple[Any, ...]]] = []

    async def execute(self, query: str, *args: Any) -> str:
        self.statements.append((query, args))
        return "INSERT 0 1"

    @property
    def tool_calls(self) -> list[dict[str, Any]]:
        columns = [
            "id", "agent_run_id", "incident_id", "server", "tool", "access", "scope",
            "environment", "caller", "correlation_id", "arguments", "ok",
            "result_summary", "error", "duration_ms", "degraded", "degraded_reason",
            "evidence_ids",
        ]
        return [
            dict(zip(columns, args, strict=True))
            for query, args in self.statements
            if "INSERT INTO tool_calls" in query
        ]


class FakeEvidenceStore:
    """An in-memory evidence store with the real store's two-state contract."""

    def __init__(self) -> None:
        self.items: dict[str, EvidenceItem] = {}
        self._seq = 0

    def _next_id(self) -> str:
        self._seq += 1
        return f"ev_{self._seq:04d}"

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
        item = EvidenceItem(
            id=self._next_id(),
            incident_id=incident_id,
            source=source,
            source_type=source_type,
            evidence_type=evidence_type,
            retrieved_at=datetime.now(UTC),
            observed_at=observed_at,
            resource_id=resource_id,
            summary=summary,
            structured_value=structured_value or {},
            content=content,
            provenance_uri=provenance_uri,
            trust_class=TrustClass.TIER_D if untrusted else trust_for(source_type),
            status=status,
        )
        self.items[item.id] = item
        return item

    async def record_unavailable(
        self, *, incident_id: str, source: str, source_type: SourceType, reason: str
    ) -> EvidenceItem:
        return await self.record(
            incident_id=incident_id,
            source=source,
            source_type=source_type,
            evidence_type=EvidenceType.EVIDENCE_GAP,
            summary=f"{source} unavailable: {reason}",
            structured_value={"reason": reason},
            provenance_uri=f"gap://{source}",
            status=EvidenceStatus.SOURCE_UNAVAILABLE,
        )

    async def get_many(self, evidence_ids: list[str]) -> dict[str, EvidenceItem]:
        return {eid: self.items[eid] for eid in evidence_ids if eid in self.items}

    @property
    def gaps(self) -> list[EvidenceItem]:
        return [
            i for i in self.items.values()
            if i.status is EvidenceStatus.SOURCE_UNAVAILABLE
        ]


def _series(metric: str, service: str) -> list[MetricSeries]:
    return [
        MetricSeries(
            metric=metric,
            labels={"service": service},
            points=[MetricPoint(timestamp=1.0, value=0.1), MetricPoint(timestamp=2.0, value=0.4)],
            query=f"promql:{metric}{{service={service}}}",
        )
    ]


class FakePrometheus:
    """Answers the three templated queries the investigation asks for."""

    def __init__(self, *, down: bool = False, services: list[str] | None = None) -> None:
        self.down = down
        self.services = services if services is not None else ["checkout"]
        self.calls: list[tuple[str, str, int]] = []

    def _fetch(self, metric: str, service: str, window_s: int) -> list[MetricSeries]:
        if self.down:
            raise SourceUnavailable("prometheus is unreachable")
        self.calls.append((metric, service, window_s))
        return _series(metric, service)

    async def error_rate(self, service: str, window_s: int = 900) -> list[MetricSeries]:
        return self._fetch("error_rate", service, window_s)

    async def latency_p99(self, service: str, window_s: int = 900) -> list[MetricSeries]:
        return self._fetch("latency_p99", service, window_s)

    async def request_rate(self, service: str, window_s: int = 900) -> list[MetricSeries]:
        return self._fetch("request_rate", service, window_s)

    # Saturation templates. A fake that implements less than the real client is
    # not a fake, it is a different object - and the gap shows up as a tool
    # failure attributed to an unreachable source.
    async def cpu_utilisation(self, service: str, window_s: int = 900) -> list[MetricSeries]:
        return self._fetch("cpu_utilisation", service, window_s)

    async def memory_bytes(self, service: str, window_s: int = 900) -> list[MetricSeries]:
        return self._fetch("memory_bytes", service, window_s)

    async def pool_saturation(self, service: str, window_s: int = 900) -> list[MetricSeries]:
        return self._fetch("pool_saturation", service, window_s)

    async def queue_depth(self, service: str, window_s: int = 900) -> list[MetricSeries]:
        return self._fetch("queue_depth", service, window_s)

    async def cache_hit_ratio(self, service: str, window_s: int = 900) -> list[MetricSeries]:
        return self._fetch("cache_hit_ratio", service, window_s)

    async def restart_count(self, service: str, window_s: int = 900) -> list[MetricSeries]:
        return self._fetch("restart_count", service, window_s)

    async def known_services(self) -> list[str]:
        if self.down:
            raise SourceUnavailable("prometheus is unreachable")
        return list(self.services)


class DeadGraph:
    """Neo4j that is always down, so topology contributes a gap and nothing else."""

    async def run(self, cypher: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        del cypher, params
        raise SourceUnavailable("neo4j is unreachable")


class UnusedRouter:
    """The investigate node consults no model; calling one would be the bug."""

    async def structured(self, **kwargs: Any) -> tuple[Any, dict[str, Any]]:
        raise AssertionError(f"investigate must not call an LLM: {kwargs}")


# --------------------------------------------------------------------------- #
# assembly                                                                     #
# --------------------------------------------------------------------------- #


def build(
    *,
    prometheus: FakePrometheus | None = None,
    with_tools: bool = True,
) -> tuple[dict[str, Any], FakeDB, FakeEvidenceStore, BudgetGuard]:
    prom = prometheus or FakePrometheus()
    evidence = FakeEvidenceStore()
    db = FakeDB()
    budget = BudgetGuard(
        max_wall_seconds=300, max_llm_calls=10, max_tool_calls=50, max_tokens=100_000
    )

    invoker: ToolInvoker | None = None
    if with_tools:
        registry = default_registry(ToolDeps(evidence=evidence, prometheus=prom))
        invoker = ToolInvoker(registry, db=db)

    deps = WorkflowDeps(
        settings=Settings(postgres_password="x"),
        db=db,
        evidence=evidence,
        prometheus=prom,
        neo4j=DeadGraph(),
        router=UnusedRouter(),
        budget=budget,
        tools=invoker,
    )
    return make_nodes(deps), db, evidence, budget


def state(services: list[str] | None = None) -> IncidentState:
    return {
        "incident_id": INCIDENT,
        "correlation_id": "corr_test_0001",
        "environment": "local",
        "title": "checkout error rate elevated",
        "candidate_services": services if services is not None else ["checkout"],
        "evidence_ids": [],
        "evidence_summaries": [],
        "evidence_gaps": [],
        "loop_count": 0,
    }


# --------------------------------------------------------------------------- #
# tests                                                                        #
# --------------------------------------------------------------------------- #


async def test_investigate_writes_a_tool_call_row_per_metric_read() -> None:
    nodes, db, _evidence, _budget = build()

    result = await nodes["investigate"](state())

    rows = db.tool_calls
    assert [r["tool"] for r in rows] == ["query_metric_range"] * 3
    assert {r["access"] for r in rows} == {"read"}
    assert {r["scope"] for r in rows} == {"telemetry:metrics"}
    assert {r["incident_id"] for r in rows} == {INCIDENT}
    assert {r["correlation_id"] for r in rows} == {"corr_test_0001"}
    assert {r["environment"] for r in rows} == {"local"}
    assert all(r["ok"] for r in rows)
    assert not any(r["degraded"] for r in rows)
    # Every row cites the evidence it produced, which is the join that makes a
    # citation traceable back to the exact invocation behind it.
    assert all(r["evidence_ids"] for r in rows)
    assert len(result["evidence_ids"]) >= 3


async def test_each_metric_in_the_closed_catalogue_is_read_once() -> None:
    nodes, db, _evidence, _budget = build()

    await nodes["investigate"](state())

    metrics = [r["arguments"]["metric"] for r in db.tool_calls]
    assert metrics == ["error_rate", "latency_p99", "request_rate"]
    assert {r["arguments"]["window_s"] for r in db.tool_calls} == {900}


async def test_the_budget_is_charged_once_per_call_not_twice() -> None:
    """The invoker charges. A node that charged as well would halve the budget."""
    nodes, _db, _evidence, budget = build()

    before = budget.view().tool_calls_remaining
    await nodes["investigate"](state())
    spent = before - budget.view().tool_calls_remaining

    # Three metric reads through the invoker, plus the one direct topology read
    # that has no tool equivalent and charges itself.
    assert spent == 4


async def test_evidence_summaries_carry_trust_and_status() -> None:
    nodes, _db, _evidence, _budget = build()

    result = await nodes["investigate"](state())

    metric_summaries = [
        s for s in result["evidence_summaries"] if s["source"] == "prometheus"
    ]
    assert metric_summaries
    for summary in metric_summaries:
        assert summary["trust_class"]
        assert summary["status"] == EvidenceStatus.UNVALIDATED.value


async def test_a_dead_source_records_a_gap_and_stops_probing() -> None:
    """Degraded is not empty. The gap must survive into the state and the row."""
    nodes, db, evidence, _budget = build(prometheus=FakePrometheus(down=True))

    result = await nodes["investigate"](state())

    gaps = [g for g in result["evidence_gaps"] if g["source"] == "prometheus"]
    assert gaps, "a dead Prometheus must leave an evidence gap"
    assert "unreachable" in gaps[0]["reason"]

    rows = [r for r in db.tool_calls if r["tool"] == "query_metric_range"]
    # One attempt, then the source is treated as down rather than re-probed.
    assert len(rows) == 1
    assert rows[0]["degraded"] is True
    assert rows[0]["degraded_reason"]

    stored = [
        i for i in evidence.gaps
        if i.source == "prometheus" and i.source_type is SourceType.METRICS
    ]
    assert stored, "the gap must be persisted, not only held in state"


async def test_without_a_tool_boundary_the_node_fails_closed() -> None:
    """No invoker means no authorised read - and an explicit gap, not silence."""
    nodes, db, evidence, budget = build(with_tools=False)

    before = budget.view().tool_calls_remaining
    result = await nodes["investigate"](state())

    assert db.tool_calls == []
    reasons = [g["reason"] for g in result["evidence_gaps"] if g["source"] == "prometheus"]
    assert reasons and "tool boundary" in reasons[0]
    assert any(i.status is EvidenceStatus.SOURCE_UNAVAILABLE for i in evidence.gaps)
    # Only the direct topology read charged; nothing pretended to call a tool.
    assert before - budget.view().tool_calls_remaining == 1


async def test_a_service_with_no_series_is_a_finding_not_a_gap() -> None:
    """Prometheus answering "no such series" is evidence, and says so."""

    class EmptyPrometheus(FakePrometheus):
        def _fetch(self, metric: str, service: str, window_s: int) -> list[MetricSeries]:
            self.calls.append((metric, service, window_s))
            return []

    nodes, db, evidence, _budget = build(prometheus=EmptyPrometheus())

    result = await nodes["investigate"](state())

    assert len(db.tool_calls) == 3
    assert all(r["ok"] and not r["degraded"] for r in db.tool_calls)
    no_data = [
        i for i in evidence.items.values()
        if i.source == "prometheus" and "no data in window" in i.summary
    ]
    assert len(no_data) == 3
    assert all(i.status is EvidenceStatus.UNVALIDATED for i in no_data)
    assert not [g for g in result["evidence_gaps"] if g["source"] == "prometheus"]
