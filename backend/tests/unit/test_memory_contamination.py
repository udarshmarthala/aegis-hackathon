"""The incident-memory contamination gate.

Memory is the only store that feeds its own future reads, so a bad write is not
a bad row - it is a bias applied to every subsequent investigation, invisible
from inside the investigation that suffers from it. These tests assert the gate
refuses each unsafe epistemic state *and* that nothing reaches the database when
it does.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from aegis.core.clock import FrozenClock
from aegis.core.errors import DomainError, ValidationError
from aegis.domain.models import Diagnosis, VerificationCheck, VerificationResult
from aegis.memory.recall import (
    CONFIDENCE_SIGNATURE,
    CONFIDENCE_SIMILARITY_MAX,
    MATCH_SIGNATURE,
    MATCH_SIMILARITY,
    IncidentMemoryRecall,
)
from aegis.memory.store import (
    IncidentMemoryStore,
    MemoryContaminationError,
    normalise_symptom,
    recurrence_signature,
)
from aegis.retrieval.hybrid import RetrievedChunk, SearchResult

NOW = datetime(2026, 9, 18, 12, 0, tzinfo=UTC)
INCIDENT = "inc_01J8Z3AAAAAAAAAAAAAAAAAAAA"
SYMPTOM = "checkout p99 latency rose to 4200ms and error rate hit 12%"


class SpyDatabase:
    """Records every statement so a refused write can be proven to be silent."""

    def __init__(self, rows: list[dict[str, Any]] | None = None) -> None:
        self.writes: list[str] = []
        self.rows = rows or []

    async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
        self.writes.append(query)
        return self.rows

    async def fetchrow(self, query: str, *args: Any) -> dict[str, Any] | None:
        self.writes.append(query)
        if "INSERT INTO incident_memories" in query:
            return {
                "id": "mem_01J8Z3BBBBBBBBBBBBBBBBBBBB",
                "occurrences": 3,
                "first_seen_at": NOW - timedelta(days=40),
                "last_seen_at": NOW,
            }
        return self.rows[0] if self.rows else None

    async def fetchval(self, query: str, *args: Any) -> Any:
        self.writes.append(query)
        return None

    async def execute(self, query: str, *args: Any) -> str:
        self.writes.append(query)
        return "OK 1"

    @property
    def touched_memories(self) -> bool:
        return any("incident_memories" in q for q in self.writes)


def good_diagnosis(**over: Any) -> Diagnosis:
    base: dict[str, Any] = {
        "incident_id": INCIDENT,
        "abstained": False,
        "statement": "connection pool saturated after the 14:02 deploy halved max_size",
        "root_cause_category": "resource_exhaustion",
        "confidence": 0.86,
        "supporting_evidence": ["ev_1", "ev_2"],
        "affected_services": ["local:demo:checkout", "local:demo:payment"],
        "contributing_factors": ["no pool-size alert"],
    }
    base.update(over)
    return Diagnosis(**base)


def passing_verification(passed: bool = True) -> VerificationResult:
    return VerificationResult(
        id="ver_1",
        incident_id=INCIDENT,
        passed=passed,
        checks=[
            VerificationCheck(name="error_rate", passed=passed, before=0.12, after=0.001),
            VerificationCheck(name="p99_latency", passed=True, before=4200, after=180),
        ],
        started_at=NOW,
        completed_at=NOW + timedelta(minutes=5),
        notes="steady for 5 minutes",
    )


def store(db: SpyDatabase) -> IncidentMemoryStore:
    return IncidentMemoryStore(db, clock=FrozenClock(NOW))  # type: ignore[arg-type]


async def write(db: SpyDatabase, **over: Any) -> Any:
    payload: dict[str, Any] = {
        "diagnosis": good_diagnosis(),
        "verification": passing_verification(),
        "title": "Checkout pool exhaustion after deploy",
        "symptoms": SYMPTOM,
        "successful_fix": "restored max_size to 40 and redeployed",
        "approved_by": "usr_ops_1",
    }
    payload.update(over)
    return await store(db).write(**payload)


# --------------------------------------------------------------------------- #
# the gate refuses                                                             #
# --------------------------------------------------------------------------- #


async def test_an_abstained_diagnosis_is_refused_and_never_written() -> None:
    """An abstention is a statement about evidence, not about cause."""
    db = SpyDatabase()
    with pytest.raises(MemoryContaminationError) as exc:
        await write(
            db,
            diagnosis=Diagnosis(
                incident_id=INCIDENT,
                abstained=True,
                statement="insufficient evidence to attribute the latency rise",
                supporting_evidence=["ev_1"],
            ),
        )
    assert exc.value.context["reason"] == "abstained"
    assert db.touched_memories is False


async def test_an_unverified_remediation_is_refused() -> None:
    db = SpyDatabase()
    with pytest.raises(MemoryContaminationError) as exc:
        await write(db, verification=None)
    assert exc.value.context["reason"] == "unverified"
    assert db.touched_memories is False


async def test_a_failed_verification_is_refused_and_names_the_failed_checks() -> None:
    db = SpyDatabase()
    with pytest.raises(MemoryContaminationError) as exc:
        await write(db, verification=passing_verification(passed=False))
    assert exc.value.context["reason"] == "verification_failed"
    assert "error_rate" in exc.value.context["failed_checks"]
    assert db.touched_memories is False


async def test_a_memory_with_no_human_approver_is_refused() -> None:
    db = SpyDatabase()
    with pytest.raises(MemoryContaminationError) as exc:
        await write(db, approved_by="   ")
    assert exc.value.context["reason"] == "unapproved"
    assert db.touched_memories is False


async def test_an_ungrounded_diagnosis_is_refused_even_if_it_bypassed_the_model() -> None:
    """Defence in depth: the pydantic model also forbids this, and may change."""
    db = SpyDatabase()
    ungrounded = Diagnosis.model_construct(
        incident_id=INCIDENT,
        abstained=False,
        statement="the pool was saturated",
        root_cause_category="resource_exhaustion",
        confidence=0.9,
        supporting_evidence=[],
        affected_services=["local:demo:checkout"],
        contributing_factors=[],
    )
    with pytest.raises(MemoryContaminationError) as exc:
        await write(db, diagnosis=ungrounded)
    assert exc.value.context["reason"] == "ungrounded"
    assert db.touched_memories is False


async def test_a_memory_with_no_confirmed_fix_is_refused() -> None:
    db = SpyDatabase()
    with pytest.raises(MemoryContaminationError):
        await write(db, successful_fix="   ")
    assert db.touched_memories is False


async def test_a_memory_with_no_cause_category_is_refused() -> None:
    """An uncategorised memory can never be matched to a recurrence."""
    db = SpyDatabase()
    with pytest.raises(MemoryContaminationError):
        await write(db, diagnosis=good_diagnosis(root_cause_category=None), cause_category=None)
    assert db.touched_memories is False


async def test_a_malformed_payload_raises_validation_not_contamination() -> None:
    """The two failures mean different things and must stay distinguishable."""
    db = SpyDatabase()
    with pytest.raises(ValidationError) as exc:
        await write(db, title="  ")
    assert not isinstance(exc.value, MemoryContaminationError)
    assert db.touched_memories is False


def test_contamination_error_is_a_typed_domain_conflict() -> None:
    err = MemoryContaminationError("nope")
    assert isinstance(err, DomainError)
    assert err.code == "MEMORY_CONTAMINATION"
    assert err.http_status == 409
    assert err.retryable is False


# --------------------------------------------------------------------------- #
# the gate admits                                                              #
# --------------------------------------------------------------------------- #


async def test_a_verified_approved_diagnosis_is_written() -> None:
    db = SpyDatabase()
    memory = await write(db)

    assert db.touched_memories is True
    assert memory.approved is True
    assert memory.verification_passed is True
    assert memory.approved_by == "usr_ops_1"
    assert memory.occurrences == 3
    assert memory.cause_category == "resource_exhaustion"
    assert memory.evidence_ids == ("ev_1", "ev_2")
    assert memory.fingerprint == recurrence_signature(
        symptom=SYMPTOM,
        services=["local:demo:checkout", "local:demo:payment"],
        cause_category="resource_exhaustion",
    )
    assert "2/2 checks passed" in memory.verification


async def test_recurrence_is_an_upsert_that_counts_rather_than_a_second_row() -> None:
    db = SpyDatabase()
    await write(db)
    insert = next(q for q in db.writes if "INSERT INTO incident_memories" in q)
    assert "ON CONFLICT (fingerprint)" in insert
    assert "incident_memories.occurrences + 1" in insert


# --------------------------------------------------------------------------- #
# recurrence signature                                                         #
# --------------------------------------------------------------------------- #


def test_signature_is_deterministic_and_order_independent() -> None:
    a = recurrence_signature(symptom=SYMPTOM, services=["b", "a"], cause_category="X")
    b = recurrence_signature(symptom=SYMPTOM, services=["a", "b"], cause_category="x")
    assert a == b
    assert a.startswith("v1:")


def test_the_same_failure_with_different_numbers_shares_a_signature() -> None:
    """Otherwise every occurrence looks new and no recurrence is ever detected."""
    first = recurrence_signature(
        symptom="checkout p99 latency rose to 4200ms on checkout-7",
        services=["local:demo:checkout"],
        cause_category="resource_exhaustion",
    )
    second = recurrence_signature(
        symptom="checkout p99 latency rose to 9100ms on checkout-3",
        services=["local:demo:checkout"],
        cause_category="resource_exhaustion",
    )
    assert first == second


def test_a_different_cause_category_is_a_different_signature() -> None:
    same_symptom = {"symptom": SYMPTOM, "services": ["local:demo:checkout"]}
    assert recurrence_signature(
        **same_symptom, cause_category="resource_exhaustion"
    ) != recurrence_signature(**same_symptom, cause_category="bad_deploy")


def test_a_different_service_set_is_a_different_signature() -> None:
    assert recurrence_signature(
        symptom=SYMPTOM, services=["a"], cause_category="x"
    ) != recurrence_signature(symptom=SYMPTOM, services=["a", "b"], cause_category="x")


def test_normalisation_sorts_deduplicates_and_drops_volatile_values() -> None:
    assert normalise_symptom("Pool exhausted pool EXHAUSTED") == "exhausted pool"
    assert "4200ms" not in normalise_symptom("latency 4200ms")
    assert "deadbeefcafe" not in normalise_symptom("commit deadbeefcafe broke it")


# --------------------------------------------------------------------------- #
# recall never presents a resemblance as a recurrence                          #
# --------------------------------------------------------------------------- #


def memory_row(row_id: str, *, occurrences: int = 1, services: tuple[str, ...] = ()) -> Any:
    return {
        "id": row_id,
        "incident_id": INCIDENT,
        "title": "Checkout pool exhaustion",
        "symptoms": SYMPTOM,
        "root_cause": "pool saturated",
        "cause_category": "resource_exhaustion",
        "evidence_pattern": {},
        "affected_services": list(services),
        "contributing_factors": [],
        "successful_fix": "raise max_size",
        "failed_attempts": [],
        "verification": "2/2 checks passed",
        "verification_passed": True,
        "prevention": "alert on pool utilisation",
        "follow_ups": [],
        "related_commits": [],
        "related_deployments": [],
        "timeline": [],
        "evidence_ids": ["ev_1"],
        "fingerprint": "v1:abc",
        "occurrences": occurrences,
        "approved": True,
        "approved_by": "usr_ops_1",
        "diagnosis_confidence": 0.86,
        "first_seen_at": NOW - timedelta(days=30),
        "last_seen_at": NOW,
    }


class FakeRetriever:
    def __init__(self, memory_ids: list[str], *, degraded: bool = False) -> None:
        self.result = SearchResult(
            chunks=tuple(
                RetrievedChunk(
                    id=f"doc_{i}",
                    kind="document",
                    content="prior incident text",
                    score=1.0 / (i + 1),
                    sub_scores={},
                    source="memory",
                    provenance_uri="",
                    metadata={"memory_id": mid},
                )
                for i, mid in enumerate(memory_ids)
            ),
            degraded=degraded,
            degraded_reason="embeddings_not_configured" if degraded else "",
        )

    async def search(self, *args: Any, **kwargs: Any) -> SearchResult:
        return self.result


async def test_signature_recall_outranks_textual_similarity() -> None:
    db = SpyDatabase(rows=[memory_row("mem_sig", services=("local:demo:checkout",))])
    recall = IncidentMemoryRecall(
        db,  # type: ignore[arg-type]
        FakeRetriever(["mem_sig"]),  # type: ignore[arg-type]
        clock=FrozenClock(NOW),
    )
    result = await recall.similar(
        SYMPTOM, ["local:demo:checkout"], 5, cause_category="resource_exhaustion"
    )
    assert len(result) == 1
    match = result.matches[0]
    assert match.match_type == MATCH_SIGNATURE
    assert match.is_exact_recurrence is True
    assert match.confidence >= CONFIDENCE_SIGNATURE
    assert match.confidence > CONFIDENCE_SIMILARITY_MAX
    assert match.provenance_uri == f"aegis://incident/{INCIDENT}"
    assert match.shared_services == ("local:demo:checkout",)


async def test_similarity_confidence_can_never_reach_signature_confidence() -> None:
    class SignatureMissDatabase(SpyDatabase):
        async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
            self.writes.append(query)
            # No fingerprint match; only the similarity lookup resolves.
            return self.rows if "id = ANY($1::text[])" in query else []

    db = SignatureMissDatabase(rows=[memory_row("mem_sim", occurrences=9)])
    recall = IncidentMemoryRecall(
        db,  # type: ignore[arg-type]
        FakeRetriever(["mem_sim"]),  # type: ignore[arg-type]
        clock=FrozenClock(NOW),
    )
    result = await recall.similar(SYMPTOM, [], 5, cause_category="resource_exhaustion")
    assert len(result) == 1
    match = result.matches[0]
    assert match.match_type == MATCH_SIMILARITY
    assert match.is_exact_recurrence is False
    # Even a nine-time repeat matched textually stays below a signature hit.
    assert match.confidence < CONFIDENCE_SIGNATURE


async def test_recall_without_a_retriever_reports_signature_only_degradation() -> None:
    db = SpyDatabase(rows=[])
    result = await IncidentMemoryRecall(db, None, clock=FrozenClock(NOW)).similar(  # type: ignore[arg-type]
        SYMPTOM, [], 5
    )
    assert result.is_empty is True
    assert result.degraded is True
    assert result.degraded_reason == "no_retriever_configured_signature_matching_only"


async def test_recall_propagates_a_degraded_search() -> None:
    db = SpyDatabase(rows=[])
    recall = IncidentMemoryRecall(
        db,  # type: ignore[arg-type]
        FakeRetriever([], degraded=True),  # type: ignore[arg-type]
        clock=FrozenClock(NOW),
    )
    result = await recall.similar(SYMPTOM, [], 5)
    assert result.degraded is True
    assert result.degraded_reason == "embeddings_not_configured"


async def test_recurring_patterns_rejects_an_unbounded_window() -> None:
    recall = IncidentMemoryRecall(SpyDatabase(), None, clock=FrozenClock(NOW))  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        await recall.recurring_patterns(window_days=0)
    with pytest.raises(ValidationError):
        await recall.recurring_patterns(window_days=5_000)


async def test_recurring_patterns_returns_approved_repeats_only() -> None:
    db = SpyDatabase(
        rows=[
            {
                "fingerprint": "v1:abc",
                "title": "Checkout pool exhaustion",
                "cause_category": "resource_exhaustion",
                "affected_services": ["local:demo:checkout"],
                "occurrences": 4,
                "first_seen_at": NOW - timedelta(days=60),
                "last_seen_at": NOW,
                "prevention": "alert on pool utilisation",
                "follow_ups": ["size the pool from load tests"],
            }
        ]
    )
    recall = IncidentMemoryRecall(db, None, clock=FrozenClock(NOW))  # type: ignore[arg-type]
    patterns = await recall.recurring_patterns(window_days=90)
    assert patterns[0].occurrences == 4
    assert patterns[0].follow_ups == ("size the pool from load tests",)
    query = next(q for q in db.writes if "FROM incident_memories" in q)
    assert "approved = TRUE" in query
    assert "occurrences >= $1" in query
