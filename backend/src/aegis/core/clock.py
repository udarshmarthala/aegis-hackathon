"""Injectable time.

No module anywhere in Aegis calls ``datetime.now()`` directly. Time enters the
system through a ``Clock``, which is what makes approval expiry, lease TTLs,
rate limits and budget exhaustion deterministically testable.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Protocol


class Clock(Protocol):
    def now(self) -> datetime:
        """Current time, always timezone-aware UTC."""
        ...

    def monotonic(self) -> float:
        """Seconds from an arbitrary origin; immune to wall-clock jumps."""
        ...


class SystemClock:
    """Production clock."""

    __slots__ = ()

    def now(self) -> datetime:
        return datetime.now(UTC)

    def monotonic(self) -> float:
        import time

        return time.monotonic()


class FrozenClock:
    """Test clock. Time advances only when the test says so."""

    __slots__ = ("_now", "_mono")

    def __init__(self, start: datetime | None = None) -> None:
        self._now = start or datetime(2026, 1, 1, tzinfo=UTC)
        self._mono = 0.0

    def now(self) -> datetime:
        return self._now

    def monotonic(self) -> float:
        return self._mono

    def advance(self, seconds: float) -> None:
        self._now += timedelta(seconds=seconds)
        self._mono += seconds


SYSTEM_CLOCK: Clock = SystemClock()
