"""Postgres storage for the long-horizon agent.

Four tables from ``010_horizon.sql``, one per memory tier plus the event log:

* **Checkpoints** are the working memory. ``save_checkpoint`` is a single
  upsert, so after it returns a ``kill -9`` resumes from exactly that
  ``(run_id, step, phase)`` - there is no second statement to lose.
* **Events** carry a ``BIGSERIAL`` sequence. The SSE stream uses it as the
  event id, and ``Last-Event-ID`` replay is a range scan on it.
* **Observations** hold the raw tool output a card was compacted from. The
  model never sees them again; they exist so a card can be audited and
  recalled. Raw text is truncated here, before the INSERT, and the truncation
  is recorded rather than silent.
* **Memory cards** are upserted by id because the incident map arrives after
  the card and changes its image fields.
"""

from __future__ import annotations

import json
import re
from typing import Any, Final

from aegis.agents.horizon.ports import StoredObservation
from aegis.core.errors import ValidationError
from aegis.core.logging import get_logger
from aegis.domain.horizon import (
    EvidenceCard,
    HorizonEvent,
    HorizonEventType,
    HorizonPhase,
    HorizonState,
    MemoryCard,
    Source,
)
from aegis.persistence.db import Database

log = get_logger(__name__)

# Raw observation bound. A container log or a web page can be megabytes; the
# card is ~60 tokens. What is kept is enough to audit the card against.
MAX_OBSERVATION_CHARS: Final = 64_000
# An event payload carries a card, a hypothesis list or SQL text. Anything much
# larger than that is a bug upstream, and the event log is not where to find out.
MAX_EVENT_PAYLOAD_CHARS: Final = 16_000
# FLUX output is a single JPEG/PNG of a few hundred kB.
MAX_INCIDENT_MAP_BYTES: Final = 8 * 1024 * 1024
# Only raster types are ever served back: an HTML or SVG body under an image
# path would be script served from the API's own origin.
INCIDENT_MAP_MIMES: Final = frozenset({"image/jpeg", "image/png", "image/webp"})
MAX_EVENTS_PAGE: Final = 1000
MAX_SERIES_POINTS: Final = 1000
INCIDENT_MAP_PATH: Final = "/v1/war-room/incident-maps/{card_id}"

_CARD_ID_RE: Final = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")


def incident_map_path(card_id: str) -> str:
    return INCIDENT_MAP_PATH.format(card_id=card_id)


def _check_card_id(card_id: str) -> str:
    if not _CARD_ID_RE.match(card_id):
        raise ValidationError("invalid memory card id", context={"card_id": card_id[:64]})
    return card_id


def _bounded_payload(payload: dict[str, Any]) -> dict[str, Any]:
    text = json.dumps(payload, default=str)
    if len(text) <= MAX_EVENT_PAYLOAD_CHARS:
        # Round-tripped so what is stored is exactly what JSON can hold, rather
        # than whatever ``default=str`` would have produced on the way in.
        loaded: dict[str, Any] = json.loads(text)
        return loaded
    return {"truncated": True, "payload_chars": len(text), "keys": sorted(payload)[:20]}


def _event(row: Any) -> HorizonEvent:
    return HorizonEvent(
        ts=row["ts"],
        run_id=row["run_id"],
        incident_id=row["incident_id"],
        step=row["step"],
        phase=HorizonPhase(row["phase"]),
        event_type=HorizonEventType(row["event_type"]),
        tool=row["tool"],
        status=row["status"],
        duration_ms=row["duration_ms"],
        source=Source(row["source"]),
        context_tokens=row["context_tokens"],
        naive_tokens=row["naive_tokens"],
        message=row["message"],
        payload=dict(row["payload"] or {}),
    )


