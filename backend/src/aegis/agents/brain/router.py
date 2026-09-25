"""The brain router: Bedrock, then Gemini, then the scripted policy.

The router is the only ``Brain`` the orchestrator holds, and it keeps the
protocol's promise that a decision always comes back. Three rules it enforces
whatever a tier returns:

* **The label is the truth.** ``source`` is the tier that actually answered,
  set here rather than trusted from the tier, and every skipped or failed tier
  is written into ``fallback_reason`` and counted.
* **The model cannot invent tools.** A call whose name is not in the request's
  tool list is dropped and counted before the orchestrator sees it. A tier
  whose every call was invented has failed, not answered.
* **A refusal is a failure of that tier**, so the next tier gets the step.

Live tiers share one deadline that leaves room for the scripted policy, so a
slow Bedrock followed by a slow Gemini still ends in a decision inside the
orchestrator's step timeout.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any, Final, Protocol

from aegis.agents.brain.bedrock import BedrockBrain
from aegis.agents.brain.gemini import GeminiBrain
from aegis.agents.horizon.ports import Brain, BrainDecision, BrainRequest
from aegis.core.clock import SYSTEM_CLOCK, Clock
from aegis.core.config import Settings
from aegis.core.errors import AegisError
from aegis.core.logging import get_logger
from aegis.core.resilience import with_timeout
from aegis.domain.horizon import HorizonState, Source

log = get_logger(__name__)

# Share of the orchestrator's step timeout the live tiers may spend between
# them; the remainder is the scripted policy's, which needs milliseconds.
_LIVE_SHARE: Final = 0.9
_MIN_TIER_S: Final = 1.0


class LiveTier(Protocol):
    """What the router needs from Bedrock and Gemini. ``step`` may raise
    ``BrainUnavailable``; anything else it raises is treated the same way."""

    @property
    def configured(self) -> bool: ...

    @property
    def reason(self) -> str: ...

    async def step(self, request: BrainRequest, state: HorizonState) -> BrainDecision: ...

    def status(self) -> dict[str, Any]: ...


class BrainRouter:
    """Implements ``Brain``. Never raises for provider failure."""

    def __init__(
        self,
        settings: Settings,
        *,
        scripted: Brain,
        clock: Clock = SYSTEM_CLOCK,
        bedrock: LiveTier | None = None,
        gemini: LiveTier | None = None,
    ) -> None:
        self._settings = settings
        self._scripted = scripted
        self._clock = clock
        self._bedrock: LiveTier = bedrock if bedrock is not None else BedrockBrain(
            settings, clock=clock
        )
        self._gemini: LiveTier = gemini if gemini is not None else GeminiBrain(
            settings, clock=clock
        )
        self._calls: dict[str, int] = {s.value: 0 for s in (Source.BEDROCK, Source.GEMINI,
                                                             Source.SCRIPTED)}
        self._failures: dict[str, int] = {"bedrock": 0, "gemini": 0}
        self._last_error: dict[str, str | None] = {"bedrock": None, "gemini": None}
        self._fallbacks = 0
        self._dropped = 0
        self._cache_read = 0
        self._cache_write = 0
        self._last_source: str | None = None

    @property
    def mode(self) -> str:
        return self._settings.aegis_mode

    # ------------------------------------------------------------------ #
    # step                                                                #
    # ------------------------------------------------------------------ #

    async def step(self, request: BrainRequest, state: HorizonState) -> BrainDecision:
        reasons: list[str] = []
        if self.mode == "live":
            deadline = self._clock.monotonic() + self._settings.horizon_step_timeout_s * _LIVE_SHARE
            for name, tier in (("bedrock", self._bedrock), ("gemini", self._gemini)):
                decision = await self._try_tier(name, tier, request, state, deadline, reasons)
                if decision is not None:
                    return self._account(decision, reasons)
        return self._account(await self._run_scripted(request, state, reasons), reasons)

    async def _try_tier(
        self,
        name: str,
        tier: LiveTier,
        request: BrainRequest,
        state: HorizonState,
        deadline: float,
        reasons: list[str],
    ) -> BrainDecision | None:
        if not tier.configured:
            reasons.append(f"{name} skipped: {tier.reason}")
            return None
        remaining = deadline - self._clock.monotonic()
        if remaining < _MIN_TIER_S:
            reasons.append(f"{name} skipped: step deadline spent")
            return None
        try:
            decision = await with_timeout(
                tier.step(request, state), remaining, what=f"brain:{name}"
            )
        except AegisError as exc:  # BrainUnavailable, TimeoutExceeded, CircuitOpen
            self._tier_failed(name, f"{exc.message}", reasons)
            return None
        except Exception as exc:  # noqa: BLE001 - a tier defect must not halt the incident
            log.exception("brain tier raised unexpectedly", tier=name)
            self._tier_failed(name, f"unexpected {type(exc).__name__}", reasons)
            return None

        if decision.stop_reason == "refusal" and not decision.tool_calls:
            self._tier_failed(name, f"refused ({decision.text or 'no detail'})", reasons)
            return None
        filtered = self._filter(decision, request)
        if decision.tool_calls and not filtered.tool_calls:
            self._tier_failed(name, "proposed only tools it was not offered", reasons)
            return None
        self._last_error[name] = None
        return replace(filtered, source=Source(name))

    def _tier_failed(self, name: str, why: str, reasons: list[str]) -> None:
        self._failures[name] += 1
        self._last_error[name] = why[:300]
        reasons.append(f"{name} failed: {why}"[:300])
        log.warning("brain tier failed; falling back", tier=name, reason=why[:300])

    async def _run_scripted(
        self, request: BrainRequest, state: HorizonState, reasons: list[str]
    ) -> BrainDecision:
        try:
            decision = await self._scripted.step(request, state)
        except Exception as exc:  # noqa: BLE001 - the last tier; an empty decision beats a crash
            log.exception("scripted brain raised")
            reasons.append(f"scripted failed: {type(exc).__name__}")
            return BrainDecision(tool_calls=(), source=Source.SCRIPTED, stop_reason="error")
        return replace(self._filter(decision, request), source=Source.SCRIPTED)

    def _filter(self, decision: BrainDecision, request: BrainRequest) -> BrainDecision:
        offered = {spec.name for spec in request.tools}
        kept = [call for call in decision.tool_calls if call.name in offered]
        dropped = len(decision.tool_calls) - len(kept)
        if dropped:
            self._dropped += dropped
            log.warning(
                "brain proposed tools it was not offered; dropped",
                source=decision.source.value,
                names=sorted({c.name for c in decision.tool_calls if c.name not in offered})[:8],
            )
        kept = kept[: request.max_tool_calls]
        return replace(decision, tool_calls=tuple(kept))

    def _account(self, decision: BrainDecision, reasons: list[str]) -> BrainDecision:
        # A tier's own model- or key-level fallback note is kept after the
        # tier-level ones, so the reason reads in the order things happened.
        notes = [*reasons, *([decision.fallback_reason] if decision.fallback_reason else [])]
        if reasons:
            self._fallbacks += 1
        self._calls[decision.source.value] = self._calls.get(decision.source.value, 0) + 1
        self._cache_read += decision.cache_read_tokens
        self._cache_write += decision.cache_write_tokens
        self._last_source = decision.source.value
        return replace(decision, fallback_reason="; ".join(notes)[:1000] if notes else None)

    # ------------------------------------------------------------------ #
    # reporting                                                           #
    # ------------------------------------------------------------------ #

    def status(self) -> dict[str, Any]:
        """Per-tier readiness and counters. Tier status payloads name models,
        regions and key *labels*; none carries a credential."""
        bedrock = dict(self._bedrock.status())
        gemini = dict(self._gemini.status())
        bedrock["last_error"] = self._last_error["bedrock"] or bedrock.get("last_error")
        gemini["last_error"] = self._last_error["gemini"] or gemini.get("last_error")
        return {
            "mode": self.mode,
            "tiers": [
                {**bedrock, "name": "bedrock", "failures": self._failures["bedrock"]},
                {**gemini, "name": "gemini", "failures": self._failures["gemini"]},
                {"name": "scripted", "configured": True, "reason": "always available",
                 "model": "scripted-policy", "last_error": None},
            ],
            "calls": dict(self._calls),
            "fallbacks": self._fallbacks,
            "dropped_tool_calls": self._dropped,
            "cache_read_tokens": self._cache_read,
            "cache_write_tokens": self._cache_write,
            "last_source": self._last_source,
        }


__all__ = ["BrainRouter", "LiveTier"]
