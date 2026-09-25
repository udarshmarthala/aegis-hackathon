"""The enforcement point.

Every test here corresponds to a way an agent could otherwise reach further
than it is allowed to: past the gate chain, past its budget, past its deadline,
or past the audit trail. The write tests mint a real ``ValidatedAction`` through
the real gate rather than fabricating one, so if the gate ever stopped producing
them these tests fail too.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from aegis.core.errors import ExternalServiceError, SourceUnavailable
from aegis.core.resilience import reset_breakers
from aegis.domain.enums import ActionState
from aegis.execution.validated import ValidatedAction
from aegis.mcp import ToolDeps, default_registry
from aegis.mcp.invoker import ToolInvoker
from aegis.mcp.registry import ToolRegistry
from aegis.mcp.types import (
    ENVIRONMENTS,
    SCOPES,
    TOOL_WRITE_EVENT,
    CallerIdentity,
    ToolBudget,
    ToolContext,
    ToolInput,
    ToolOutcome,
    ToolOutput,
    ToolSpec,
)

NOW = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)


class FixedClock:
    def __init__(self, now: datetime = NOW) -> None:
        self._now = now
        self._mono = 0.0

    def now(self) -> datetime:
        return self._now

    def monotonic(self) -> float:
        return self._mono

    def advance(self, seconds: float) -> None:
        self._now += timedelta(seconds=seconds)
        self._mono += seconds


class FakeDB:
    """Captures the tool_calls inserts without needing Postgres."""

    def __init__(self, fail: bool = False) -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        self.fail = fail

    async def execute(self, query: str, *args: Any) -> str:
        if self.fail:
            raise ExternalServiceError("database is down")
        self.calls.append((query, args))
        return "INSERT 0 1"

    @property
    def rows(self) -> list[dict[str, Any]]:
        columns = [
            "id", "agent_run_id", "incident_id", "server", "tool", "access", "scope",
            "environment", "caller", "correlation_id", "arguments", "ok",
            "result_summary", "error", "duration_ms", "degraded", "degraded_reason",
            "evidence_ids",
        ]
        return [dict(zip(columns, args, strict=True)) for _, args in self.calls]


class FakeAudit:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    async def record(self, **kwargs: Any) -> None:
        self.events.append(kwargs)


# --------------------------------------------------------------------------- #
# fixtures                                                                     #
# --------------------------------------------------------------------------- #


@pytest.fixture(autouse=True)
def _clean_breakers() -> None:
    """Breaker state is process-global; a failing tool must not leak across tests."""
    reset_breakers()


class Args(ToolInput):
    value: int = 1


class Out(ToolOutput):
    value: int = 1


def spec(**over: Any) -> ToolSpec:
    base: dict[str, Any] = {
        "name": "sample_tool",
        "description": "a sample tool",
        "server": "test",
        "input_model": Args,
        "output_model": Out,
        "access": "read",
        "mutates": "nothing",
        "scope": "telemetry:metrics",
        "environments": ENVIRONMENTS,
        "timeout_s": 5.0,
        "retryable": False,
        "idempotent": True,
        "cost_hint": "cheap",
    }
    base.update(over)
    return ToolSpec(**base)


def context(clock: FixedClock | None = None, **over: Any) -> ToolContext:
    tick = clock or FixedClock()
    base: dict[str, Any] = {
        "environment": "local",
        "caller": CallerIdentity(
            subject="agent:test", actor_type="agent", scopes=frozenset(SCOPES)
        ),
        "budget": ToolBudget(max_tool_calls=10, max_seconds=60.0, clock=tick),
        "deadline": tick.now() + timedelta(seconds=30),
        "correlation_id": "corr_01",
        "incident_id": "inc_01TEST",
        "agent_run_id": None,
    }
    base.update(over)
    return ToolContext(**base)


def build(
    tool: ToolSpec, handler: Any, *, db: FakeDB | None = None, audit: FakeAudit | None = None
) -> tuple[ToolInvoker, FakeDB, FakeAudit]:
    registry = ToolRegistry()
    registry.register(tool, handler)
    registry.freeze()
    database = db or FakeDB()
    log = audit or FakeAudit()
    invoker = ToolInvoker(
        registry, db=database, audit=log, clock=FixedClock()  # type: ignore[arg-type]
    )
    return invoker, database, log


async def ok_handler(context: ToolContext, args: Args) -> ToolOutcome:
    assert context.environment == "local"
    return ToolOutcome(value=Out(value=args.value))


# --------------------------------------------------------------------------- #
# the gate chain is not optional                                               #
# --------------------------------------------------------------------------- #


async def make_validated(**over: Any) -> ValidatedAction:
    """Mint a real ValidatedAction through the real gate."""
    from tests.unit.test_execution_gate import build_gate, run_gate

    gate, _ = build_gate(**over.pop("gate_parts", {}))
    result = await run_gate(gate, over.pop("proposal", None))
    assert isinstance(result, ValidatedAction), result
    return result


class FakeExecution:
    """Stands in for ExecutionService; records that it was reached at all."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def execute(self, validated: ValidatedAction, ports: Any, **kwargs: Any) -> Any:
        from aegis.execution.service import ExecutionReport

        self.calls.append(validated.action.id)
        assert ports is not None
        assert kwargs
        return ExecutionReport(
            action_id=validated.action.id,
            incident_id=validated.action.incident_id,
            executed=True,
            outcome=None,
            verification=None,
            rolled_back=False,
            rollback_outcome=None,
            final_state=ActionState.SUCCESS,
            escalated=False,
            escalation_reason="",
            started_at=NOW,
            finished_at=NOW,
        )


