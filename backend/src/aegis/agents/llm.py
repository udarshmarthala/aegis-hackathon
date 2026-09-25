"""Model routing with key-level failover.

Two invariants (AIArchitecture 35):

* Domain logic never binds to a provider. Callers ask for a *task class*.
* Fallback preserves the schema and every safety gate. Degrading the model may
  reduce answer quality; it can never reduce safety.

There is one provider - Google AI Studio - and up to four keys. Failover moves
between keys, not vendors; see ``core.keyring`` for why. The consequence worth
stating here is that the *model* is identical on every attempt, so a fallback
cannot change the shape or quality of the answer, only which quota paid for it.

Structured output is validated against a pydantic model. A parse failure is not
retried on another key, because another key would produce the same malformed
answer - it is surfaced, so the orchestrator can abstain rather than proceed on
a result it could not read.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, TypeVar

from pydantic import BaseModel

from aegis.core.clock import SYSTEM_CLOCK, Clock
from aegis.core.config import Settings
from aegis.core.errors import ExternalServiceError
from aegis.core.keyring import KeyFault, KeyRing, KeySlot, classify
from aegis.core.logging import get_logger
from aegis.core.resilience import guarded_call

log = get_logger(__name__)

T = TypeVar("T", bound=BaseModel)

PROVIDER = "google"


class TaskClass(StrEnum):
    FAST = "fast"            # summarisation, classification
    REASONING = "reasoning"  # hypothesis generation, diagnosis
    CODE = "code"            # debugging, patch generation


class LLMUnavailable(ExternalServiceError):
    code = "LLM_UNAVAILABLE"
    retryable = False


class ModelRouter:
    """Selects a model per task class and fails over across configured keys."""

    __slots__ = ("_settings", "_ring", "_clients")

    def __init__(self, settings: Settings, clock: Clock = SYSTEM_CLOCK) -> None:
        self._settings = settings
        self._ring = KeyRing(settings, purpose="llm", clock=clock)
        # Cached per (key, model): building a client is cheap, but each one owns
        # a connection pool, and rebuilding it per call would defeat keep-alive.
        self._clients: dict[tuple[str, str], Any] = {}

    # ------------------------------------------------------------------ #
    # configuration                                                       #
    # ------------------------------------------------------------------ #

    def model_for(self, task: TaskClass) -> str:
        """Resolve the model name for a task class.

        A ``vendor/model`` prefix is stripped. The native Gemini endpoint
        rejects an aggregator-style id with a 404, and a 404 during failover
        looks identical to an outage while actually being a config error.
        """
        s = self._settings
        configured = {
            TaskClass.FAST: s.llm_model_fast,
            TaskClass.REASONING: s.llm_model_reasoning,
            TaskClass.CODE: s.llm_model_code,
        }[task]
        return configured.split("/", 1)[-1].strip()

    @property
    def configured(self) -> bool:
        """False when no Gemini key is present - a legitimate local state."""
        return self._ring.configured

    @property
    def provider(self) -> str:
        return PROVIDER

    def key_status(self) -> list[dict[str, Any]]:
        """Per-key readiness for ``/health``. Contains no secret material."""
        return self._ring.status()

    def _client(self, slot: KeySlot, model: str) -> Any:
        """Build a chat client bound to one key.

        Gemini is reached through its OpenAI-compatible endpoint so that the
        rest of the stack - structured output, tracing, token accounting - keeps
        working against a single wire format.
        """
        cache_key = (slot.label, model)
        cached = self._clients.get(cache_key)
        if cached is not None:
            return cached

        from langchain_openai import ChatOpenAI

        client = ChatOpenAI(
            model=model,
            api_key=slot.secret,
            base_url=self._settings.google_base_url,
            timeout=self._settings.llm_request_timeout_s,
            max_retries=0,  # retry policy is ours, not the SDK's
            # An explicit cap matters for more than cost: providers reject a
            # request whose reserved max_tokens exceeds the account balance,
            # and structured outputs here are small by construction.
            max_tokens=self._settings.llm_max_output_tokens,
        )
        self._clients[cache_key] = client
        return client

    # ------------------------------------------------------------------ #
    # invocation                                                          #
    # ------------------------------------------------------------------ #

    async def structured(
        self,
        *,
        schema: type[T],
        system: str,
        user: str,
        task: TaskClass = TaskClass.REASONING,
    ) -> tuple[T, dict[str, Any]]:
        """Return a validated instance of ``schema`` plus call metadata.

        Raises ``LLMUnavailable`` when every usable key fails, which the
        orchestrator turns into an abstention rather than a crash.
        """
        slots = self._ring.slots()
        if not slots:
            raise LLMUnavailable("no GOOGLE_API_KEY is configured")

        model = self.model_for(task)
        messages = [("system", system), ("human", user)]
        last_error: Exception | None = None
        last_fault: KeyFault | None = None
        tried: list[str] = []

        for slot in slots:
            tried.append(slot.label)
            try:
                structured_client = self._client(slot, model).with_structured_output(
                    schema
                )

                async def _call(_client: Any = structured_client) -> Any:
                    return await _client.ainvoke(messages)

                result: T = await guarded_call(
                    _call,
                    dependency=slot.dependency,
                    timeout_s=self._settings.llm_request_timeout_s,
                    attempts=self._settings.llm_max_retries + 1,
                )
            except Exception as exc:  # noqa: BLE001 - classified, then re-raised
                last_error = exc
                last_fault = classify(exc)
                log.warning(
                    "llm call failed",
                    key=slot.label,
                    model=model,
                    fault=last_fault.value,
                    error=f"{type(exc).__name__}: {exc}"[:300],
                )
                if last_fault is KeyFault.REQUEST:
                    # Every key would reject this identically. Advancing would
                    # burn the whole ring and report "all keys failed" for what
                    # is actually a bad model id or an unparseable answer.
                    break
                self._ring.park(slot, last_fault)
                continue

            self._ring.release(slot)
            # Not "the first key we attempted this call" but "not the primary
            # key": when key1 is already parked the ring starts at key2, and an
            # operator reading fallback_used=False there would conclude the
            # primary quota was healthy when it is not.
            fallback_used = slot.index > 0
            meta: dict[str, Any] = {
                "provider": PROVIDER,
                "model": model,
                "key": slot.label,
                "fallback_used": fallback_used,
                "keys_tried": len(tried),
            }
            if fallback_used:
                # Surfaced in the UI: the answer came from a different quota
                # than the benchmarked baseline (UX spec 82).
                log.warning("llm key fallback used", key=slot.label, model=model)
            return result, meta

        # The message must match what actually happened. A request-level fault
        # stops at the first key, and reporting that as "every key failed" sends
        # an operator hunting for a provider outage when the real cause is a
        # model id the endpoint no longer serves - which is exactly how this
        # path was first exercised.
        summary = (
            "Gemini rejected the request itself; other keys would reject it too"
            if last_fault is KeyFault.REQUEST
            else "every configured Gemini key failed"
        )
        raise LLMUnavailable(
            summary,
            context={
                "keys_tried": tried,
                "model": model,
                "fault": last_fault.value if last_fault else "unknown",
                "last_error": str(last_error)[:300],
            },
        )


def unavailable_reason(exc: LLMUnavailable) -> str:
    """A one-line, accurate reason for an abstention.

    "No LLM provider available" was wrong in three of the four ways this can
    fail, and the one it was wrong about most often - every key transiently
    5xx-ing - reads to an operator as "nobody configured a model", sending them
    to the wrong file. Configuration absent and configuration unreachable are
    distinct states and stay distinct all the way into the timeline (PRD 13).
    """
    fault = str(exc.context.get("fault", "")) if exc.context else ""
    return {
        "quota": "every Gemini key is rate limited",
        "auth": "every Gemini key was rejected",
        "transient": "Gemini is unreachable; every key returned a transient error",
        "request": "Gemini rejected the request itself",
    }.get(fault, exc.message)


__all__ = [
    "PROVIDER",
    "LLMUnavailable",
    "ModelRouter",
    "TaskClass",
    "unavailable_reason",
]
