"""Failover across several API keys for a single provider.

Aegis talks to exactly one model vendor, Google AI Studio. Redundancy therefore
cannot come from a second vendor - it comes from a second *key*. That is not a
downgrade: multi-vendor fallback was never a different capability here, only a
different account, and each extra dialect carried its own model-id namespace, so
an id valid for the primary endpoint 404'd on the fallback - disabling failover
at precisely the moment the primary was failing.

Gemini quotas are enforced per key, per minute and per day. So the unit that
gets exhausted is the key, and the unit that must fail over is the key.

The distinction this module exists to make is between a fault that *another key
would survive* and a fault that *every key shares*:

* a 429 or a rejected credential is the key's problem - park it, advance;
* a 400 or an unparseable response is the request's problem - advancing would
  burn all four keys on an identical failure and hide the real cause.

Parking is bounded and time-based rather than permanent. A per-minute quota
recovers on its own, and a key parked forever after one bad minute would quietly
shrink a four-key ring to a one-key ring with no signal that it had happened.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Final

from aegis.core.clock import SYSTEM_CLOCK, Clock
from aegis.core.config import Settings
from aegis.core.logging import get_logger

log = get_logger(__name__)

# How long a key stays parked, by fault. A per-minute quota clears within a
# minute; a per-day quota does not, but re-testing once a minute costs one
# rejected request and is how the ring notices the quota came back.
_PARK_SECONDS: Final[dict[str, float]] = {
    "quota": 60.0,
    # A rejected credential will not fix itself on the next request. Parking it
    # for a quarter hour keeps a typo'd key from consuming a slot on every call,
    # while still letting a key that was rotated in place recover without a
    # restart.
    "auth": 900.0,
    # Transport and 5xx faults are the provider's, not the key's. A short park
    # spreads load off the affected slot without taking it out of service.
    "transient": 15.0,
}


class KeyFault(StrEnum):
    """Why a call failed, expressed as what to do about it."""

    QUOTA = "quota"          # this key is rate limited; another may not be
    AUTH = "auth"            # this key is rejected; another may be accepted
    TRANSIENT = "transient"  # provider-side or transport; another key may work
    REQUEST = "request"      # the request itself is wrong; every key agrees


def _status_of(exc: BaseException) -> int | None:
    """Best-effort HTTP status for an exception from any client library.

    The OpenAI SDK exposes ``status_code`` on ``APIStatusError``; httpx carries
    it on ``.response``; Aegis's own ``ExternalServiceError`` puts it in
    ``context["status"]``. All three are read defensively because this must
    never be the thing that raises while classifying someone else's failure.
    """
    status = getattr(exc, "status_code", None)
    if isinstance(status, int):
        return status
    response: Any = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    if isinstance(status, int):
        return status
    context: Any = getattr(exc, "context", None)
    if isinstance(context, dict):
        status = context.get("status")
        if isinstance(status, int):
            return status
    return None


def classify(exc: BaseException) -> KeyFault:
    """Map an exception to the ring's decision about it.

    Unclassifiable failures are TRANSIENT rather than REQUEST: trying the next
    key costs one call, whereas mislabelling a recoverable fault as terminal
    aborts an investigation that would have succeeded.
    """
    status = _status_of(exc)
    if status is not None:
        if status == 429:
            return KeyFault.QUOTA
        if status in (401, 403):
            return KeyFault.AUTH
        if status >= 500:
            return KeyFault.TRANSIENT
        if 400 <= status < 500:
            # 400 (malformed), 404 (no such model) and 422 (schema) are all
            # properties of what was sent. Every key would reject them equally.
            return KeyFault.REQUEST
    # Pydantic rejecting the model's output is a request-shaped fault: the key
    # worked, the answer did not parse.
    if type(exc).__name__ == "ValidationError":
        return KeyFault.REQUEST
    return KeyFault.TRANSIENT


@dataclass(frozen=True, slots=True)
class KeySlot:
    """One credential in the ring.

    ``label`` is what gets logged and reported; the secret itself never appears
    in a log line, a breaker name, an error context or the health payload.
    """

    index: int
    label: str
    dependency: str
    secret: str


@dataclass(slots=True)
class _SlotState:
    parked_until: float = 0.0
    last_fault: KeyFault | None = None
    faults: int = 0


class KeyRing:
    """An ordered, self-healing ring of provider credentials.

    Callers iterate ``slots()`` in priority order and report each failure with
    ``park``. The ring owns only the decision of *which key next*; retries,
    timeouts and circuit breaking stay with ``core.resilience``, one breaker per
    slot so that one exhausted key cannot trip the whole provider.
    """

    __slots__ = ("_keys", "_purpose", "_clock", "_state")

    def __init__(
        self,
        settings: Settings,
        *,
        purpose: str,
        clock: Clock = SYSTEM_CLOCK,
    ) -> None:
        self._keys = settings.google_api_keys
        self._purpose = purpose
        self._clock = clock
        self._state: dict[int, _SlotState] = {
            i: _SlotState() for i in range(len(self._keys))
        }

    # ------------------------------------------------------------------ #
    # shape                                                               #
    # ------------------------------------------------------------------ #

    @property
    def configured(self) -> bool:
        return bool(self._keys)

    @property
    def size(self) -> int:
        return len(self._keys)

    def _slot(self, index: int) -> KeySlot:
        label = f"key{index + 1}"
        return KeySlot(
            index=index,
            label=label,
            # One breaker per key. A shared breaker would let a single
            # exhausted free-tier key open the circuit for three healthy ones.
            dependency=f"{self._purpose}:google:{label}",
            secret=self._keys[index],
        )

    # ------------------------------------------------------------------ #
    # selection                                                           #
    # ------------------------------------------------------------------ #

    def slots(self) -> list[KeySlot]:
        """Usable keys, primary first, parked ones omitted.

        If every key is parked the ring returns them all anyway. A ring that
        went empty would turn a transient quota minute into a hard outage, and
        an attempt that gets rejected again is cheaper than an investigation
        that never ran.
        """
        now = self._clock.monotonic()
        ready = [
            self._slot(i)
            for i in range(len(self._keys))
            if self._state[i].parked_until <= now
        ]
        if ready:
            return ready
        log.warning(
            "every api key is parked; retrying anyway",
            purpose=self._purpose,
            keys=self.size,
        )
        return [self._slot(i) for i in range(len(self._keys))]

    def park(self, slot: KeySlot, fault: KeyFault) -> None:
        """Take a key out of rotation for the window its fault deserves."""
        if fault is KeyFault.REQUEST:
            return  # not this key's fault; parking it would punish the healthy
        state = self._state[slot.index]
        state.parked_until = self._clock.monotonic() + _PARK_SECONDS[fault.value]
        state.last_fault = fault
        state.faults += 1
        log.warning(
            "api key parked",
            purpose=self._purpose,
            key=slot.label,
            fault=fault.value,
            seconds=_PARK_SECONDS[fault.value],
        )

    def release(self, slot: KeySlot) -> None:
        """Return a key to rotation after a success."""
        self._state[slot.index].parked_until = 0.0

    # ------------------------------------------------------------------ #
    # reporting                                                           #
    # ------------------------------------------------------------------ #

    def status(self) -> list[dict[str, Any]]:
        """Per-key health, with no part of any secret in it."""
        now = self._clock.monotonic()
        out: list[dict[str, Any]] = []
        for index in range(len(self._keys)):
            state = self._state[index]
            parked = state.parked_until > now
            out.append(
                {
                    "key": f"key{index + 1}",
                    "state": "parked" if parked else "ready",
                    "parked_for_s": (
                        round(state.parked_until - now, 1) if parked else 0.0
                    ),
                    "last_fault": state.last_fault.value if state.last_fault else None,
                    "faults": state.faults,
                }
            )
        return out

    @property
    def ready_count(self) -> int:
        now = self._clock.monotonic()
        return sum(1 for s in self._state.values() if s.parked_until <= now)


__all__ = ["KeyFault", "KeyRing", "KeySlot", "classify"]
