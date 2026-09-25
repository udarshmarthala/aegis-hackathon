"""The horizon event bus.

One ordered path for every event: persist (which assigns the sequence number),
hand to RawTree, then fan out to live sinks. Persistence comes first because
the store is the record; RawTree and the SSE fan-out are conveniences, and a
failure in either is logged and absorbed rather than allowed to stop the loop
(observability is not a control-plane dependency).
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any, Final

from aegis.agents.horizon.ports import EventSink, HorizonStore, RawTreePort
from aegis.core.clock import SYSTEM_CLOCK, Clock
from aegis.core.logging import get_logger, redact_processor
from aegis.domain.horizon import (
    HorizonEvent,
    HorizonEventType,
    HorizonState,
    Source,
)

log = get_logger(__name__)

# A live sink that cannot accept an event within this bound is skipped for that
# event. The UI can always re-read the ordered log from the store.
SINK_TIMEOUT_S: Final = 2.0
MAX_MESSAGE_CHARS: Final = 500


def _scrub(payload: dict[str, Any]) -> dict[str, Any]:
    """Mask anything credential-shaped before the payload leaves the process."""
    return dict(redact_processor(None, "", dict(payload)))


class EventBus:
    """Implements the contract ``emit(event) -> seq``."""

    __slots__ = ("_clock", "_rawtree", "_sinks", "_store")

    def __init__(
        self,
        store: HorizonStore,
        sinks: list[EventSink],
        rawtree: RawTreePort | None = None,
        *,
        clock: Clock = SYSTEM_CLOCK,
    ) -> None:
        self._store = store
        self._sinks = list(sinks)
        self._rawtree = rawtree
        self._clock = clock

    async def emit(self, event: HorizonEvent) -> int:
        seq = await self._store.append_event(event)
        if self._rawtree is not None:
            try:
                self._rawtree.enqueue_event(event)
            except Exception as exc:  # noqa: BLE001 - the port promises not to raise
                log.warning("rawtree enqueue_event failed", error=type(exc).__name__)
        for sink in self._sinks:
            try:
                await asyncio.wait_for(sink.publish(seq, event), timeout=SINK_TIMEOUT_S)
            except Exception as exc:  # noqa: BLE001 - a live sink is never load-bearing
                log.warning(
                    "event sink publish failed",
                    sink=type(sink).__name__,
                    error=type(exc).__name__,
                )
        return seq

    def build(
        self,
        state: HorizonState,
        event_type: HorizonEventType,
        *,
        message: str = "",
        tool: str | None = None,
        status: str = "ok",
        source: Source = Source.SYSTEM,
        duration_ms: int = 0,
        payload: dict[str, Any] | None = None,
        ts: datetime | None = None,
    ) -> HorizonEvent:
        return HorizonEvent(
            ts=ts or self._clock.now(),
            run_id=state.run_id,
            incident_id=state.incident_id,
            step=state.step,
            phase=state.phase,
            event_type=event_type,
            tool=tool,
            status=status,
            duration_ms=max(0, duration_ms),
            source=source,
            context_tokens=state.tokens.context_tokens,
            naive_tokens=state.tokens.naive_tokens,
            message=str(_scrub({"m": message})["m"])[:MAX_MESSAGE_CHARS],
            payload=_scrub(payload or {}),
        )

    async def publish(self, state: HorizonState, event_type: HorizonEventType, **kw: Any) -> int:
        return await self.emit(self.build(state, event_type, **kw))


__all__ = ["EventBus"]
