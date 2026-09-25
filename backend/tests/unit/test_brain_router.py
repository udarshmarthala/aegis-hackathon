"""The brain router: tier order, honest labels, invented tools dropped.

Tiers are fakes with the ``LiveTier`` shape so each test states exactly which
tier fails and how; the router under test is the real one.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from aegis.agents.brain.errors import BrainUnavailable
from aegis.agents.brain.router import BrainRouter
from aegis.agents.horizon.ports import BrainDecision, BrainRequest, ToolCall, ToolSpec
from aegis.core.config import Settings
from aegis.domain.horizon import HorizonState, Source

STATE = HorizonState(run_id="r", incident_id="INC-1", service="checkout")
TOOLS = (ToolSpec("observe_metrics", "m", {"type": "object"}),
         ToolSpec("propose_action", "p", {"type": "object"}))
REQ = BrainRequest(system="S", user="U", tools=TOOLS, phase="X", step=1, max_tool_calls=2)
SENTINEL = "SENTINEL-KEY-VALUE"


def rsettings(mode: str = "live", **over: Any) -> Settings:
    return Settings(aegis_mode=mode, horizon_step_timeout_s=10.0, **over)


def call(name: str) -> ToolCall:
    return ToolCall(id=name, name=name, arguments={})


class Tier:
    def __init__(self, name: str, outcome: Any, configured: bool = True) -> None:
        self.name = name
        self.outcome = outcome
        self._configured = configured
        self.calls = 0

    @property
    def configured(self) -> bool:
        return self._configured

    @property
    def reason(self) -> str:
        return "configured" if self._configured else f"{self.name} not configured"

    async def step(self, request: BrainRequest, state: HorizonState) -> BrainDecision:
        self.calls += 1
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        if callable(self.outcome):
            return await self.outcome()
        assert isinstance(self.outcome, BrainDecision)
        return self.outcome

    def status(self) -> dict[str, Any]:
        return {"name": self.name, "configured": self._configured, "model": "m",
                "last_error": None}


def decision(source: Source, *names: str, **kw: Any) -> BrainDecision:
    return BrainDecision(tool_calls=tuple(call(n) for n in names), source=source, **kw)


SCRIPTED = Tier("scripted", decision(Source.SCRIPTED, "observe_metrics"))


def router(bedrock: Tier, gemini: Tier, mode: str = "live",
           scripted: Tier = SCRIPTED) -> BrainRouter:
    return BrainRouter(rsettings(mode), scripted=scripted, bedrock=bedrock, gemini=gemini)


async def test_bedrock_answers_first_and_is_labelled() -> None:
    b = Tier("bedrock", decision(Source.BEDROCK, "observe_metrics", cache_read_tokens=900))
    g = Tier("gemini", decision(Source.GEMINI, "observe_metrics"))
    r = router(b, g)
    d = await r.step(REQ, STATE)
    assert d.source is Source.BEDROCK and d.fallback_reason is None
    assert g.calls == 0
    assert r.status()["cache_read_tokens"] == 900


async def test_forced_bedrock_failure_falls_to_gemini_then_scripted() -> None:
    b = Tier("bedrock", BrainUnavailable("every Bedrock model failed"))
    g = Tier("gemini", decision(Source.GEMINI, "propose_action"))
    d = await router(b, g).step(REQ, STATE)
    assert d.source is Source.GEMINI
    assert d.fallback_reason is not None and "bedrock failed" in d.fallback_reason

    g2 = Tier("gemini", BrainUnavailable("pool exhausted"))
    r = router(b, g2)
    d2 = await r.step(REQ, STATE)
    assert d2.source is Source.SCRIPTED
    assert "bedrock failed" in (d2.fallback_reason or "")
    assert "gemini failed" in (d2.fallback_reason or "")
    assert r.status()["fallbacks"] == 1
    assert r.status()["calls"]["scripted"] == 1


async def test_unconfigured_tiers_are_skipped_and_recorded() -> None:
    b = Tier("bedrock", decision(Source.BEDROCK, "observe_metrics"), configured=False)
    g = Tier("gemini", decision(Source.GEMINI, "observe_metrics"), configured=False)
    d = await router(b, g).step(REQ, STATE)
    assert d.source is Source.SCRIPTED
    assert b.calls == g.calls == 0
    assert "bedrock skipped" in (d.fallback_reason or "")


async def test_scripted_mode_never_touches_a_provider() -> None:
    b = Tier("bedrock", decision(Source.BEDROCK, "observe_metrics"))
    g = Tier("gemini", decision(Source.GEMINI, "observe_metrics"))
    d = await router(b, g, mode="scripted").step(REQ, STATE)
    assert d.source is Source.SCRIPTED and d.fallback_reason is None
    assert b.calls == g.calls == 0


async def test_refusal_is_a_failure_of_that_tier() -> None:
    b = Tier("bedrock", decision(Source.BEDROCK, stop_reason="refusal", text="refused"))
    g = Tier("gemini", decision(Source.GEMINI, "observe_metrics"))
    d = await router(b, g).step(REQ, STATE)
    assert d.source is Source.GEMINI
    assert "refused" in (d.fallback_reason or "")


async def test_invented_tools_are_dropped_and_counted() -> None:
    b = Tier("bedrock", decision(Source.BEDROCK, "observe_metrics", "delete_database",
                                 "propose_action", "observe_metrics"))
    r = router(b, Tier("gemini", decision(Source.GEMINI)))
    d = await r.step(REQ, STATE)
    assert [c.name for c in d.tool_calls] == ["observe_metrics", "propose_action"]  # capped at 2
    assert r.status()["dropped_tool_calls"] == 1


async def test_a_tier_that_only_invents_tools_has_failed() -> None:
    b = Tier("bedrock", decision(Source.BEDROCK, "grant_admin"))
    g = Tier("gemini", decision(Source.GEMINI, "observe_metrics"))
    d = await router(b, g).step(REQ, STATE)
    assert d.source is Source.GEMINI


async def test_a_tier_label_cannot_be_spoofed() -> None:
    """The router, not the tier, decides the label."""
    b = Tier("bedrock", decision(Source.SCRIPTED, "observe_metrics"))
    d = await router(b, Tier("gemini", decision(Source.GEMINI))).step(REQ, STATE)
    assert d.source is Source.BEDROCK


async def test_unexpected_tier_exception_and_slow_tier_still_yield_a_decision() -> None:
    async def slow() -> BrainDecision:
        await asyncio.sleep(30)
        raise AssertionError("unreachable")

    b = Tier("bedrock", RuntimeError("bug"))
    g = Tier("gemini", slow)
    r = BrainRouter(Settings(aegis_mode="live", horizon_step_timeout_s=4.0),
                    scripted=SCRIPTED, bedrock=b, gemini=g)
    d = await asyncio.wait_for(r.step(REQ, STATE), timeout=6.0)
    assert d.source is Source.SCRIPTED


async def test_scripted_failure_never_raises() -> None:
    broken = Tier("scripted", RuntimeError("bug"))
    d = await router(Tier("b", None, False), Tier("g", None, False), scripted=broken).step(
        REQ, STATE
    )
    assert d.source is Source.SCRIPTED and d.tool_calls == ()


def test_status_shape_and_no_secrets() -> None:
    s = rsettings(google_api_key_6=SENTINEL, aws_secret_access_key=SENTINEL,
                  aws_access_key_id="AKIA-SENTINEL", bedrock_model_id="m1")
    r = BrainRouter(s, scripted=SCRIPTED)  # real tiers, built from settings
    status = r.status()
    assert status["mode"] == "live"
    assert [t["name"] for t in status["tiers"]] == ["bedrock", "gemini", "scripted"]
    assert {"calls", "fallbacks", "cache_read_tokens"} <= set(status)
    assert SENTINEL not in repr(status) and "AKIA-SENTINEL" not in repr(status)


@pytest.mark.parametrize("mode", ["live", "scripted"])
def test_router_satisfies_the_brain_protocol(mode: str) -> None:
    from aegis.agents.horizon.ports import Brain

    assert isinstance(router(SCRIPTED, SCRIPTED, mode=mode), Brain)
