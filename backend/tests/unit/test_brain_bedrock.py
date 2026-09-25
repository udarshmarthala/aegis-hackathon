"""Bedrock brain tier: model fallback, caching markers, strict tools, stop reasons.

The client is a fake with the SDK's ``messages.create`` shape; everything
between the request and the transport is the real code.
"""

from __future__ import annotations

from typing import Any

import pytest

from aegis.agents.brain.bedrock import BedrockBrain, decode_response
from aegis.agents.brain.errors import BrainUnavailable
from aegis.agents.horizon.ports import BrainRequest, ToolSpec
from aegis.core.clock import FrozenClock
from aegis.core.config import Settings
from aegis.core.resilience import reset_breakers
from aegis.domain.horizon import HorizonState, Source

PRIMARY = "us.anthropic.claude-sonnet-5"
FALLBACK = "us.anthropic.claude-sonnet-4-6"
SENTINEL_SECRET = "SENTINEL-AWS-SECRET-do-not-leak"


def brain_settings(**over: Any) -> Settings:
    base: dict[str, Any] = {
        "aws_region": "us-west-2",
        "aws_profile": "",
        "aws_access_key_id": "AKIASENTINEL",
        "aws_secret_access_key": SENTINEL_SECRET,
        "bedrock_model_id": PRIMARY,
        "bedrock_fallback_model_id": FALLBACK,
        "bedrock_timeout_s": 2.0,
        "bedrock_max_tokens": 1000,
    }
    base.update(over)
    return Settings(**base)


STATE = HorizonState(run_id="r", incident_id="INC-1", service="checkout")
TOOLS = (
    ToolSpec(
        "observe_metrics",
        "read metrics",
        {
            "type": "object",
            "properties": {
                "service": {"type": "string"},
                "window_s": {"type": "integer", "minimum": 1},
            },
            "required": ["service"],
        },
    ),
    ToolSpec(
        "rawtree__run-query",
        "sql",
        {"type": "object", "properties": {"sql": {"type": "string"}}, "required": ["sql"]},
    ),
)


def req(**over: Any) -> BrainRequest:
    base: dict[str, Any] = {
        "system": "SYS", "user": "USER", "tools": TOOLS, "phase": "X", "step": 1,
    }
    base.update(over)
    return BrainRequest(**base)


class Status(Exception):
    def __init__(self, status: int, message: str = "") -> None:
        super().__init__(message or f"status {status}")
        self.status_code = status


def message(
    content: list[dict[str, Any]], stop_reason: str = "tool_use", **usage: int
) -> dict[str, Any]:
    return {
        "stop_reason": stop_reason,
        "content": content,
        "usage": {
            "input_tokens": 10,
            "output_tokens": 5,
            "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 0,
            **usage,
        },
    }


def tool_use(name: str, args: Any, id_: str = "tu1") -> dict[str, Any]:
    return {"type": "tool_use", "id": id_, "name": name, "input": args}


class FakeClient:
    def __init__(self, outcomes: dict[str, list[Any]]) -> None:
        self.outcomes = outcomes
        self.calls: list[dict[str, Any]] = []
        self.messages = self

    async def create(self, **params: Any) -> Any:
        self.calls.append(params)
        outcome = self.outcomes[params["model"]].pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


@pytest.fixture(autouse=True)
def _breakers() -> None:
    reset_breakers()


def make(
    outcomes: dict[str, list[Any]], clock: FrozenClock | None = None, **over: Any
) -> tuple[BedrockBrain, FakeClient]:
    client = FakeClient(outcomes)
    brain = BedrockBrain(
        brain_settings(**over), client_factory=lambda: client, clock=clock or FrozenClock()
    )
    return brain, client


async def test_403_on_primary_uses_fallback_model_and_labels_it() -> None:
    ok = message([tool_use("observe_metrics", {"service": "checkout"})])
    brain, client = make({PRIMARY: [Status(403)], FALLBACK: [ok]})
    d = await brain.step(req(), STATE)
    assert d.source is Source.BEDROCK
    assert d.model == FALLBACK
    assert d.fallback_reason == (
        "claude-sonnet-5 not enabled for this account; used claude-sonnet-4-6"
    )
    assert [c["model"] for c in client.calls] == [PRIMARY, FALLBACK]


async def test_denial_is_remembered_so_the_403_is_paid_once() -> None:
    clock = FrozenClock()
    ok = message([tool_use("observe_metrics", {"service": "a"})])
    brain, client = make({PRIMARY: [Status(403), message([])], FALLBACK: [ok, ok]}, clock)
    await brain.step(req(), STATE)
    d2 = await brain.step(req(), STATE)
    assert [c["model"] for c in client.calls] == [PRIMARY, FALLBACK, FALLBACK]
    assert d2.fallback_reason is not None and "not enabled" in d2.fallback_reason
    clock.advance(901)  # the denial expires; the primary is probed again
    await brain.step(req(), STATE)
    assert client.calls[-1]["model"] == PRIMARY


