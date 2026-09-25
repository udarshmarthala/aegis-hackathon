"""An in-memory ``HorizonStore``.

Used by the unit tests and as the process-local fallback when Postgres has not
been wired. Every collection is bounded: a store that grows without limit in a
process expected to run for a whole on-call shift is a slow memory leak with a
dashboard on top.
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict, deque
from typing import Final

from aegis.agents.horizon.ports import StoredObservation
from aegis.domain.horizon import HorizonEvent, HorizonState, MemoryCard

MAX_EVENTS: Final = 20_000
MAX_OBSERVATIONS: Final = 5_000
MAX_CHECKPOINTS_PER_INCIDENT: Final = 50
MAX_INCIDENTS: Final = 200
MAX_MEMORY_CARDS: Final = 500
MAX_IMAGES: Final = 50


class InMemoryHorizonStore:
    """Implements ``ports.HorizonStore``. Safe for one event loop."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._checkpoints: OrderedDict[str, deque[HorizonState]] = OrderedDict()
        self._events: deque[tuple[int, HorizonEvent]] = deque(maxlen=MAX_EVENTS)
        self._seq = 0
        self._observations: OrderedDict[str, StoredObservation] = OrderedDict()
        self._cards: OrderedDict[str, MemoryCard] = OrderedDict()
        self._images: OrderedDict[str, tuple[bytes, str]] = OrderedDict()

    # ---- checkpoints ---------------------------------------------------- #

    async def save_checkpoint(self, state: HorizonState) -> None:
        # A deep copy, so a caller mutating its live state after the save cannot
        # rewrite what a resumed run will load - the same isolation Postgres
        # gives for free.
        snapshot = state.model_copy(deep=True)
        async with self._lock:
            history = self._checkpoints.setdefault(
                state.incident_id, deque(maxlen=MAX_CHECKPOINTS_PER_INCIDENT)
            )
            history.append(snapshot)
            self._checkpoints.move_to_end(state.incident_id)
            while len(self._checkpoints) > MAX_INCIDENTS:
                self._checkpoints.popitem(last=False)

    async def load_latest(self, incident_id: str) -> HorizonState | None:
        async with self._lock:
            history = self._checkpoints.get(incident_id)
            if not history:
                return None
            return history[-1].model_copy(deep=True)

    # ---- events --------------------------------------------------------- #

    async def append_event(self, event: HorizonEvent) -> int:
        async with self._lock:
            self._seq += 1
            self._events.append((self._seq, event))
            return self._seq

    async def events(
        self, incident_id: str, *, after_seq: int = 0, limit: int = 500
    ) -> list[tuple[int, HorizonEvent]]:
        async with self._lock:
            out = [
                (seq, ev)
                for seq, ev in self._events
                if seq > after_seq and ev.incident_id == incident_id
            ]
        return out[: max(1, min(limit, 5_000))]

    # ---- raw observations ----------------------------------------------- #

    async def save_observation(self, obs: StoredObservation) -> None:
        async with self._lock:
            self._observations[obs.evidence_id] = obs
            self._observations.move_to_end(obs.evidence_id)
            while len(self._observations) > MAX_OBSERVATIONS:
                self._observations.popitem(last=False)

    async def get_observation(self, evidence_id: str) -> StoredObservation | None:
        async with self._lock:
            return self._observations.get(evidence_id)

    # ---- memory cards and incident maps ---------------------------------- #

    async def save_memory_card(self, card: MemoryCard) -> None:
        async with self._lock:
            self._cards[card.id] = card
            self._cards.move_to_end(card.id)
            while len(self._cards) > MAX_MEMORY_CARDS:
                self._cards.popitem(last=False)

    async def memory_cards(self, *, limit: int = 20) -> list[MemoryCard]:
        async with self._lock:
            cards = list(self._cards.values())
        return list(reversed(cards))[: max(1, min(limit, MAX_MEMORY_CARDS))]

    async def save_incident_map(self, card_id: str, image: bytes, mime: str) -> str:
        async with self._lock:
            self._images[card_id] = (image, mime)
            while len(self._images) > MAX_IMAGES:
                self._images.popitem(last=False)
        return f"/war-room/incident-maps/{card_id}"

    def image(self, card_id: str) -> tuple[bytes, str] | None:
        """Test and API helper: the stored map bytes, if any."""
        return self._images.get(card_id)


__all__ = ["InMemoryHorizonStore"]