def remediation_invoker(
    execution: FakeExecution | None = None,
) -> tuple[ToolInvoker, FakeDB, FakeAudit]:
    deps = ToolDeps(
        execution=execution,  # type: ignore[arg-type]
        ports=object() if execution is not None else None,  # type: ignore[arg-type]
        clock=FixedClock(),  # type: ignore[arg-type]
    )
    registry = default_registry(deps)
    db, audit = FakeDB(), FakeAudit()
    invoker = ToolInvoker(
        registry, db=db, audit=audit, clock=FixedClock()  # type: ignore[arg-type]
    )
    return invoker, db, audit


async def test_a_write_tool_cannot_be_invoked_without_a_validated_action() -> None:
    """The single most important property in this package.

    The failure code proves the handler was never entered: with no execution
    service configured, reaching the body would have raised EXECUTION_UNAVAILABLE.
    """
    invoker, db, audit = remediation_invoker()

    result = await invoker.invoke(
        "execute_validated_action",
        {"action_id": "act_01TEST", "settle_seconds": 0.0},
        context(),
    )

    assert result.ok is False
    assert result.error is not None
    assert result.error.code == "VALIDATED_ACTION_REQUIRED"
    # Refused, and recorded as an attempted write in both the call log and audit.
    assert db.rows[0]["access"] == "write"
    assert audit.events[0]["event_type"] == TOOL_WRITE_EVENT
    assert audit.events[0]["detail"]["denied"] == "VALIDATED_ACTION_REQUIRED"


async def test_a_write_tool_with_a_gate_minted_action_reaches_execution() -> None:
    execution = FakeExecution()
    invoker, db, audit = remediation_invoker(execution)
    validated = await make_validated()

    result = await invoker.invoke(
        "execute_validated_action",
        {"action_id": validated.action.id, "settle_seconds": 0.0},
        context(),
        validated=validated,
    )

    assert result.ok is True, result.error
    assert execution.calls == [validated.action.id]
    assert db.rows[0]["access"] == "write"
    assert audit.events[-1]["event_type"] == TOOL_WRITE_EVENT
    assert audit.events[-1]["detail"]["ok"] is True


