"""One chat-completions call to Gemini's OpenAI-compatible endpoint.

Shared by the Gemini brain tier and the compactor. Both walk a ``KeyRing``
pool with the same fault semantics as ``agents.llm.ModelRouter``: a fault
another key would survive parks the key and advances, a fault every key
shares stops at the first key. What differs is only what each caller does
when the ring is exhausted, which is why the ring walk returns a typed
outcome rather than raising.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import httpx

from aegis.core.keyring import KeyFault, KeyRing, KeySlot, classify
from aegis.core.logging import get_logger
from aegis.core.resilience import guarded_call

log = get_logger(__name__)


def model_name(configured: str) -> str:
    """Strip a ``vendor/`` prefix; the endpoint 404s on aggregator-style ids."""
    return configured.split("/", 1)[-1].strip()


def build_client(
    base_url: str, timeout_s: float, transport: httpx.AsyncBaseTransport | None
) -> httpx.AsyncClient:
    # The key travels per request in the Authorization header, never on the
    # client, so one pooled client serves every key in the ring.
    return httpx.AsyncClient(
        base_url=base_url if base_url.endswith("/") else base_url + "/",
        timeout=timeout_s,
        transport=transport,
    )


async def post_chat(
    client: httpx.AsyncClient, slot: KeySlot, body: dict[str, Any], *, timeout_s: float
) -> dict[str, Any]:
    """POST ``chat/completions`` with one key, bounded by ``guarded_call``."""

    async def _call() -> dict[str, Any]:
        response = await client.post(
            "chat/completions",
            json=body,
            headers={"Authorization": f"Bearer {slot.secret}"},
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError("chat completion body was not a JSON object")
        return payload

    # attempts=1: failover is across keys, not repeats on one key, and a
    # repeat on a quota-limited key is exactly the wait the callers forbid.
    return await guarded_call(_call, dependency=slot.dependency, timeout_s=timeout_s, attempts=1)


def describe(exc: BaseException) -> str:
    """Error text for logs and status. httpx's message carries the URL and
    status, never the Authorization header; the key is not in the URL."""
    status = getattr(getattr(exc, "response", None), "status_code", None)
    return f"{type(exc).__name__}{f' {status}' if status else ''}"


@dataclass(slots=True)
class RingOutcome[T]:
    value: T | None
    key: str | None = None
    keys_tried: int = 0
    fault: KeyFault | None = None
    error: str | None = None


async def walk_ring[T](
    ring: KeyRing,
    attempt: Callable[[KeySlot], Awaitable[T]],
    *,
    what: str,
    skip_when_all_parked: bool,
) -> RingOutcome[T]:
    """Try ``attempt`` per usable key until one succeeds.

    ``skip_when_all_parked`` is the compactor's rule: when every key is parked
    it returns at once instead of re-asking throttled keys, because its
    fallback (the rule compactor) is instant and free.
    """
    if skip_when_all_parked and ring.ready_count == 0:
        return RingOutcome(None, fault=KeyFault.QUOTA, error="every key in the pool is parked")
    tried = 0
    last_fault: KeyFault | None = None
    last_error: str | None = None
    for slot in ring.slots():
        tried += 1
        try:
            value = await attempt(slot)
        except Exception as exc:  # noqa: BLE001 - classified, then parked or stopped
            last_fault = classify(exc)
            last_error = describe(exc)
            log.warning(
                f"{what} call failed", key=slot.label, pool=ring.pool, fault=last_fault.value,
                error=last_error,
            )
            if last_fault is KeyFault.REQUEST:
                break  # every key would reject this identically
            ring.park(slot, last_fault)
            continue
        ring.release(slot)
        return RingOutcome(value, key=slot.label, keys_tried=tried)
    return RingOutcome(None, keys_tried=tried, fault=last_fault, error=last_error)


__all__ = ["RingOutcome", "build_client", "describe", "model_name", "post_chat", "walk_ring"]
