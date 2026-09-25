"""Server-sent events for live incident state.

Design constraints from the UX spec (sections 111-112): the UI must reconcile
incrementally and idempotently, never re-render wholesale, and never present a
state the backend does not hold.

Each event therefore carries a monotonic sequence number. A client that
reconnects sends ``Last-Event-ID`` and receives only what it missed. Redis
pub/sub fans events out across API replicas; when Redis is unavailable the
endpoint degrades to periodic snapshots rather than failing.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Annotated, Any

from fastapi import APIRouter, Header, Request
from sse_starlette.sse import EventSourceResponse

from aegis.api.deps import IncidentsDep, RequireViewer
from aegis.core.logging import get_logger

log = get_logger(__name__)
router = APIRouter(tags=["stream"])

HEARTBEAT_S = 15.0
# A slow browser must not make the server buffer without bound; if the client
# falls this far behind we drop it and let it reconnect with Last-Event-ID.
MAX_QUEUE = 100


def channel_for(incident_id: str) -> str:
    return f"aegis:incident:{incident_id}"


async def publish(redis: Any, incident_id: str, event: dict[str, Any]) -> None:
    """Publish an incident event. Never raises into the caller.

    Streaming is a convenience layer: a failure here must not abort the
    investigation that produced the event.
    """
    if redis is None:
        return
    try:
        await redis.publish(channel_for(incident_id), json.dumps(event, default=str))
    except Exception as exc:  # noqa: BLE001
        log.warning("event publish failed", incident_id=incident_id, error=str(exc))


@router.get("/incidents/{incident_id}/stream", summary="Live incident event stream")
async def stream_incident(
    incident_id: str,
    request: Request,
    _: RequireViewer,
    incidents: IncidentsDep,
    last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
) -> EventSourceResponse:
    # Verifies existence and that the caller may see it before opening a stream.
    incident = await incidents.get(incident_id)
    redis = request.app.state.redis

    async def generator() -> AsyncIterator[dict[str, Any]]:
        seq = int(last_event_id) if last_event_id and last_event_id.isdigit() else 0

        # Always open with a snapshot so a fresh or reconnecting client is
        # immediately consistent without a separate fetch.
        seq += 1
        yield {
            "id": str(seq),
            "event": "snapshot",
            "data": json.dumps(incident.model_dump(), default=str),
        }

        if redis is None:
            # Degraded mode: poll rather than fail. The UI still updates, just
            # less promptly, and /health reports why.
            while not await request.is_disconnected():
                await asyncio.sleep(HEARTBEAT_S)
                try:
                    current = await incidents.get(incident_id)
                except Exception:  # noqa: BLE001
                    break
                seq += 1
                yield {
                    "id": str(seq),
                    "event": "snapshot",
                    "data": json.dumps(current.model_dump(), default=str),
                }
            return

        pubsub = redis.pubsub()
        await pubsub.subscribe(channel_for(incident_id))
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    message = await asyncio.wait_for(
                        pubsub.get_message(ignore_subscribe_messages=True),
                        timeout=HEARTBEAT_S,
                    )
                except TimeoutError:
                    message = None

                if message is None:
                    # Heartbeat keeps proxies from closing an idle connection.
                    yield {"event": "ping", "data": "{}"}
                    continue

                seq += 1
                payload = message.get("data")
                try:
                    body = json.loads(payload) if isinstance(payload, str) else {}
                except json.JSONDecodeError:
                    log.warning("dropped malformed stream payload",
                                incident_id=incident_id)
                    continue
                yield {
                    "id": str(seq),
                    "event": body.get("type", "update"),
                    "data": json.dumps(body, default=str),
                }
        finally:
            # Always release the subscription, including on client disconnect,
            # or connections leak until Redis refuses new ones.
            try:
                await pubsub.unsubscribe(channel_for(incident_id))
                await pubsub.aclose()
            except Exception as exc:  # noqa: BLE001
                log.warning("pubsub cleanup failed", error=str(exc))

    return EventSourceResponse(generator(), send_timeout=30)
