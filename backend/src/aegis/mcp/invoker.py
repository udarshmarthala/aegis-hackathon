"""The enforcement point.

Every tool call in Aegis - from the LangGraph orchestrator, from the API, from
an external MCP client - goes through ``ToolInvoker.invoke``. Nothing else calls
a handler directly, and a handler is useless on its own because it has no way to
obtain the arguments object or, for a write, the ``ValidatedAction`` it requires.

What this class guarantees, in order:

1. **Schema.** Arguments are validated against the tool's pydantic model in
   strict mode. Nothing is coerced; a wrong type is a rejection.
2. **Permission.** The caller must hold the tool's scope, and the tool must be
   declared for the caller's environment. An unknown scope or an unknown
   environment denies.
3. **Budget.** Every accepted call charges ``budget.charge_tool()`` and an
   exhausted budget refuses before any work starts. The invoker can spend a
   budget; it has no method that raises one.
4. **Bounded execution.** ``guarded_call`` applies the tool's timeout, capped
   further by the caller's deadline and remaining wall-clock budget. There is no
   unbounded await anywhere under this boundary.
5. **Retry only where it is safe.** ``attempts > 1`` requires both ``retryable``
   and ``idempotent``. A write tool is structurally barred from being retryable,
   so no write is ever repeated automatically.
6. **The gate chain is not optional.** A ``write``-class tool is refused unless
   the caller supplies an ``execution.ValidatedAction`` - a type only
   ``ActionGate.validate`` can construct, and only after schema, evidence,
   policy, authz and lease gates have all passed. There is therefore no code
   path from a tool call to an environment mutation that skips the gate chain:
   the write handler is never entered without one, the action is re-checked for
   liveness immediately before the handler runs, and ``tests/unit/
   test_mcp_invoker.py`` asserts both properties.
7. **Audit and persistence.** Every invocation - success, denial, failure or
   timeout - writes a ``tool_calls`` row, and every write-class attempt also
   writes an audit event.
8. **No tracebacks.** Every exception becomes a typed ``ToolResult(ok=False)``.
   An agent never sees a stack trace, because a stack trace is both an
   implementation leak and a large blob of instruction-shaped text.
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from typing import Any

from pydantic import ValidationError as PydanticValidationError

from aegis.core.clock import SYSTEM_CLOCK, Clock
from aegis.core.errors import (
    AuthorizationError,
    BudgetExhausted,
    SourceUnavailable,
    TimeoutExceeded,
    ValidationError,
)
from aegis.core.ids import TOOL_CALL, new_id
from aegis.core.logging import get_logger
from aegis.core.resilience import guarded_call
from aegis.execution.validated import ValidatedAction
from aegis.mcp.registry import RegisteredTool, ToolNotFound, ToolRegistry
from aegis.mcp.types import (
    MAX_ARGUMENT_CHARS,
    SCOPES,
    TOOL_WRITE_EVENT,
    ToolContext,
    ToolError,
    ToolOutcome,
    ToolResult,
    ToolSpec,
)
from aegis.persistence.audit import AuditLog
from aegis.persistence.db import Database

log = get_logger(__name__)

# Below this there is no point starting a call: the deadline will fire before
# any real dependency answers, and a 0s timeout would look like a dependency
# failure rather than a caller that ran out of time.
MIN_TIMEOUT_S = 0.05


class ToolInvoker:
    """The single door between an agent and everything outside it."""

    __slots__ = ("_registry", "_db", "_audit", "_clock")

    def __init__(
        self,
        registry: ToolRegistry,
        *,
        db: Database | None = None,
        audit: AuditLog | None = None,
        clock: Clock = SYSTEM_CLOCK,
    ) -> None:
        self._registry = registry
        self._db = db
        self._audit = audit
        self._clock = clock

    @property
    def registry(self) -> ToolRegistry:
        return self._registry

    # ------------------------------------------------------------------ #
    # the one entry point                                                 #
    # ------------------------------------------------------------------ #

    async def invoke(
        self,
        name: str,
        arguments: Mapping[str, Any],
        context: ToolContext,
        *,
        validated: ValidatedAction | None = None,
    ) -> ToolResult:
        """Run one tool call. Never raises - failures come back typed."""
        call_id = new_id(TOOL_CALL)
        started = time.perf_counter()

        try:
            entry = self._registry.get(name)
        except ToolNotFound as exc:
            # Recorded like any other call: an agent repeatedly reaching for a
            # tool that does not exist is a signal worth seeing in the audit.
            return await self._denied(
                call_id=call_id,
                spec=None,
                tool_name=name,
                context=context,
                arguments=dict(arguments),
                error=ToolError(code=exc.code, message=exc.message),
                started=started,
            )

        spec = entry.spec

        # --- budget: check, then charge, before anything else can loop ----
        view = context.budget_view()
        if view.exhausted:
            return await self._denied(
                call_id=call_id, spec=spec, tool_name=name, context=context,
                arguments=dict(arguments),
                error=ToolError(
                    code=BudgetExhausted.code,
                    message="agent budget exhausted; no further tool calls",
                ),
                started=started,
            )
        # Charged here rather than after validation so that a loop of malformed
        # or forbidden calls is bounded too. A refused call still consumed a
        # turn, and only a charged turn is a bounded turn.
        context.budget.charge_tool()

        denial = self._permission_error(spec, context, validated)
        if denial is not None:
            if spec.access == "write":
                await self._audit_write(
                    spec=spec, context=context, call_id=call_id,
                    validated=validated, ok=False, detail={"denied": denial.code},
                )
            return await self._denied(
                call_id=call_id, spec=spec, tool_name=name, context=context,
                arguments=dict(arguments), error=denial, started=started,
            )

        # --- schema: reject, never coerce --------------------------------
        try:
            args = spec.input_model.model_validate(dict(arguments))
        except PydanticValidationError as exc:
            return await self._denied(
                call_id=call_id, spec=spec, tool_name=name, context=context,
                arguments=dict(arguments),
                error=ToolError(
                    code="TOOL_ARGUMENTS_INVALID",
                    message=f"{spec.name} arguments rejected by schema",
                    detail={"errors": exc.error_count()},
                ),
                started=started,
            )

        timeout_s = self._effective_timeout(spec, context)
        if timeout_s is None:
            return await self._denied(
                call_id=call_id, spec=spec, tool_name=name, context=context,
                arguments=args.model_dump(mode="json"),
                error=ToolError(
                    code="DEADLINE_EXCEEDED",
                    message=f"no time left to run {spec.name}",
                ),
                started=started,
            )

        return await self._execute(
            entry=entry,
            call_id=call_id,
            context=context,
            args=args,
            validated=validated,
            timeout_s=timeout_s,
            started=started,
        )

    # ------------------------------------------------------------------ #
    # permission                                                          #
    # ------------------------------------------------------------------ #

    def _permission_error(
        self, spec: ToolSpec, context: ToolContext, validated: ValidatedAction | None
    ) -> ToolError | None:
        """Every reason to refuse, evaluated fail-closed. ``None`` means allow."""
        if not context.environment_known:
            return ToolError(
                code="ENVIRONMENT_UNKNOWN",
                message=f"environment {context.environment!r} is not a known environment",
            )
        if context.environment not in spec.environments:
            return ToolError(
                code=AuthorizationError.code,
                message=f"{spec.name} is not available in {context.environment}",
            )
        if not context.caller.may(spec.scope):
            return ToolError(
                code=AuthorizationError.code,
                message=f"caller lacks scope {spec.scope}",
            )

        if spec.requires_validated_action:
            if validated is None:
                # The core structural guarantee. Without the gate chain's token
                # there is no ValidatedAction, and without a ValidatedAction the
                # handler is never entered.
                return ToolError(
                    code="VALIDATED_ACTION_REQUIRED",
                    message=(
                        f"{spec.name} mutates the environment and requires a "
                        "ValidatedAction produced by the gate chain"
                    ),
                )
            return self._validated_action_error(spec, context, validated)

        if validated is not None:
            # A read tool handed an authorisation is a caller bug, and a caller
            # bug that quietly works is how authorisations start travelling.
            return ToolError(
                code="VALIDATED_ACTION_NOT_ACCEPTED",
                message=f"{spec.name} is a read tool and takes no ValidatedAction",
            )
        return None

    def _validated_action_error(
        self, spec: ToolSpec, context: ToolContext, validated: ValidatedAction
    ) -> ToolError | None:
        now = self._clock.now()
        if not validated.still_valid(now):
            # Re-checked here and not only at validation time: the lease or the
            # approval may have lapsed in between, and a lapsed authorisation is
            # not an authorisation.
            return ToolError(
                code="AUTHORISATION_EXPIRED",
                message="the lease or approval backing this action has lapsed",
            )
        if validated.target.environment != context.environment:
            return ToolError(
                code=AuthorizationError.code,
                message="the validated action targets a different environment",
            )
        if context.incident_id and validated.action.incident_id != context.incident_id:
            return ToolError(
                code=AuthorizationError.code,
                message="the validated action belongs to a different incident",
            )
        if context.environment not in spec.environments:
            return ToolError(
                code=AuthorizationError.code,
                message=f"{spec.name} is not available in {context.environment}",
            )
        return None

    # ------------------------------------------------------------------ #
    # execution                                                           #
    # ------------------------------------------------------------------ #

    def _effective_timeout(self, spec: ToolSpec, context: ToolContext) -> float | None:
        """The smallest of the tool's timeout, the deadline and the budget."""
        view = context.budget_view()
        remaining = min(
            spec.timeout_s,
            context.remaining_seconds(self._clock.now()),
            view.seconds_remaining,
        )
        return remaining if remaining >= MIN_TIMEOUT_S else None

    async def _execute(
        self,
        *,
        entry: RegisteredTool,
        call_id: str,
        context: ToolContext,
        args: Any,
        validated: ValidatedAction | None,
        timeout_s: float,
        started: float,
    ) -> ToolResult:
        spec = entry.spec
        arguments = args.model_dump(mode="json")

        async def _call() -> ToolOutcome:
            if spec.access == "write":
                assert validated is not None  # guaranteed by _permission_error
                return await entry.handler(context, args, validated)  # type: ignore[call-arg]
            return await entry.handler(context, args)  # type: ignore[call-arg]

        try:
            outcome = await guarded_call(
                _call,
                dependency=f"tool:{spec.name}",
                timeout_s=timeout_s,
                attempts=spec.attempts,
            )
        except SourceUnavailable as exc:
            # A handler is expected to convert this into a degraded outcome with
            # an evidence gap itself; catching it here as well means a tool that
            # forgets still cannot report an outage as "nothing found".
            result = self._failure(
                spec, call_id, started,
                ToolError.from_exception(exc, fallback_code="SOURCE_UNAVAILABLE"),
                degraded=True, degraded_reason=exc.message,
            )
        except TimeoutExceeded as exc:
            result = self._failure(
                spec, call_id, started,
                ToolError(code=exc.code, message=f"{spec.name} exceeded {timeout_s:.1f}s"),
                degraded=True, degraded_reason="tool timed out before answering",
            )
        except Exception as exc:  # noqa: BLE001 - the boundary converts, never propagates
            log.warning(
                "tool call failed",
                tool=spec.name, call_id=call_id, incident_id=context.incident_id,
                correlation_id=context.correlation_id, error=type(exc).__name__,
            )
            result = self._failure(
                spec, call_id, started,
                ToolError.from_exception(exc, fallback_code="TOOL_FAILED"),
            )
        else:
            result = ToolResult(
                ok=True,
                tool=spec.name,
                call_id=call_id,
                duration_ms=self._elapsed_ms(started),
                value=outcome.value,
                evidence_ids=outcome.evidence_ids,
                provenance=outcome.provenance,
                degraded=outcome.degraded,
                degraded_reason=outcome.degraded_reason,
            )

        await self._record(spec, call_id, context, arguments, result)
        if spec.access == "write":
            await self._audit_write(
                spec=spec, context=context, call_id=call_id, validated=validated,
                ok=result.ok, detail={"degraded": result.degraded},
            )
        return result

    def _failure(
        self,
        spec: ToolSpec,
        call_id: str,
        started: float,
        error: ToolError,
        *,
        degraded: bool = False,
        degraded_reason: str = "",
    ) -> ToolResult:
        return ToolResult(
            ok=False,
            tool=spec.name,
            call_id=call_id,
            duration_ms=self._elapsed_ms(started),
            error=error,
            degraded=degraded,
            degraded_reason=degraded_reason,
        )

    async def _denied(
        self,
        *,
        call_id: str,
        spec: ToolSpec | None,
        tool_name: str,
        context: ToolContext,
        arguments: dict[str, Any],
        error: ToolError,
        started: float,
    ) -> ToolResult:
        """Refusals are first-class results, and they are recorded like calls."""
        result = ToolResult(
            ok=False,
            tool=tool_name,
            call_id=call_id,
            duration_ms=self._elapsed_ms(started),
            error=error,
        )
        log.info(
            "tool call denied",
            tool=tool_name, call_id=call_id, reason=error.code,
            caller=context.caller.subject, environment=context.environment,
            incident_id=context.incident_id, correlation_id=context.correlation_id,
        )
        await self._record(spec, call_id, context, arguments, result)
        return result

    @staticmethod
    def _elapsed_ms(started: float) -> int:
        return max(0, int((time.perf_counter() - started) * 1000))

    # ------------------------------------------------------------------ #
    # persistence                                                         #
    # ------------------------------------------------------------------ #

    async def _record(
        self,
        spec: ToolSpec | None,
        call_id: str,
        context: ToolContext,
        arguments: dict[str, Any],
        result: ToolResult,
    ) -> None:
        """Append the ``tool_calls`` row for this invocation.

        Never raises. Like the audit log, losing this row must not abort an
        in-flight investigation, but it is logged at ERROR so the gap is visible
        rather than assumed away.
        """
        if self._db is None:
            return
        try:
            await self._db.execute(
                """
                INSERT INTO tool_calls
                    (id, agent_run_id, incident_id, server, tool, access, scope,
                     environment, caller, correlation_id, arguments, ok,
                     result_summary, error, duration_ms, degraded,
                     degraded_reason, evidence_ids)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18)
                """,
                call_id,
                context.agent_run_id,
                context.incident_id,
                spec.server if spec is not None else "unknown",
                result.tool,
                # An unresolvable tool is recorded as a read: it never reached a
                # handler, so claiming it was a write would misreport the audit.
                spec.access if spec is not None else "read",
                spec.scope if spec is not None else "",
                context.environment,
                context.caller.subject,
                context.correlation_id,
                _bounded_arguments(arguments),
                result.ok,
                result.summary(),
                result.error.message if result.error is not None else None,
                result.duration_ms,
                result.degraded,
                result.degraded_reason,
                list(result.evidence_ids),
            )
        except Exception as exc:  # noqa: BLE001 - observability must not break work
            log.error(
                "tool_calls write failed",
                tool=result.tool, call_id=call_id,
                incident_id=context.incident_id, error=str(exc),
            )

    async def _audit_write(
        self,
        *,
        spec: ToolSpec,
        context: ToolContext,
        call_id: str,
        validated: ValidatedAction | None,
        ok: bool,
        detail: dict[str, Any],
    ) -> None:
        """Audit every write-class attempt, including the refused ones."""
        if self._audit is None:
            return
        target = validated.target if validated is not None else None
        await self._audit.record(
            event_type=TOOL_WRITE_EVENT,
            actor=context.caller.subject,
            actor_type=context.caller.actor_type,
            incident_id=context.incident_id,
            resource_type=target.resource_type if target else None,
            resource_id=target.resource_id if target else None,
            detail={
                "tool": spec.name,
                "call_id": call_id,
                "ok": ok,
                "action_id": validated.action.id if validated is not None else None,
                "environment": context.environment,
                **detail,
            },
            correlation_id=context.correlation_id or None,
        )


def _bounded_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    """Keep one pathological argument set from bloating the incident database."""
    rendered = repr(arguments)
    if len(rendered) <= MAX_ARGUMENT_CHARS:
        return arguments
    return {"_truncated": True, "_chars": len(rendered)}


def require_scopes(*scopes: str) -> frozenset[str]:
    """Build a scope set, rejecting anything outside the closed vocabulary."""
    unknown = set(scopes) - SCOPES
    if unknown:
        raise ValidationError(
            "unknown tool scope requested", context={"unknown": sorted(unknown)}
        )
    return frozenset(scopes)


__all__ = ["MIN_TIMEOUT_S", "ToolInvoker", "require_scopes"]
