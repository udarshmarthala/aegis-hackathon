"""Gemini on the brain key pool: the second brain tier.

Function calling through Gemini's OpenAI-compatible endpoint. The pool is
``gemini_brain_keys`` only, so a Bedrock outage that moves every step here
cannot starve the compactor, which draws on a different set of slots.

Two dialect gaps are closed here so the orchestrator never sees them:

* Gemini function names are narrower than Anthropic tool names (a ``-`` in
  ``rawtree__run-query`` is rejected). Names are mapped to a safe alphabet for
  the request and mapped back on the way out; an unmappable name is passed
  through untouched so the router drops it as unknown.
* Gemini function declarations accept a subset of JSON Schema. Keywords
  outside it are removed rather than sent, because one unsupported keyword
  rejects the whole request - and that 400 would be identical on every key.
"""

from __future__ import annotations

import json
import re
from typing import Any, Final

import httpx

from aegis.agents.brain._gemini_http import build_client, model_name, post_chat, walk_ring
from aegis.agents.brain.errors import BrainUnavailable
from aegis.agents.horizon.ports import BrainDecision, BrainRequest, ToolCall, ToolSpec
from aegis.core.clock import SYSTEM_CLOCK, Clock
from aegis.core.config import Settings
from aegis.core.keyring import KeyRing, KeySlot
from aegis.core.logging import get_logger
from aegis.domain.horizon import HorizonState, Source

log = get_logger(__name__)

_SAFE_NAME: Final = re.compile(r"[^A-Za-z0-9_]")
_MAX_NAME: Final = 64

# The JSON Schema keywords Gemini function declarations accept.
_SCHEMA_KEYS: Final = frozenset(
    {
        "type", "description", "properties", "required", "items", "enum", "format",
        "nullable", "minimum", "maximum", "minItems", "maxItems", "anyOf",
    }
)


def safe_tool_names(specs: tuple[ToolSpec, ...]) -> dict[str, str]:
    """``original -> safe`` for every tool, collision-free and deterministic."""
    out: dict[str, str] = {}
    used: set[str] = set()
    for spec in specs:
        base = _SAFE_NAME.sub("_", spec.name)
        if not base or not (base[0].isalpha() or base[0] == "_"):
            base = f"t_{base}"
        base = base[:_MAX_NAME]
        name, n = base, 2
        while name in used:
            suffix = f"_{n}"
            name = base[: _MAX_NAME - len(suffix)] + suffix
            n += 1
        used.add(name)
        out[spec.name] = name
    return out


def gemini_schema(schema: Any) -> Any:
    """Reduce a JSON Schema to the subset Gemini declarations accept."""
    if isinstance(schema, list):
        return [gemini_schema(item) for item in schema]
    if not isinstance(schema, dict):
        return schema
    out: dict[str, Any] = {}
    for key, value in schema.items():
        if key == "const":
            out["enum"] = [value]
        elif key == "type" and isinstance(value, list):
            # ["string", "null"] -> string, nullable
            kinds = [v for v in value if v != "null"]
            out["type"] = kinds[0] if kinds else "string"
            if "null" in value:
                out["nullable"] = True
        elif key == "properties" and isinstance(value, dict):
            out["properties"] = {k: gemini_schema(v) for k, v in value.items()}
        elif key in _SCHEMA_KEYS:
            out[key] = gemini_schema(value)
    return out


