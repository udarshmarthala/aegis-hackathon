"""Bounded failure handling for every outbound call.

The rules this module exists to enforce (CLAUDE.md section 4):

* no unbounded await - everything has a deadline
* retries only for errors explicitly marked retryable, with full jitter
* a failing dependency trips a breaker instead of consuming the whole pool
* concurrency into any one dependency is capped by a bulkhead

Breaker state is process-local by design. A shared breaker would need a network
round trip to decide whether to make a network call, which adds the very failure
mode it is meant to contain.
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import ParamSpec, TypeVar

from aegis.core.clock import SYSTEM_CLOCK, Clock
from aegis.core.errors import AegisError, CircuitOpen, TimeoutExceeded
from aegis.core.logging import get_logger

log = get_logger(__name__)

P = ParamSpec("P")
T = TypeVar("T")


# --------------------------------------------------------------------------- #
# timeout                                                                      #
# --------------------------------------------------------------------------- #


async def with_timeout(awaitable: Awaitable[T], seconds: float, *, what: str) -> T:
    """Await with a hard deadline, converting to a typed error."""
    try:
        return await asyncio.wait_for(awaitable, timeout=seconds)
    except TimeoutError as exc:
        raise TimeoutExceeded(
            f"{what} exceeded {seconds:.1f}s", context={"operation": what, "timeout_s": seconds}
        ) from exc


# --------------------------------------------------------------------------- #
# retry                                                                        #
# --------------------------------------------------------------------------- #


async def retry_async(
    fn: Callable[[], Awaitable[T]],
    *,
    attempts: int = 3,
    base_delay: float = 0.2,
    max_delay: float = 5.0,
    what: str = "operation",
) -> T:
    """Retry ``fn`` while it raises *retryable* errors.

    Full jitter (``sleep = random(0, min(cap, base * 2**n))``) is used rather
    than equal backoff so that a fleet of workers recovering from a shared
    outage does not resynchronise into a thundering herd.

    A non-retryable error propagates immediately - notably every write action,
    which must never be retried automatically.
    """
    if attempts < 1:
        raise ValueError("attempts must be >= 1")

    last: BaseException | None = None
    for attempt in range(attempts):
        try:
            return await fn()
        except AegisError as exc:
            if not exc.retryable:
                raise
            last = exc
        except (TimeoutError, ConnectionError, OSError) as exc:
            last = exc

        if attempt == attempts - 1:
            break
        delay = random.uniform(0, min(max_delay, base_delay * (2**attempt)))  # noqa: S311
        log.warning(
            "retrying", operation=what, attempt=attempt + 1, of=attempts,
            delay_s=round(delay, 3), error=str(last),
        )
        await asyncio.sleep(delay)

    assert last is not None
    raise last


# --------------------------------------------------------------------------- #
# circuit breaker                                                              #
# --------------------------------------------------------------------------- #


class BreakerState(StrEnum):
    CLOSED = "closed"        # healthy, calls flow
    OPEN = "open"            # failing, calls rejected immediately
    HALF_OPEN = "half_open"  # probing with a single call


@dataclass
class CircuitBreaker:
    """Per-dependency breaker.

    Opens after ``failure_threshold`` consecutive failures, rejects for
    ``recovery_timeout`` seconds, then admits exactly one probe. A successful
    probe closes it; a failed probe re-opens it for another full interval.
    """

    name: str
    failure_threshold: int = 5
    recovery_timeout: float = 30.0
    clock: Clock = field(default=SYSTEM_CLOCK)

    _state: BreakerState = field(default=BreakerState.CLOSED, init=False)
    _failures: int = field(default=0, init=False)
    _opened_at: float = field(default=0.0, init=False)
    _probing: bool = field(default=False, init=False)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False)

    @property
    def state(self) -> BreakerState:
        return self._state

    async def call(self, fn: Callable[[], Awaitable[T]]) -> T:
        await self._before()
        try:
            result = await fn()
        except Exception:
            await self._on_failure()
            raise
        await self._on_success()
        return result

    async def _before(self) -> None:
        async with self._lock:
            if self._state is BreakerState.OPEN:
                elapsed = self.clock.monotonic() - self._opened_at
                if elapsed < self.recovery_timeout:
                    raise CircuitOpen(
                        f"circuit {self.name} is open",
                        context={
                            "dependency": self.name,
                            "retry_in_s": round(self.recovery_timeout - elapsed, 1),
                        },
                    )
                self._state = BreakerState.HALF_OPEN
                self._probing = False

            if self._state is BreakerState.HALF_OPEN:
                if self._probing:
                    raise CircuitOpen(
                        f"circuit {self.name} is probing",
                        context={"dependency": self.name},
                    )
                self._probing = True

    async def _on_success(self) -> None:
        async with self._lock:
            self._failures = 0
            self._probing = False
            if self._state is not BreakerState.CLOSED:
                log.info("circuit closed", dependency=self.name)
            self._state = BreakerState.CLOSED

    async def _on_failure(self) -> None:
        async with self._lock:
            self._failures += 1
            self._probing = False
            if self._state is BreakerState.HALF_OPEN or self._failures >= self.failure_threshold:
                if self._state is not BreakerState.OPEN:
                    log.error(
                        "circuit opened", dependency=self.name, consecutive_failures=self._failures
                    )
                self._state = BreakerState.OPEN
                self._opened_at = self.clock.monotonic()


_BREAKERS: dict[str, CircuitBreaker] = {}


def get_breaker(
    name: str, *, failure_threshold: int = 5, recovery_timeout: float = 30.0
) -> CircuitBreaker:
    """Return the process-wide breaker for a dependency, creating it on demand."""
    if name not in _BREAKERS:
        _BREAKERS[name] = CircuitBreaker(
            name=name, failure_threshold=failure_threshold, recovery_timeout=recovery_timeout
        )
    return _BREAKERS[name]


def breaker_states() -> dict[str, str]:
    """Snapshot for the integration-health surface and Prometheus export."""
    return {name: br.state.value for name, br in _BREAKERS.items()}


# --------------------------------------------------------------------------- #
# bulkhead                                                                     #
# --------------------------------------------------------------------------- #


class Bulkhead:
    """Caps concurrent in-flight calls to one dependency.

    Without this, a single slow dependency absorbs every worker task and starves
    unrelated work. ``acquire_timeout`` ensures callers fail fast rather than
    queueing without bound.
    """

    __slots__ = ("_sem", "_name", "_acquire_timeout", "_limit")

    def __init__(self, name: str, limit: int, *, acquire_timeout: float = 5.0) -> None:
        if limit < 1:
            raise ValueError("bulkhead limit must be >= 1")
        self._name = name
        self._limit = limit
        self._sem = asyncio.Semaphore(limit)
        self._acquire_timeout = acquire_timeout

    @property
    def available(self) -> int:
        return self._sem._value  # noqa: SLF001 - introspection for health only

    async def __aenter__(self) -> None:
        try:
            await asyncio.wait_for(self._sem.acquire(), timeout=self._acquire_timeout)
        except TimeoutError as exc:
            raise TimeoutExceeded(
                f"bulkhead {self._name} saturated",
                context={"dependency": self._name, "limit": self._limit},
            ) from exc

    async def __aexit__(self, *_exc: object) -> None:
        self._sem.release()


# --------------------------------------------------------------------------- #
# composed guard                                                               #
# --------------------------------------------------------------------------- #


async def guarded_call(
    fn: Callable[[], Awaitable[T]],
    *,
    dependency: str,
    timeout_s: float,
    attempts: int = 3,
    bulkhead: Bulkhead | None = None,
) -> T:
    """The single entry point every outbound integration call should use.

    Applies, in order: bulkhead -> breaker -> retry -> timeout. The breaker wraps
    the retry loop so that a sustained outage trips the breaker once rather than
    multiplying every failure by the retry count.
    """
    br = get_breaker(dependency)

    async def _attempt() -> T:
        return await with_timeout(fn(), timeout_s, what=dependency)

    async def _retried() -> T:
        return await retry_async(_attempt, attempts=attempts, what=dependency)

    if bulkhead is None:
        return await br.call(_retried)
    async with bulkhead:
        return await br.call(_retried)


def reset_breakers() -> None:
    """Test helper - clears all breaker state between cases."""
    _BREAKERS.clear()


__all__ = [
    "Bulkhead",
    "BreakerState",
    "CircuitBreaker",
    "breaker_states",
    "get_breaker",
    "guarded_call",
    "reset_breakers",
    "retry_async",
    "with_timeout",
]
