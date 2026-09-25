"""Claude on Amazon Bedrock: the first brain tier.

One fresh request per step, never a transcript. The system prompt and tool
list are the same bytes on every step of a phase, so both carry a cache
breakpoint and every step after the first reads the prefix from cache.

Model order is ``[bedrock_model_id, bedrock_fallback_model_id]``. A model id
that is listed in the region but not enabled for the account answers 403, and
one the region does not serve answers 404. Neither will change on the next
step, so the denial is remembered for a quarter of an hour: paying a 403 round
trip on every step would add a second of latency to an incident for nothing.
The fallback is still Claude on Bedrock, so it is labelled as a *model*
fallback in ``fallback_reason`` rather than as a tier change.

This tier raises ``BrainUnavailable`` when it cannot answer; the router turns
that into the next tier. It never retries a step itself beyond moving to the
fallback model - the next tier is the retry.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from dataclasses import replace
from typing import Any, Final

import anthropic

from aegis.agents.brain.errors import BrainUnavailable
from aegis.agents.horizon.ports import BrainDecision, BrainRequest, ToolCall, ToolSpec
from aegis.core.clock import SYSTEM_CLOCK, Clock
from aegis.core.config import Settings
from aegis.core.keyring import http_status
from aegis.core.logging import get_logger
from aegis.core.resilience import guarded_call
from aegis.domain.horizon import HorizonState, Source

log = get_logger(__name__)

# How long a 403/404 on a model id is believed. Long enough that a denied
# primary costs one round trip per incident rather than one per step; short
# enough that enabling model access in the console takes effect without a
# restart.
_DENIAL_TTL_S: Final = 900.0

_EPHEMERAL: Final[dict[str, str]] = {"type": "ephemeral"}

# Thinking tokens come out of ``max_tokens``. A diagnose step that thinks at
# high effort with the ordinary cap can spend the whole budget reasoning and
# be cut off before its tool call, so high-effort steps get twice the room.
_HIGH_EFFORT_TOKEN_FACTOR: Final = 2


def short_model(model_id: str) -> str:
    """``us.anthropic.claude-sonnet-5`` -> ``claude-sonnet-5`` for labels."""
    return model_id.rsplit(".", 1)[-1] if "anthropic." in model_id else model_id


def _dedupe(ids: list[str]) -> list[str]:
    out: list[str] = []
    for raw in ids:
        model = raw.strip()
        if model and model not in out:
            out.append(model)
    return out


def translate_tool(spec: ToolSpec, *, strict: bool) -> dict[str, Any]:
    """``ToolSpec`` -> Anthropic client tool.

    ``strict: true`` makes the API guarantee the tool input validates against
    the schema, which is what lets the orchestrator treat a malformed argument
    as a defect rather than as model behaviour. Strict mode accepts only a
    subset of JSON Schema, so the schema is normalised by the SDK's own
    ``transform_schema`` (unsupported constraints move into the description,
    every object gets ``additionalProperties: false``). A schema it cannot
    normalise - no ``type``, a type list - is sent as-is without ``strict``;
    the orchestrator validates arguments either way.
    """
    tool: dict[str, Any] = {"name": spec.name, "description": spec.description}
    if strict:
        try:
            tool["input_schema"] = anthropic.transform_schema(spec.input_schema)
            tool["strict"] = True
            return tool
        except (ValueError, TypeError, AssertionError, KeyError):
            log.info("tool schema not strict-compatible; sent without strict", tool=spec.name)
    tool["input_schema"] = spec.input_schema
    return tool


def _about_tools(exc: BaseException) -> bool:
    """Whether a 400 is plausibly about the tool definitions.

    Read from the provider's error text, not from anything the model wrote.
    Without this, an invalid model id (also a 400 on Bedrock) would be
    mistaken for a strict-mode rejection and cost a second doomed request.
    """
    text = str(exc).lower()
    return any(word in text for word in ("tool", "schema", "strict"))


def _field(obj: Any, name: str, default: Any = None) -> Any:
    """Read an SDK model attribute or a dict key - fakes in tests are dicts."""
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


class BedrockBrain:
    """Claude on Bedrock with model-level fallback and prompt caching."""

    __slots__ = (
        "_settings",
        "_factory",
        "_clock",
        "_client",
        "_models",
        "_denied",
        "_strict_rejected",
        "_last_error",
        "_last_model",
    )

    def __init__(
        self,
        settings: Settings,
        client_factory: Callable[[], Any] | None = None,
        *,
        clock: Clock = SYSTEM_CLOCK,
    ) -> None:
        self._settings = settings
        self._factory = client_factory
        self._clock = clock
        self._client: Any = None
        self._models = _dedupe([settings.bedrock_model_id, settings.bedrock_fallback_model_id])
        # model id -> (monotonic expiry, human reason)
        self._denied: dict[str, tuple[float, str]] = {}
        # Models that answered 400 to a strict tool list. Remembered for the
        # process: whether a model supports strict tools does not flap.
        self._strict_rejected: set[str] = set()
        self._last_error: str | None = None
        self._last_model: str | None = None

    # ------------------------------------------------------------------ #
    # readiness                                                           #
    # ------------------------------------------------------------------ #

    @property
    def configured(self) -> bool:
        return bool(self._settings.aws_region.strip()) and bool(self._models)

    @property
    def reason(self) -> str:
        if not self._settings.aws_region.strip():
            return "AWS_REGION is not set"
        if not self._models:
            return "BEDROCK_MODEL_ID is not set"
        return "configured"

    @property
    def models(self) -> tuple[str, ...]:
        return tuple(self._models)

    def _auth_mode(self) -> str:
        s = self._settings
        if s.aws_access_key_id.get_secret_value() and s.aws_secret_access_key.get_secret_value():
            return "access-keys"
        if s.aws_profile:
            return "profile"
        return "default-chain"

    def status(self) -> dict[str, Any]:
        """Readiness for the war-room badge. Names a profile mode, never a key."""
        now = self._clock.monotonic()
        denied = {
            short_model(m): {"reason": why, "retry_in_s": round(until - now, 1)}
            for m, (until, why) in self._denied.items()
            if until > now
        }
        return {
            "name": "bedrock",
            "configured": self.configured,
            "reason": self.reason,
            "region": self._settings.aws_region,
            "auth": self._auth_mode(),
            "model": self._last_model or (self._models[0] if self._models else ""),
            "models": list(self._models),
            "denied_models": denied,
            "strict_tools_rejected": sorted(self._strict_rejected),
            "last_error": self._last_error,
        }

    # ------------------------------------------------------------------ #
    # client                                                              #
    # ------------------------------------------------------------------ #

    def _get_client(self) -> Any:
        if self._client is not None:
            return self._client
        if self._factory is not None:
            self._client = self._factory()
            return self._client
        s = self._settings
        access = s.aws_access_key_id.get_secret_value()
        secret = s.aws_secret_access_key.get_secret_value()
        kwargs: dict[str, Any] = {
            "aws_region": s.aws_region,
            # The deadline is guarded_call's; the SDK's own must not be the
            # shorter one, and its retries would double-spend the step budget.
            "timeout": s.bedrock_timeout_s + 5.0,
            "max_retries": 0,
        }
        if access and secret:
            kwargs["aws_access_key"] = access
            kwargs["aws_secret_key"] = secret
        elif s.aws_profile:
            kwargs["aws_profile"] = s.aws_profile
        elif os.environ.get("AWS_PROFILE", None) == "":
            # botocore reads an empty AWS_PROFILE as a profile literally named
            # "" and fails with ProfileNotFound instead of falling through to
            # the task role - which is exactly the deployed case.
            os.environ.pop("AWS_PROFILE", None)
        # Otherwise botocore's default chain (env, SSO cache, task role).
        self._client = anthropic.AsyncAnthropicBedrock(**kwargs)
        return self._client

    # ------------------------------------------------------------------ #
    # request                                                             #
    # ------------------------------------------------------------------ #

    def build_params(self, request: BrainRequest, *, strict: bool = True) -> dict[str, Any]:
        """Everything except ``model``. Deterministic, so the prefix caches."""
        tools = [translate_tool(spec, strict=strict) for spec in request.tools]
        if tools:
            # The last tool definition closes the tools block; a breakpoint
            # there caches the whole list, which is stable for the phase.
            tools[-1] = {**tools[-1], "cache_control": dict(_EPHEMERAL)}
        max_tokens = self._settings.bedrock_max_tokens
        params: dict[str, Any] = {
            "max_tokens": (
                max_tokens * _HIGH_EFFORT_TOKEN_FACTOR if request.high_effort else max_tokens
            ),
            "system": [
                {"type": "text", "text": request.system, "cache_control": dict(_EPHEMERAL)}
            ],
            "messages": [{"role": "user", "content": request.user}],
        }
        if tools:
            params["tools"] = tools
            # Thinking forbids a forced tool choice, and a forced choice would
            # stop the model from saying it cannot justify any tool - which
            # is an answer worth logging.
            params["tool_choice"] = {"type": "auto"}
        if request.high_effort:
            # Adaptive thinking decides how much to think per request;
            # ``effort`` sets the ceiling. Only diagnose/plan steps pay for it.
            params["thinking"] = {"type": "adaptive"}
            params["output_config"] = {"effort": "high"}
        return params

    def _candidates(self) -> list[str]:
        now = self._clock.monotonic()
        return [m for m in self._models if self._denied.get(m, (0.0, ""))[0] <= now]

    async def _create(self, client: Any, model: str, params: dict[str, Any]) -> Any:
        async def _call() -> Any:
            return await client.messages.create(model=model, **params)

        # attempts=1: the step deadline cannot afford two full timeouts, and
        # the fallback model and the next tier are the retries.
        return await guarded_call(
            _call,
            dependency=f"brain:bedrock:{short_model(model)}",
            timeout_s=self._settings.bedrock_timeout_s,
            attempts=1,
        )

    async def step(self, request: BrainRequest, state: HorizonState) -> BrainDecision:
        del state  # the request already carries the rendered state
        if not self.configured:
            raise BrainUnavailable(self.reason, context={"tier": "bedrock"})
        try:
            client = self._get_client()
        except Exception as exc:  # noqa: BLE001 - SDK/credential setup; reported, tier skipped
            self._last_error = f"client setup failed: {type(exc).__name__}"
            raise BrainUnavailable(
                "Bedrock client could not be created",
                context={"tier": "bedrock", "error": type(exc).__name__},
            ) from exc

        primary = self._models[0]
        notes: list[str] = []
        if primary in self._denied and primary not in self._candidates():
            notes.append(f"{short_model(primary)} {self._denied[primary][1]}")
        candidates = self._candidates()
        if not candidates:
            raise BrainUnavailable(
                "every configured Bedrock model is denied for this account",
                context={"tier": "bedrock", "models": [short_model(m) for m in self._models]},
            )

        for model in candidates:
            strict = model not in self._strict_rejected
            started = self._clock.monotonic()
            try:
                params = self.build_params(request, strict=strict)
                response = await self._create(client, model, params)
            except Exception as exc:  # noqa: BLE001 - classified below, never swallowed
                status = http_status(exc)
                if status == 400 and strict and request.tools and _about_tools(exc):
                    # Strict mode has limits (schema subset, strict-tool
                    # count) a model may enforce differently. One re-ask
                    # without it is a different request, not a retry.
                    self._strict_rejected.add(model)
                    log.warning(
                        "bedrock rejected strict tools; retrying without strict",
                        model=model,
                        error=str(exc)[:200],
                    )
                    try:
                        response = await self._create(
                            client, model, self.build_params(request, strict=False)
                        )
                    except Exception as retry_exc:  # noqa: BLE001 - classified below
                        exc = retry_exc
                        status = http_status(retry_exc)
                    else:
                        return self._finish(response, request, model, started, primary, notes)
                notes.append(self._record_failure(model, exc, status))
                continue
            return self._finish(response, request, model, started, primary, notes)

        raise BrainUnavailable(
            "every Bedrock model failed: " + "; ".join(notes),
            context={"tier": "bedrock", "models": [short_model(m) for m in candidates]},
        )

    def _record_failure(self, model: str, exc: BaseException, status: int | None) -> str:
        if status == 403:
            why = "not enabled for this account"
            self._denied[model] = (self._clock.monotonic() + _DENIAL_TTL_S, why)
        elif status == 404:
            why = "not served in this region"
            self._denied[model] = (self._clock.monotonic() + _DENIAL_TTL_S, why)
        else:
            why = f"failed ({type(exc).__name__}{f' {status}' if status else ''})"
        # SDK error text is the provider's JSON message; it never carries the
        # SigV4 signature or the credentials used to make it.
        self._last_error = f"{short_model(model)}: {type(exc).__name__}: {str(exc)[:200]}"
        log.warning(
            "bedrock model failed",
            model=model,
            status=status,
            error=f"{type(exc).__name__}: {str(exc)[:200]}",
        )
        return f"{short_model(model)} {why}"

    def _finish(
        self,
        response: Any,
        request: BrainRequest,
        model: str,
        started: float,
        primary: str,
        notes: list[str],
    ) -> BrainDecision:
        latency_ms = int((self._clock.monotonic() - started) * 1000)
        decision = decode_response(response, request, model=model, latency_ms=latency_ms)
        self._last_model = model
        self._last_error = None
        if model != primary:
            reason = "; ".join(notes) if notes else f"{short_model(primary)} unavailable"
            decision = replace(decision, fallback_reason=f"{reason}; used {short_model(model)}")
        return decision


def decode_response(
    response: Any, request: BrainRequest, *, model: str, latency_ms: int = 0
) -> BrainDecision:
    """Anthropic message -> provider-neutral decision.

    ``stop_reason`` is read before any content. A refusal yields no tool
    calls whatever else the content holds; ``max_tokens`` keeps only tool
    calls the model finished, because the block it was writing when cut off
    may carry a partial input.
    """
    stop_reason = str(_field(response, "stop_reason", "") or "")
    usage = _field(response, "usage")
    usage_fields: dict[str, Any] = {
        "input_tokens": int(_field(usage, "input_tokens", 0) or 0),
        "output_tokens": int(_field(usage, "output_tokens", 0) or 0),
        "cache_read_tokens": int(_field(usage, "cache_read_input_tokens", 0) or 0),
        "cache_write_tokens": int(_field(usage, "cache_creation_input_tokens", 0) or 0),
    }
    content = list(_field(response, "content", []) or [])
    texts = [
        str(_field(block, "text", "")) for block in content if _field(block, "type") == "text"
    ]

    if stop_reason == "refusal":
        details = _field(response, "stop_details")
        category = _field(details, "category") if details is not None else None
        return BrainDecision(
            tool_calls=(),
            source=Source.BEDROCK,
            model=model,
            text=f"refused{f' ({category})' if category else ''}",
            stop_reason=stop_reason,
            latency_ms=latency_ms,
            **usage_fields,
        )

    if stop_reason == "max_tokens" and content and _field(content[-1], "type") == "tool_use":
        content = content[:-1]

    calls: list[ToolCall] = []
    for block in content:
        if _field(block, "type") != "tool_use":
            continue
        raw_input = _field(block, "input", {})
        if isinstance(raw_input, str):
            try:
                raw_input = json.loads(raw_input)
            except json.JSONDecodeError:
                log.warning("bedrock tool input was not JSON; dropped", tool=_field(block, "name"))
                continue
        if not isinstance(raw_input, dict):
            log.warning("bedrock tool input was not an object; dropped", tool=_field(block, "name"))
            continue
        calls.append(
            ToolCall(id=str(_field(block, "id", "")), name=str(_field(block, "name", "")),
                     arguments=raw_input)
        )
        if len(calls) >= request.max_tool_calls:
            break

    return BrainDecision(
        tool_calls=tuple(calls),
        source=Source.BEDROCK,
        model=model,
        text="\n".join(t for t in texts if t)[:2000],
        stop_reason=stop_reason,
        latency_ms=latency_ms,
        **usage_fields,
    )


__all__ = ["BedrockBrain", "decode_response", "short_model", "translate_tool"]