async def test_a_validated_action_for_another_action_id_is_refused() -> None:
    execution = FakeExecution()
    invoker, _, _ = remediation_invoker(execution)
    validated = await make_validated()

    result = await invoker.invoke(
        "execute_validated_action",
        {"action_id": "act_SOMETHING_ELSE", "settle_seconds": 0.0},
        context(),
        validated=validated,
    )

    assert result.ok is False
    assert execution.calls == []


async def test_an_expired_authorisation_is_refused() -> None:
    """A lease that lapsed between validation and execution is not a permission."""
    execution = FakeExecution()
    registry = default_registry(
        ToolDeps(execution=execution, ports=object())  # type: ignore[arg-type]
    )
    late = FixedClock(NOW + timedelta(hours=6))
    invoker = ToolInvoker(registry, db=FakeDB(), audit=FakeAudit(), clock=late)  # type: ignore[arg-type]
    validated = await make_validated()

    result = await invoker.invoke(
        "execute_validated_action",
        {"action_id": validated.action.id, "settle_seconds": 0.0},
        context(clock=late),
        validated=validated,
    )

    assert result.ok is False
    assert result.error is not None
    assert result.error.code == "AUTHORISATION_EXPIRED"
    assert execution.calls == []


async def test_a_read_tool_refuses_a_validated_action() -> None:
    """An authorisation offered to a read tool is a caller bug, not a shortcut."""
    execution = FakeExecution()
    invoker, _, _ = remediation_invoker(execution)
    validated = await make_validated()

    result = await invoker.invoke(
        "service_health", {"service_id": "local:demo:payment"}, context(), validated=validated
    )

    assert result.ok is False
    assert result.error is not None
    assert result.error.code == "VALIDATED_ACTION_NOT_ACCEPTED"


def test_a_validated_action_cannot_be_fabricated_by_a_tool_caller() -> None:
    with pytest.raises(Exception):  # noqa: B017 - the guard type is module-private
        ValidatedAction(
            token=object(), action=None, proposal=None, decision=None,  # type: ignore[arg-type]
            evidence_report=None, lease=None, approval=None,  # type: ignore[arg-type]
            validated_at=NOW, correlation_id="c",
        )


# --------------------------------------------------------------------------- #
# permission                                                                   #
# --------------------------------------------------------------------------- #


async def test_a_missing_scope_denies_the_call() -> None:
    invoker, db, _ = build(spec(), ok_handler)
    caller = CallerIdentity(
        subject="agent:test", actor_type="agent", scopes=frozenset({"topology:read"})
    )

    result = await invoker.invoke("sample_tool", {"value": 1}, context(caller=caller))

    assert result.ok is False
    assert result.error is not None
    assert result.error.code == "FORBIDDEN"
    assert db.rows[0]["ok"] is False


async def test_an_unknown_scope_grants_nothing() -> None:
    invoker, _, _ = build(spec(), ok_handler)
    caller = CallerIdentity(
        subject="agent:test", actor_type="agent", scopes=frozenset({"telemetry:everything"})
    )

    result = await invoker.invoke("sample_tool", {"value": 1}, context(caller=caller))

    assert result.ok is False
    assert result.error is not None
    assert result.error.code == "FORBIDDEN"


async def test_an_unknown_environment_denies_the_call() -> None:
    invoker, _, _ = build(spec(), ok_handler)

    result = await invoker.invoke("sample_tool", {"value": 1}, context(environment="dr-site"))

    assert result.ok is False
    assert result.error is not None
    assert result.error.code == "ENVIRONMENT_UNKNOWN"


async def test_a_tool_not_declared_for_this_environment_is_denied() -> None:
    invoker, _, _ = build(spec(environments=frozenset({"local"})), ok_handler)

    result = await invoker.invoke(
        "sample_tool", {"value": 1}, context(environment="production")
    )

    assert result.ok is False
    assert result.error is not None
    assert result.error.code == "FORBIDDEN"


