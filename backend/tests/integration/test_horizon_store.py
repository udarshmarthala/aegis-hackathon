"""PostgresHorizonStore against a real PostgreSQL.

The crash-resume claim rests on these: a checkpoint written is the checkpoint
read back, the event log's seq is monotonic, and a memory card upsert keeps
the image that arrived for it.

``010_horizon.sql`` is executed directly rather than through the migrator so a
test run never records a checksum against a developer's database; the file is
idempotent, which this also exercises by running it twice.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from aegis.agents.horizon.ports import StoredObservation
from aegis.core.errors import ValidationError
from aegis.core.ids import INCIDENT, new_id
from aegis.domain.horizon import (
    EvidenceCard,
    HorizonEvent,
    HorizonEventType,
    HorizonPhase,
    HorizonState,
    MemoryCard,
    Source,
    TokenStats,
)
from aegis.persistence.db import Database
from aegis.persistence.horizon import MAX_OBSERVATION_CHARS, PostgresHorizonStore

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

MIGRATION = Path(__file__).resolve().parents[2] / "migrations" / "010_horizon.sql"


@pytest.fixture
async def store(db: Database) -> AsyncIterator[tuple[PostgresHorizonStore, str, str]]:
    sql = MIGRATION.read_text(encoding="utf-8")
    await db.execute(sql)
    await db.execute(sql)
    incident_id = new_id(INCIDENT)
    run_id = f"run_{incident_id[-12:]}"
    card_id = f"mem_{incident_id[-12:]}"
    try:
        yield PostgresHorizonStore(db), incident_id, run_id
    finally:
        # Only this test's rows; a developer's demo data is left alone.
        await db.execute("DELETE FROM horizon_checkpoints WHERE incident_id = $1", incident_id)
        await db.execute("DELETE FROM horizon_events WHERE incident_id = $1", incident_id)
        await db.execute("DELETE FROM horizon_observations WHERE incident_id = $1", incident_id)
        await db.execute("DELETE FROM horizon_memory_cards WHERE id = $1", card_id)


def _state(incident_id: str, run_id: str, step: int, phase: HorizonPhase) -> HorizonState:
    return HorizonState(
        run_id=run_id,
        incident_id=incident_id,
        service="checkout",
        step=step,
        phase=phase,
        notes=[f"step {step}"],
        tokens=TokenStats(context_tokens=1500 + step, naive_tokens=1000 * step),
    )


async def test_checkpoint_resume_returns_the_latest_step(
    store: tuple[PostgresHorizonStore, str, str],
) -> None:
    s, incident_id, run_id = store
    await s.save_checkpoint(_state(incident_id, run_id, 1, HorizonPhase.INVESTIGATING))
    await s.save_checkpoint(_state(incident_id, run_id, 2, HorizonPhase.DIAGNOSING))
    # A resumed step re-saves the same (run, step): an upsert, not a conflict.
    await s.save_checkpoint(_state(incident_id, run_id, 2, HorizonPhase.PLANNING))

    resumed = await s.load_latest(incident_id)
    assert resumed is not None
    assert (resumed.run_id, resumed.step, resumed.phase) == (run_id, 2, HorizonPhase.PLANNING)
    assert resumed.notes == ["step 2"]

    series = await s.context_series(incident_id)
    assert [p["step"] for p in series] == [1, 2]
    assert series[1] == {"step": 2, "context_tokens": 1502, "naive_tokens": 2000}

    assert await s.mark_idle(run_id) is True
    idle = await s.load_latest(incident_id)
    assert idle is not None and idle.phase is HorizonPhase.IDLE and idle.step == 3
    assert await s.mark_idle(run_id) is False  # already terminal
    assert [p["step"] for p in await s.context_series(incident_id)] == [1, 2]


async def test_events_have_monotonic_seq_and_page_by_it(
    store: tuple[PostgresHorizonStore, str, str],
) -> None:
    s, incident_id, run_id = store
    seqs = []
    for step in range(5):
        seqs.append(await s.append_event(HorizonEvent(
            ts=datetime.now(UTC), run_id=run_id, incident_id=incident_id, step=step,
            phase=HorizonPhase.INVESTIGATING,
            event_type=(HorizonEventType.RAWTREE_QUERY if step == 3
                        else HorizonEventType.STEP_COMPLETED),
            source=Source.RAWTREE if step == 3 else Source.SYSTEM,
            payload={"sql": "SELECT 1", "rows": 1} if step == 3 else {"n": step},
        )))
    assert seqs == sorted(seqs) and len(set(seqs)) == 5

    page = await s.events(incident_id, after_seq=seqs[1], limit=2)
    assert [seq for seq, _ in page] == seqs[2:4]
    assert page[0][1].payload == {"n": 2}
    typed = await s.events_of_type(incident_id, HorizonEventType.RAWTREE_QUERY)
    assert [seq for seq, _ in typed] == [seqs[3]]
    assert await s.last_seq(incident_id) == seqs[-1]


async def test_observation_raw_is_truncated_with_a_recorded_flag(
    store: tuple[PostgresHorizonStore, str, str],
) -> None:
    s, incident_id, _ = store
    evidence_id = f"ev_{incident_id[-12:]}"
    card = EvidenceCard(id=evidence_id, step=1, tool="get_logs", source=Source.RULE,
                        claim="pool exhausted")
    await s.save_observation(StoredObservation(
        evidence_id=evidence_id, incident_id=incident_id, tool="get_logs",
        raw="x" * (MAX_OBSERVATION_CHARS + 10), card=card,
    ))
    got = await s.get_observation(evidence_id)
    assert got is not None
    assert len(got.raw) == MAX_OBSERVATION_CHARS
    assert got.extra["raw_truncated"] is True
    assert got.extra["raw_chars"] == MAX_OBSERVATION_CHARS + 10
    assert got.card == card


async def test_memory_card_upsert_keeps_the_image(
    store: tuple[PostgresHorizonStore, str, str],
) -> None:
    s, incident_id, _ = store
    card_id = f"mem_{incident_id[-12:]}"
    card = MemoryCard(id=card_id, incident_id=incident_id, symptoms="p99 up",
                      root_cause="pool leak")
    await s.save_memory_card(card)
    image = b"\x89PNG\r\n\x1a\n" + bytes(range(256))
    path = await s.save_incident_map(card_id, image, "image/png")
    assert path == f"/v1/war-room/incident-maps/{card_id}"

    assert await s.get_incident_map(card_id) == (image, "image/png")
    listed = {c.id: c for c in await s.memory_cards(limit=100)}
    assert listed[card_id].image_url == path
    assert listed[card_id].image_status == "ready"

    # A later card upsert replaces the card, never the stored image.
    await s.save_memory_card(card.model_copy(update={"lesson": "roll back first"}))
    assert await s.get_incident_map(card_id) == (image, "image/png")
    listed = {c.id: c for c in await s.memory_cards(limit=100)}
    assert listed[card_id].lesson == "roll back first"

    with pytest.raises(ValidationError):
        await s.save_incident_map(card_id, b"<svg/>", "image/svg+xml")