class PostgresHorizonStore:
    """``HorizonStore`` on Postgres, plus the reads the war room needs."""

    __slots__ = ("_db",)

    def __init__(self, db: Database) -> None:
        self._db = db

    # ---- checkpoints ------------------------------------------------------ #

    async def save_checkpoint(self, state: HorizonState) -> None:
        await self._db.execute(
            """
            INSERT INTO horizon_checkpoints (run_id, incident_id, step, phase, state)
            VALUES ($1, $2, $3, $4, $5)
            ON CONFLICT (run_id, step) DO UPDATE
               SET phase = EXCLUDED.phase,
                   state = EXCLUDED.state,
                   created_at = now()
            """,
            state.run_id, state.incident_id, state.step, state.phase.value,
            state.model_dump(mode="json"),
        )

    async def load_latest(self, incident_id: str) -> HorizonState | None:
        row = await self._db.fetchrow(
            """
            SELECT state FROM horizon_checkpoints
             WHERE incident_id = $1
             ORDER BY created_at DESC, step DESC
             LIMIT 1
            """,
            incident_id,
        )
        return HorizonState.model_validate(row["state"]) if row else None

    async def latest_run(self) -> HorizonState | None:
        """The most recent checkpoint of any incident: what the war room shows."""
        row = await self._db.fetchrow(
            "SELECT state FROM horizon_checkpoints ORDER BY created_at DESC, step DESC LIMIT 1"
        )
        return HorizonState.model_validate(row["state"]) if row else None

    async def mark_idle(self, run_id: str) -> bool:
        """Close a run by appending an IDLE checkpoint at the next step.

        Appended rather than rewritten, so the history of what the run actually
        did stays intact. Returns ``False`` when the run is unknown or already
        terminal. A worker still mid-step on this run can write one more
        checkpoint after this; the operator reset that calls this is a demo
        control, not a lock.
        """
        async with self._db.transaction() as conn:
            row = await conn.fetchrow(
                """
                SELECT state FROM horizon_checkpoints
                 WHERE run_id = $1 ORDER BY step DESC LIMIT 1 FOR UPDATE
                """,
                run_id,
            )
            if row is None:
                return False
            state = HorizonState.model_validate(row["state"])
            if state.phase.is_terminal:
                return False
            idle = state.model_copy(
                update={"phase": HorizonPhase.IDLE, "step": state.step + 1}
            )
            await conn.execute(
                """
                INSERT INTO horizon_checkpoints (run_id, incident_id, step, phase, state)
                VALUES ($1, $2, $3, $4, $5)
                ON CONFLICT (run_id, step) DO UPDATE
                   SET phase = EXCLUDED.phase, state = EXCLUDED.state, created_at = now()
                """,
                idle.run_id, idle.incident_id, idle.step, idle.phase.value,
                idle.model_dump(mode="json"),
            )
        log.info("horizon run marked idle", run_id=run_id, step=idle.step)
        return True

    async def context_series(self, incident_id: str) -> list[dict[str, int]]:
        """Context vs naive tokens per step for the incident's latest run.

        Read from the checkpoints rather than the event log because there is
        exactly one checkpoint per step. The IDLE marker a reset appends is not
        a step the model took, so it is not a point on the chart.
        """
        rows = await self._db.fetch(
            """
            SELECT step,
                   COALESCE((state->'tokens'->>'context_tokens')::int, 0) AS context_tokens,
                   COALESCE((state->'tokens'->>'naive_tokens')::int, 0) AS naive_tokens
              FROM horizon_checkpoints
             WHERE run_id = (SELECT run_id FROM horizon_checkpoints
                              WHERE incident_id = $1
                              ORDER BY created_at DESC LIMIT 1)
               AND phase <> 'IDLE'
             ORDER BY step
             LIMIT $2
            """,
            incident_id, MAX_SERIES_POINTS,
        )
        return [
            {
                "step": r["step"],
                "context_tokens": r["context_tokens"],
                "naive_tokens": r["naive_tokens"],
            }
            for r in rows
        ]

    # ---- events ----------------------------------------------------------- #

    async def append_event(self, event: HorizonEvent) -> int:
        seq = await self._db.fetchval(
            """
            INSERT INTO horizon_events
                (incident_id, run_id, step, phase, event_type, tool, status, source,
                 duration_ms, context_tokens, naive_tokens, message, payload, ts)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14)
            RETURNING seq
            """,
            event.incident_id, event.run_id, event.step, event.phase.value,
            event.event_type.value, event.tool, event.status, event.source.value,
            event.duration_ms, event.context_tokens, event.naive_tokens,
            event.message[:500], _bounded_payload(event.payload), event.ts,
        )
        return int(seq)

    async def events(
        self, incident_id: str, *, after_seq: int = 0, limit: int = 500
    ) -> list[tuple[int, HorizonEvent]]:
        rows = await self._db.fetch(
            """
            SELECT * FROM horizon_events
             WHERE incident_id = $1 AND seq > $2
             ORDER BY seq
             LIMIT $3
            """,
            incident_id, max(after_seq, 0), max(1, min(limit, MAX_EVENTS_PAGE)),
        )
        return [(int(r["seq"]), _event(r)) for r in rows]

    async def events_of_type(
        self, incident_id: str, event_type: HorizonEventType, *, limit: int = 100
    ) -> list[tuple[int, HorizonEvent]]:
        """The newest ``limit`` events of one type, oldest first."""
        rows = await self._db.fetch(
            """
            SELECT * FROM horizon_events
             WHERE incident_id = $1 AND event_type = $2
             ORDER BY seq DESC
             LIMIT $3
            """,
            incident_id, event_type.value, max(1, min(limit, MAX_EVENTS_PAGE)),
        )
        return [(int(r["seq"]), _event(r)) for r in reversed(rows)]

    async def last_seq(self, incident_id: str | None = None) -> int:
        value = await self._db.fetchval(
            """
            SELECT COALESCE(max(seq), 0) FROM horizon_events
             WHERE ($1::text IS NULL OR incident_id = $1)
            """,
            incident_id,
        )
        return int(value or 0)

    # ---- observations ----------------------------------------------------- #

    async def save_observation(self, obs: StoredObservation) -> None:
        raw = obs.raw
        extra = dict(obs.extra)
        if len(raw) > MAX_OBSERVATION_CHARS:
            extra["raw_truncated"] = True
            extra["raw_chars"] = len(raw)
            raw = raw[:MAX_OBSERVATION_CHARS]
        # The raw text of an evidence id is immutable once written: a resumed
        # step that re-saves the same id may refresh the card, never the raw.
        await self._db.execute(
            """
            INSERT INTO horizon_observations (evidence_id, incident_id, tool, raw, card, extra)
            VALUES ($1, $2, $3, $4, $5, $6)
            ON CONFLICT (evidence_id) DO UPDATE
               SET card = COALESCE(EXCLUDED.card, horizon_observations.card),
                   extra = horizon_observations.extra || EXCLUDED.extra
            """,
            obs.evidence_id, obs.incident_id, obs.tool, raw,
            obs.card.model_dump(mode="json") if obs.card is not None else None,
            _bounded_payload(extra),
        )

    async def get_observation(self, evidence_id: str) -> StoredObservation | None:
        row = await self._db.fetchrow(
            "SELECT * FROM horizon_observations WHERE evidence_id = $1", evidence_id
        )
        if row is None:
            return None
        return StoredObservation(
            evidence_id=row["evidence_id"],
            incident_id=row["incident_id"],
            tool=row["tool"],
            raw=row["raw"],
            card=EvidenceCard.model_validate(row["card"]) if row["card"] else None,
            extra=dict(row["extra"] or {}),
        )

    # ---- memory cards ----------------------------------------------------- #

    async def save_memory_card(self, card: MemoryCard) -> None:
        _check_card_id(card.id)
        await self._db.execute(
            """
            INSERT INTO horizon_memory_cards (id, incident_id, card)
            VALUES ($1, $2, $3)
            ON CONFLICT (id) DO UPDATE
               SET incident_id = EXCLUDED.incident_id,
                   card = EXCLUDED.card,
                   updated_at = now()
            """,
            card.id, card.incident_id, card.model_dump(mode="json"),
        )

    async def memory_cards(self, *, limit: int = 20) -> list[MemoryCard]:
        rows = await self._db.fetch(
            """
            SELECT card FROM horizon_memory_cards
             WHERE card IS NOT NULL
             ORDER BY updated_at DESC
             LIMIT $1
            """,
            max(1, min(limit, 100)),
        )
        return [MemoryCard.model_validate(r["card"]) for r in rows]

    async def save_incident_map(self, card_id: str, image: bytes, mime: str) -> str:
        _check_card_id(card_id)
        if mime not in INCIDENT_MAP_MIMES:
            raise ValidationError(
                "incident map must be a raster image", context={"mime": mime[:64]}
            )
        if not image or len(image) > MAX_INCIDENT_MAP_BYTES:
            raise ValidationError(
                "incident map size out of bounds",
                context={"bytes": len(image), "max_bytes": MAX_INCIDENT_MAP_BYTES},
            )
        path = incident_map_path(card_id)
        # Upsert so the image may land before the card; when the card exists
        # its image fields are brought in line in the same statement.
        await self._db.execute(
            """
            INSERT INTO horizon_memory_cards (id, image, image_mime)
            VALUES ($1, $2, $3)
            ON CONFLICT (id) DO UPDATE
               SET image = EXCLUDED.image,
                   image_mime = EXCLUDED.image_mime,
                   card = CASE WHEN horizon_memory_cards.card IS NULL THEN NULL
                               ELSE horizon_memory_cards.card
                                    || jsonb_build_object('image_url', $4::text,
                                                          'image_status', 'ready')
                          END,
                   updated_at = now()
            """,
            card_id, image, mime, path,
        )
        return path

    async def get_incident_map(self, card_id: str) -> tuple[bytes, str] | None:
        if not _CARD_ID_RE.match(card_id):
            return None
        row = await self._db.fetchrow(
            "SELECT image, image_mime FROM horizon_memory_cards WHERE id = $1", card_id
        )
        if row is None or row["image"] is None or row["image_mime"] not in INCIDENT_MAP_MIMES:
            return None
        return bytes(row["image"]), str(row["image_mime"])


__all__ = [
    "INCIDENT_MAP_MIMES",
    "MAX_INCIDENT_MAP_BYTES",
    "MAX_OBSERVATION_CHARS",
    "PostgresHorizonStore",
    "incident_map_path",
]