async def test_an_unknown_tool_returns_a_typed_error_and_is_recorded() -> None:
    invoker, db, _ = build(spec(), ok_handler)

    result = await invoker.invoke("drop_database", {}, context())

    assert result.ok is False
    assert result.error is not None
    assert result.error.code == "TOOL_NOT_FOUND"
    assert db.rows[0]["tool"] == "drop_database"
    assert db.rows[0]["access"] == "read"


# --------------------------------------------------------------------------- #
# schema                                                                       #
# --------------------------------------------------------------------------- #


async def test_arguments_are_rejected_not_coerced() -> None:
    invoker, db, _ = build(spec(), ok_handler)

    result = await invoker.invoke("sample_tool", {"value": "3"}, context())

    assert result.ok is False
    assert result.error is not None
    assert result.error.code == "TOOL_ARGUMENTS_INVALID"
    assert db.rows[0]["ok"] is False


async def test_unknown_arguments_are_rejected() -> None:
    invoker, _, _ = build(spec(), ok_handler)

    result = await invoker.invoke("sample_tool", {"value": 1, "sudo": True}, context())

    assert result.ok is False
    assert result.error is not None
    assert result.error.code == "TOOL_ARGUMENTS_INVALID"


# --------------------------------------------------------------------------- #
# budget                                                                       #
# --------------------------------------------------------------------------- #


async def test_an_exhausted_budget_refuses_the_call() -> None:
    invoker, db, _ = build(spec(), ok_handler)
    budget = ToolBudget(max_tool_calls=1, max_seconds=60.0, clock=FixedClock())
    ctx = context(budget=budget)

    first = await invoker.invoke("sample_tool", {"value": 1}, ctx)
    second = await invoker.invoke("sample_tool", {"value": 1}, ctx)

    assert first.ok is True
    assert second.ok is False
    assert second.error is not None
    assert second.error.code == "BUDGET_EXHAUSTED"
    # Both the successful call and the refusal are on the record.
    assert len(db.rows) == 2


async def test_every_accepted_call_charges_the_budget() -> None:
    """Including denied ones - otherwise a rejected call loops for free."""
    invoker, _, _ = build(spec(), ok_handler)
    budget = ToolBudget(max_tool_calls=5, max_seconds=60.0, clock=FixedClock())
    ctx = context(budget=budget)

    await invoker.invoke("sample_tool", {"value": "not an int"}, ctx)

    assert budget.view().tool_calls_remaining == 4


async def test_an_expired_deadline_refuses_before_running() -> None:
    invoker, _, _ = build(spec(), ok_handler)

    result = await invoker.invoke(
        "sample_tool", {"value": 1}, context(deadline=NOW - timedelta(seconds=1))
    )

    assert result.ok is False
    assert result.error is not None
    assert result.error.code == "DEADLINE_EXCEEDED"


# --------------------------------------------------------------------------- #
# bounded execution                                                            #
# --------------------------------------------------------------------------- #


async def test_a_timeout_becomes_a_typed_result_not_an_exception() -> None:
    async def slow(context: ToolContext, args: Args) -> ToolOutcome:
        assert context
        await asyncio.sleep(5)
        return ToolOutcome(value=Out(value=args.value))

    invoker, db, _ = build(spec(timeout_s=0.05), slow)

    result = await invoker.invoke("sample_tool", {"value": 1}, context())

    assert result.ok is False
    assert result.error is not None
    assert result.error.code == "TIMEOUT"
    # A tool that could not answer in time has not told us there is nothing there.
    assert result.degraded is True
    assert db.rows[0]["ok"] is False


async def test_a_non_idempotent_tool_is_never_retried() -> None:
    attempts: list[int] = []

    async def flaky(context: ToolContext, args: Args) -> ToolOutcome:
        assert context and args
        attempts.append(1)
        raise ExternalServiceError("transient", retryable=True)

    invoker, _, _ = build(spec(retryable=False, idempotent=False), flaky)

    result = await invoker.invoke("sample_tool", {"value": 1}, context())

    assert result.ok is False
    assert len(attempts) == 1