class GeminiBrain:
    """Gemini function calling across the brain key pool."""

    __slots__ = ("_settings", "_transport", "_ring", "_client", "_last_error", "_clock")

    def __init__(
        self,
        settings: Settings,
        transport: httpx.AsyncBaseTransport | None = None,
        *,
        clock: Clock = SYSTEM_CLOCK,
    ) -> None:
        self._settings = settings
        self._transport = transport
        self._clock = clock
        self._ring = KeyRing(settings, purpose="brain", clock=clock, pool="brain")
        self._client: httpx.AsyncClient | None = None
        self._last_error: str | None = None

    @property
    def configured(self) -> bool:
        return self._ring.configured

    @property
    def reason(self) -> str:
        if not self._ring.configured:
            return f"no Gemini key in the brain pool (slots {self._settings.gemini_brain_keys})"
        return "configured"

    @property
    def model(self) -> str:
        return model_name(self._settings.llm_model_reasoning)

    def status(self) -> dict[str, Any]:
        return {
            "name": "gemini",
            "configured": self.configured,
            "reason": self.reason,
            "model": self.model,
            "keys": self._ring.status(),
            "last_error": self._last_error,
        }

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = build_client(
                self._settings.google_base_url,
                self._settings.llm_request_timeout_s,
                self._transport,
            )
        return self._client

    def build_body(self, request: BrainRequest, names: dict[str, str]) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": request.system},
                {"role": "user", "content": request.user},
            ],
            "temperature": 0,
            "max_tokens": self._settings.llm_max_output_tokens,
        }
        if request.tools:
            body["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": names[spec.name],
                        "description": spec.description,
                        "parameters": gemini_schema(spec.input_schema),
                    },
                }
                for spec in request.tools
            ]
            body["tool_choice"] = "auto"
        return body

    async def step(self, request: BrainRequest, state: HorizonState) -> BrainDecision:
        del state
        if not self.configured:
            raise BrainUnavailable(self.reason, context={"tier": "gemini"})
        names = safe_tool_names(request.tools)
        back = {safe: original for original, safe in names.items()}
        body = self.build_body(request, names)
        client = self._http()
        started = self._clock.monotonic()

        async def _attempt(slot: KeySlot) -> dict[str, Any]:
            return await post_chat(
                client, slot, body, timeout_s=self._settings.llm_request_timeout_s
            )

        outcome = await walk_ring(
            self._ring, _attempt, what="gemini brain", skip_when_all_parked=False
        )
        if outcome.value is None:
            fault = outcome.fault.value if outcome.fault else "unknown"
            self._last_error = f"{fault}: {outcome.error}"
            raise BrainUnavailable(
                f"Gemini brain pool failed ({fault})",
                context={"tier": "gemini", "fault": fault, "keys_tried": outcome.keys_tried},
            )
        self._last_error = None
        latency_ms = int((self._clock.monotonic() - started) * 1000)
        return decode_completion(
            outcome.value, request, back, model=self.model, latency_ms=latency_ms,
            key_fallback=outcome.key not in (None, "key1"),
        )


def decode_completion(
    payload: dict[str, Any],
    request: BrainRequest,
    back: dict[str, str],
    *,
    model: str,
    latency_ms: int = 0,
    key_fallback: bool = False,
) -> BrainDecision:
    """OpenAI-shaped completion -> provider-neutral decision."""
    choices = payload.get("choices") or []
    choice = choices[0] if choices and isinstance(choices[0], dict) else {}
    message = choice.get("message") or {}
    finish = str(choice.get("finish_reason") or "")
    usage = payload.get("usage") or {}
    details = usage.get("prompt_tokens_details") or {}
    common: dict[str, Any] = {
        "source": Source.GEMINI,
        "model": model,
        "input_tokens": int(usage.get("prompt_tokens") or 0),
        "output_tokens": int(usage.get("completion_tokens") or 0),
        "cache_read_tokens": int(details.get("cached_tokens") or 0),
        "latency_ms": latency_ms,
        "fallback_reason": "gemini brain primary key parked; used a later key"
        if key_fallback
        else None,
    }
    if finish == "content_filter":
        # Gemini's refusal. Normalised to the same stop reason Bedrock uses so
        # the router treats both identically.
        return BrainDecision(tool_calls=(), stop_reason="refusal", text="refused", **common)

    calls: list[ToolCall] = []
    for index, raw in enumerate(message.get("tool_calls") or []):
        fn = (raw or {}).get("function") or {}
        safe = str(fn.get("name") or "")
        arguments = fn.get("arguments")
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments) if arguments.strip() else {}
            except json.JSONDecodeError:
                # A truncated (finish_reason=length) or malformed call is
                # dropped; its siblings are still valid.
                log.warning("gemini tool arguments were not JSON; dropped", tool=safe)
                continue
        if not isinstance(arguments, dict):
            log.warning("gemini tool arguments were not an object; dropped", tool=safe)
            continue
        calls.append(
            ToolCall(
                id=str(raw.get("id") or f"gemini-{index}"),
                name=back.get(safe, safe),
                arguments=arguments,
            )
        )
        if len(calls) >= request.max_tool_calls:
            break
    content = message.get("content")
    return BrainDecision(
        tool_calls=tuple(calls),
        text=str(content)[:2000] if isinstance(content, str) else "",
        stop_reason=finish,
        **common,
    )


__all__ = ["GeminiBrain", "decode_completion", "gemini_schema", "safe_tool_names"]
