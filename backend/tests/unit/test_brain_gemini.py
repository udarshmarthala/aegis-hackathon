"""Gemini brain tier over ``httpx.MockTransport``: tool-name mapping, schema
reduction, key failover within the brain pool only, and no secret leakage."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from aegis.agents.brain.errors import BrainUnavailable
from aegis.agents.brain.gemini import GeminiBrain, gemini_schema, safe_tool_names
from aegis.agents.horizon.ports import BrainRequest, ToolSpec
from aegis.core.clock import FrozenClock
from aegis.core.config import Settings
from aegis.core.resilience import reset_breakers
from aegis.domain.horizon import HorizonState, Source

BRAIN_KEYS = ["SENTINEL-BRAIN-6", "SENTINEL-BRAIN-7", "SENTINEL-BRAIN-8"]


def gsettings(**over: Any) -> Settings:
    base: dict[str, Any] = {
        "google_api_key": "SENTINEL-COMPACT-1",
        "google_api_key_2": "",
        "google_api_key_3": "",
        "google_api_key_4": "",
        "google_api_key_5": "",
        "google_api_key_6": BRAIN_KEYS[0],
        "google_api_key_7": BRAIN_KEYS[1],
        "google_api_key_8": BRAIN_KEYS[2],
        "gemini_compactor_keys": "1,2,3,4,5",
        "gemini_brain_keys": "6,7,8",
        "google_base_url": "https://gemini.test/v1beta/openai/",
        "llm_model_reasoning": "google/gemini-test",
        "llm_request_timeout_s": 2.0,
    }
    base.update(over)
    return Settings(**base)


STATE = HorizonState(run_id="r", incident_id="INC-1", service="checkout")
TOOLS = (
    ToolSpec("observe_metrics", "metrics", {
        "type": "object",
        "properties": {"service": {"type": ["string", "null"]}, "mode": {"const": "p99"}},
        "required": ["service"],
        "additionalProperties": False,
        "$schema": "https://json-schema.org/draft/2020-12/schema",
    }),
    ToolSpec("rawtree__run-query", "sql", {
        "type": "object", "properties": {"sql": {"type": "string"}}, "required": ["sql"],
    }),
)
REQ = BrainRequest(system="SYS", user="USER", tools=TOOLS, phase="X", step=1)


def completion(tool_calls: list[dict[str, Any]], finish: str = "tool_calls") -> dict[str, Any]:
    return {
        "choices": [{"message": {"content": None, "tool_calls": tool_calls},
                     "finish_reason": finish}],
        "usage": {"prompt_tokens": 100, "completion_tokens": 20},
    }


def call(name: str, arguments: str, id_: str = "c1") -> dict[str, Any]:
    return {"id": id_, "type": "function", "function": {"name": name, "arguments": arguments}}


@pytest.fixture(autouse=True)
def _breakers() -> None:
    reset_breakers()


def transport(responses: list[httpx.Response], seen: list[httpx.Request]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return responses.pop(0)

    return httpx.MockTransport(handler)


def test_dash_names_are_mapped_to_a_safe_alphabet_and_are_unique() -> None:
    names = safe_tool_names((*TOOLS, ToolSpec("rawtree__run_query", "x", {"type": "object"})))
    assert names["rawtree__run-query"] == "rawtree__run_query"
    assert names["rawtree__run_query"] == "rawtree__run_query_2"


def test_schema_is_reduced_to_the_gemini_subset() -> None:
    reduced = gemini_schema(TOOLS[0].input_schema)
    assert "additionalProperties" not in reduced and "$schema" not in reduced
    assert reduced["properties"]["service"] == {"type": "string", "nullable": True}
    assert reduced["properties"]["mode"] == {"enum": ["p99"]}


async def test_tool_calls_are_normalised_and_names_mapped_back() -> None:
    seen: list[httpx.Request] = []
    body = completion([
        call("rawtree__run_query", json.dumps({"sql": "select 1"}), "a"),
        call("observe_metrics", "{not json", "b"),
    ])
    brain = GeminiBrain(gsettings(), transport([httpx.Response(200, json=body)], seen),
                        clock=FrozenClock())
    d = await brain.step(REQ, STATE)
    assert d.source is Source.GEMINI
    assert d.model == "gemini-test"
    assert [(c.name, c.arguments) for c in d.tool_calls] == [
        ("rawtree__run-query", {"sql": "select 1"}),
    ]
    sent = json.loads(seen[0].content)
    assert sent["tool_choice"] == "auto"
    assert {t["function"]["name"] for t in sent["tools"]} == {
        "observe_metrics", "rawtree__run_query",
    }
    assert seen[0].url.path.endswith("/chat/completions")


async def test_quota_fails_over_within_the_brain_pool_only() -> None:
    seen: list[httpx.Request] = []
    ok = completion([call("observe_metrics", '{"service": "a"}')])
    brain = GeminiBrain(
        gsettings(),
        transport([httpx.Response(429), httpx.Response(200, json=ok)], seen),
        clock=FrozenClock(),
    )
    d = await brain.step(REQ, STATE)
    assert len(d.tool_calls) == 1
    used = [r.headers["authorization"].removeprefix("Bearer ") for r in seen]
    assert used == BRAIN_KEYS[:2]  # never the compactor's key
    assert d.fallback_reason is not None


async def test_request_fault_stops_at_first_key_and_raises_typed() -> None:
    seen: list[httpx.Request] = []
    brain = GeminiBrain(gsettings(), transport([httpx.Response(400)], seen), clock=FrozenClock())
    with pytest.raises(BrainUnavailable) as err:
        await brain.step(REQ, STATE)
    assert len(seen) == 1
    blob = str(err.value) + repr(err.value.context) + repr(brain.status())
    assert not any(k in blob for k in BRAIN_KEYS)


async def test_content_filter_is_normalised_to_refusal() -> None:
    seen: list[httpx.Request] = []
    body = completion([], finish="content_filter")
    brain = GeminiBrain(gsettings(), transport([httpx.Response(200, json=body)], seen),
                        clock=FrozenClock())
    d = await brain.step(REQ, STATE)
    assert d.stop_reason == "refusal" and d.tool_calls == ()


def test_empty_brain_pool_is_unconfigured() -> None:
    brain = GeminiBrain(gsettings(google_api_key_6="", google_api_key_7="", google_api_key_8=""))
    assert brain.configured is False
    assert "brain pool" in brain.reason