async def test_an_idempotent_retryable_tool_is_retried() -> None:
    attempts: list[int] = []

    async def flaky(context: ToolContext, args: Args) -> ToolOutcome:
        assert context
        attempts.append(1)
        if len(attempts) < 3:
            raise ExternalServiceError("transient", retryable=True)
        return ToolOutcome(value=Out(value=args.value))

    invoker, _, _ = build(spec(retryable=True, idempotent=True), flaky)

    result = await invoker.invoke("sample_tool", {"value": 1}, context())

    assert result.ok is True
    assert len(attempts) == 3


# --------------------------------------------------------------------------- #
# failure handling                                                             #
# --------------------------------------------------------------------------- #


async def test_an_unexpected_exception_never_reaches_the_caller() -> None:
    async def explode(context: ToolContext, args: Args) -> ToolOutcome:
        assert context and args
        raise RuntimeError("secret internal detail from upstream response body")

    invoker, db, _ = build(spec(), explode)

    result = await invoker.invoke("sample_tool", {"value": 1}, context())

    assert result.ok is False
    assert result.error is not None
    assert result.error.code == "TOOL_FAILED"
    # The message is the exception *type*, never its body: a third-party
    # exception string can carry attacker-influenced response text.
    assert result.error.message == "RuntimeError"
    assert "secret internal detail" not in db.rows[0]["result_summary"]


async def test_a_source_outage_is_degraded_rather_than_empty() -> None:
    async def down(context: ToolContext, args: Args) -> ToolOutcome:
        assert context and args
        raise SourceUnavailable("prometheus unavailable: ConnectError")

    invoker, db, _ = build(spec(), down)

    result = await invoker.invoke("sample_tool", {"value": 1}, context())

    assert result.ok is False
    assert result.degraded is True
    assert result.degraded_reason
    assert result.found_nothing is False
    assert db.rows[0]["degraded"] is True


async def test_a_successful_empty_answer_is_not_degraded() -> None:
    class Empty(ToolOutput):
        rows: tuple[str, ...] = ()

        @property
        def is_empty(self) -> bool:
            return not self.rows

    async def nothing(context: ToolContext, args: Args) -> ToolOutcome:
        assert context and args
        return ToolOutcome(value=Empty())

    invoker, _, _ = build(spec(output_model=Empty), nothing)

    result = await invoker.invoke("sample_tool", {"value": 1}, context())

    assert result.ok is True
    assert result.degraded is False
    assert result.found_nothing is True


# --------------------------------------------------------------------------- #
# persistence and audit                                                        #
# --------------------------------------------------------------------------- #


async def test_every_invocation_writes_a_tool_calls_row() -> None:
    invoker, db, _ = build(spec(), ok_handler)
    ctx = context()

    await invoker.invoke("sample_tool", {"value": 1}, ctx)          # success
    await invoker.invoke("sample_tool", {"value": "x"}, ctx)        # schema failure
    await invoker.invoke("nope", {}, ctx)                           # unknown tool
    await invoker.invoke(
        "sample_tool", {"value": 1},
        context(caller=CallerIdentity("agent:x", "agent", frozenset())),
    )                                                               # denied

    assert len(db.rows) == 4
    assert [row["ok"] for row in db.rows] == [True, False, False, False]
    assert all(row["correlation_id"] in ("corr_01",) for row in db.rows[:3])
    assert all(row["environment"] == "local" for row in db.rows[:3])


async def test_a_failed_tool_calls_write_does_not_fail_the_call() -> None:
    """Losing observability must never abort the work being observed."""
    invoker, _, _ = build(spec(), ok_handler, db=FakeDB(fail=True))

    result = await invoker.invoke("sample_tool", {"value": 1}, context())

    assert result.ok is True


async def test_read_tools_emit_no_write_audit_event() -> None:
    invoker, _, audit = build(spec(), ok_handler)

    await invoker.invoke("sample_tool", {"value": 1}, context())

    assert audit.events == []