async def test_every_model_failing_raises_brain_unavailable() -> None:
    brain, _ = make({PRIMARY: [Status(403)], FALLBACK: [Status(503)]})
    with pytest.raises(BrainUnavailable) as err:
        await brain.step(req(), STATE)
    assert err.value.retryable is False


async def test_unconfigured_model_is_a_typed_skip() -> None:
    brain = BedrockBrain(brain_settings(bedrock_model_id="", bedrock_fallback_model_id=""))
    assert brain.configured is False
    assert "BEDROCK_MODEL_ID" in brain.reason
    with pytest.raises(BrainUnavailable):
        await brain.step(req(), STATE)


def test_cache_control_on_system_and_last_tool_and_tools_are_strict() -> None:
    brain, _ = make({})
    params = brain.build_params(req())
    assert params["system"][-1]["cache_control"] == {"type": "ephemeral"}
    assert params["tools"][-1]["cache_control"] == {"type": "ephemeral"}
    assert "cache_control" not in params["tools"][0]
    assert all(t["strict"] is True for t in params["tools"])
    assert params["tools"][0]["input_schema"]["additionalProperties"] is False
    assert params["tool_choice"] == {"type": "auto"}
    assert "thinking" not in params
    # Deterministic: an identical prefix every step is what makes caching pay.
    assert brain.build_params(req()) == params


def test_high_effort_uses_adaptive_thinking_and_auto_tool_choice() -> None:
    brain, _ = make({})
    params = brain.build_params(req(high_effort=True))
    assert params["thinking"] == {"type": "adaptive"}
    assert params["output_config"] == {"effort": "high"}
    # A forced tool choice is illegal alongside thinking.
    assert params["tool_choice"] == {"type": "auto"}
    assert params["max_tokens"] == 2000


async def test_strict_rejection_retries_once_without_strict() -> None:
    ok = message([tool_use("observe_metrics", {"service": "x"})])
    brain, client = make({PRIMARY: [Status(400, "tools.0: strict schema not supported"), ok]})
    d = await brain.step(req(), STATE)
    assert d.model == PRIMARY
    assert d.fallback_reason is None
    assert "strict" in client.calls[0]["tools"][0]
    assert "strict" not in client.calls[1]["tools"][0]


async def test_invalid_model_400_is_not_mistaken_for_a_strict_rejection() -> None:
    ok = message([tool_use("observe_metrics", {"service": "x"})])
    brain, client = make(
        {PRIMARY: [Status(400, "The provided model identifier is invalid.")], FALLBACK: [ok]}
    )
    d = await brain.step(req(), STATE)
    assert [c["model"] for c in client.calls] == [PRIMARY, FALLBACK]
    assert d.model == FALLBACK


def test_refusal_yields_no_tool_calls_even_if_content_has_some() -> None:
    resp = message([tool_use("observe_metrics", {"service": "x"})], stop_reason="refusal")
    d = decode_response(resp, req(), model=PRIMARY)
    assert d.tool_calls == ()
    assert d.stop_reason == "refusal"


def test_max_tokens_keeps_only_completed_tool_calls() -> None:
    resp = message(
        [
            tool_use("observe_metrics", {"service": "a"}, "1"),
            tool_use("observe_metrics", {"serv": ""}, "2"),
        ],
        stop_reason="max_tokens",
    )
    d = decode_response(resp, req(), model=PRIMARY)
    assert [c.id for c in d.tool_calls] == ["1"]


def test_string_input_is_json_parsed_and_calls_are_capped() -> None:
    resp = message(
        [tool_use("observe_metrics", '{"service": "a"}', str(i)) for i in range(6)]
        + [tool_use("observe_metrics", "not json", "bad")],
    )
    d = decode_response(resp, req(max_tool_calls=4), model=PRIMARY)
    assert len(d.tool_calls) == 4
    assert d.tool_calls[0].arguments == {"service": "a"}


def test_cache_usage_is_reported() -> None:
    resp = message(
        [], stop_reason="end_turn", cache_read_input_tokens=2583, cache_creation_input_tokens=7
    )
    d = decode_response(resp, req(), model=PRIMARY)
    assert (d.cache_read_tokens, d.cache_write_tokens) == (2583, 7)


async def test_no_secret_in_status_or_error_text() -> None:
    brain, _ = make({PRIMARY: [Status(403)], FALLBACK: [Status(500)]})
    with pytest.raises(BrainUnavailable) as err:
        await brain.step(req(), STATE)
    blob = repr(brain.status()) + str(err.value) + repr(err.value.context)
    assert SENTINEL_SECRET not in blob
    assert "AKIASENTINEL" not in blob
    assert brain.status()["auth"] == "access-keys"
