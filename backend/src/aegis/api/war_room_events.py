"""Live fan-out for the war room: the Redis event sink and the SSE generator.

Redis is never authoritative here. Every horizon event is appended to Postgres
first (that is where its ``seq`` comes from) and only then published, so a lost
publish costs a browser some latency - it reconnects with ``Last-Event-ID`` and
the gap is replayed from ``horizon_events`` - and never costs the record.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any, Final

from aegis.core.logging import get_logger
from aegis.core.resilience import guarded_call
from aegis.domain.horizon import HorizonEvent

log = get_logger(__name__)

WAR_ROOM_CHANNEL: Final = "aegis:war-room"
CONTROL_CHANNEL: Final = "aegis:control"
PUBLISH_TIMEOUT_S: Final = 2.0

HEALTH_INTERVAL_S: Final = 5.0
PING_INTERVAL_S: Final = 15.0
SNAPSHOT_INTERVAL_S: Final = 5.0
# A browser that falls this far behind is dropped rather than buffered for; it
# reconnects with Last-Event-ID and replays from Postgres.
MAX_QUEUE: Final = 256
MAX_REPLAY: Final = 2000
REPLAY_PAGE: Final = 500


def event_message(seq: int, event: HorizonEvent) -> dict[str, Any]:
    """The one wire shape for a horizon event, on Redis and over SSE."""
    return {"seq": seq, "event": event.model_dump(mode="json")}


class RedisEventSink:
    """``EventSink`` publishing to ``aegis:war-room``. Never raises."""

    __slots__ = ("_redis",)

    def __init__(self, redis: Any) -> None:
        self._redis = redis

    async def publish(self, seq: int, event: HorizonEvent) -> None:
        if self._redis is None:
            return
        body = json.dumps(event_message(seq, event), default=str)
        redis = self._redis

        async def _send() -> Any:
            return await redis.publish(WAR_ROOM_CHANNEL, body)

        try:
            await guarded_call(
                _send, dependency="redis-pubsub", timeout_s=PUBLISH_TIMEOUT_S, attempts=1
            )
        except Exception as exc:  # noqa: BLE001 - live fan-out is a convenience
            log.warning(
                "war-room publish failed",
                seq=seq,
                incident_id=event.incident_id,
                error=type(exc).__name__,
            )


async def publish_control(redis: Any, command: str) -> int:
    """Publish a control command; return how many subscribers received it.

    Raises on failure: unlike the event sink, the caller of a control command
    must know it did not arrive.
    """

    async def _send() -> int:
        return int(await redis.publish(CONTROL_CHANNEL, json.dumps({"command": command})))

    return await guarded_call(
        _send, dependency="redis-pubsub", timeout_s=PUBLISH_TIMEOUT_S, attempts=1
    )


def parse_last_event_id(header: str | None) -> int:
    if header and header.strip().isdigit():
        return int(header.strip())
    return 0


def _sse(name: str, data: Any, event_id: int | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {"event": name, "data": json.dumps(data, default=str)}
    # Only horizon events and snapshots carry an id. A health frame without one
    # leaves the browser's Last-Event-ID on the last horizon seq, which is what
    # replay needs.
    if event_id is not None:
        out["id"] = str(event_id)
    return out


SnapshotFn = Callable[[], Awaitable[dict[str, Any]]]
HealthFn = Callable[[], Awaitable[dict[str, Any]]]
ReplayFn = Callable[[int, int], Awaitable[list[dict[str, Any]]]]
DisconnectedFn = Callable[[], Awaitable[bool]]


async def _pump(pubsub: Any, queue: asyncio.Queue[dict[str, Any] | None]) -> None:
    """Move Redis messages into the bounded queue; ``None`` means overflow."""
    while True:
        message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
        if message is None:
            continue
        data = message.get("data")
        try:
            body = json.loads(data) if isinstance(data, str | bytes) else None
        except json.JSONDecodeError:
            log.warning("dropped malformed war-room payload")
            continue
        if not isinstance(body, dict) or not isinstance(body.get("seq"), int):
            continue
        try:
            queue.put_nowait(body)
        except asyncio.QueueFull:
            # Signal overflow and stop reading; the stream closes and the
            # client replays from Postgres.
            with contextlib.suppress(asyncio.QueueFull):
                queue.get_nowait()
                queue.put_nowait(None)
            return


async def war_room_stream(
    *,
    snapshot: SnapshotFn,
    health: HealthFn,
    replay: ReplayFn,
    redis: Any,
    is_disconnected: DisconnectedFn,
    last_event_id: int = 0,
    health_interval_s: float = HEALTH_INTERVAL_S,
    ping_interval_s: float = PING_INTERVAL_S,
    snapshot_interval_s: float = SNAPSHOT_INTERVAL_S,
    max_queue: int = MAX_QUEUE,
) -> AsyncIterator[dict[str, Any]]:
    """The war-room SSE body.

    Order matters: subscribe first, then snapshot, then replay, then live. A
    publish landing between the snapshot and the subscription would otherwise
    be lost; with this order it may arrive twice, and duplicates are dropped by
    seq.
    """
    pubsub: Any = None
    pump: asyncio.Task[None] | None = None
    queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue(maxsize=max_queue)
    if redis is not None:
        try:
            pubsub = redis.pubsub()
            await asyncio.wait_for(pubsub.subscribe(WAR_ROOM_CHANNEL), timeout=PUBLISH_TIMEOUT_S)
            pump = asyncio.create_task(_pump(pubsub, queue))
        except Exception as exc:  # noqa: BLE001 - degrade to snapshots, never fail
            log.warning("war-room subscribe failed; degrading", error=type(exc).__name__)
            pubsub = None
    try:
        state = await snapshot()
        sent = max(int(state.get("last_seq", 0) or 0), 0)
        yield _sse("snapshot", state, sent)
        if last_event_id:
            # Replay what the reconnecting client missed, bounded; the snapshot
            # already carries the current state, so a longer gap loses history
            # on the panel, never correctness.
            cursor = last_event_id
            replayed = 0
            while replayed < MAX_REPLAY:
                page = await replay(cursor, REPLAY_PAGE)
                for item in page:
                    yield _sse("horizon", item, item["seq"])
                    cursor = item["seq"]
                replayed += len(page)
                if len(page) < REPLAY_PAGE:
                    break
            sent = max(sent, cursor)

        loop = asyncio.get_running_loop()
        next_health = loop.time()
        next_ping = loop.time() + ping_interval_s
        next_snapshot = loop.time() + snapshot_interval_s
        while not await is_disconnected():
            now = loop.time()
            if now >= next_health:
                yield _sse("health", await health())
                next_health = now + health_interval_s
                continue
            if pubsub is None and now >= next_snapshot:
                # Degraded mode: no live feed, so poll the record instead.
                for item in await replay(sent, REPLAY_PAGE):
                    yield _sse("horizon", item, item["seq"])
                    sent = max(sent, item["seq"])
                state = await snapshot()
                yield _sse("snapshot", state, max(sent, int(state.get("last_seq", 0) or 0)))
                next_snapshot = now + snapshot_interval_s
                continue
            if now >= next_ping:
                yield {"event": "ping", "data": "{}"}
                next_ping = now + ping_interval_s
                continue
            wait = max(0.05, min(next_health, next_ping, next_snapshot) - now)
            if pubsub is None:
                await asyncio.sleep(min(wait, 1.0))
                continue
            try:
                body = await asyncio.wait_for(queue.get(), timeout=min(wait, 1.0))
            except TimeoutError:
                continue
            if body is None:
                log.warning("war-room client fell behind; closing for replay")
                return
            if body["seq"] <= sent:
                continue
            sent = body["seq"]
            yield _sse("horizon", body, sent)
    finally:
        # Always release the subscription, including on client disconnect, or
        # connections leak until Redis refuses new ones.
        if pump is not None:
            pump.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await pump
        if pubsub is not None:
            try:
                await asyncio.wait_for(pubsub.unsubscribe(WAR_ROOM_CHANNEL), timeout=2.0)
                await asyncio.wait_for(pubsub.aclose(), timeout=2.0)
            except Exception as exc:  # noqa: BLE001
                log.warning("war-room pubsub cleanup failed", error=type(exc).__name__)


__all__ = [
    "CONTROL_CHANNEL",
    "WAR_ROOM_CHANNEL",
    "RedisEventSink",
    "event_message",
    "parse_last_event_id",
    "publish_control",
    "war_room_stream",
]
