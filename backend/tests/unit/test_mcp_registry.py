"""The tool catalogue as a safety boundary.

These are not coverage tests. Each one asserts a property that, if it broke,
would widen what an agent can reach without anybody noticing: a write tool that
does not demand the gate chain, a scope nobody granted, an output model that
hands a model raw log text, or a catalogue that can grow after boot.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from aegis.domain.models import UntrustedText
from aegis.integrations.runtime import RuntimeAdapter
from aegis.mcp import ToolDeps, default_registry
from aegis.mcp.registry import ToolNotFound, ToolRegistry
from aegis.mcp.tools import runtime as runtime_tools
from aegis.mcp.types import (
    ENVIRONMENTS,
    SCOPES,
    CallerIdentity,
    ToolBudget,
    ToolContext,
    ToolContractError,
    ToolInput,
    ToolOutcome,
    ToolOutput,
    ToolSpec,
)

NOW = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)


class FixedClock:
    def __init__(self, now: datetime = NOW) -> None:
        self._now = now

    def now(self) -> datetime:
        return self._now

    def monotonic(self) -> float:
        return self._now.timestamp()


class Args(ToolInput):
    value: int = 1


class Out(ToolOutput):
    value: int = 1


async def handler(context: ToolContext, args: Args) -> ToolOutcome:
    assert context.environment
    return ToolOutcome(value=Out(value=args.value))


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
        "retryable": True,
        "idempotent": True,
        "cost_hint": "cheap",
    }
    base.update(over)
    return ToolSpec(**base)


def context(**over: Any) -> ToolContext:
    base: dict[str, Any] = {
        "environment": "local",
        "caller": CallerIdentity(
            subject="agent:test", actor_type="agent", scopes=frozenset(SCOPES)
        ),
        "budget": ToolBudget(max_tool_calls=50, max_seconds=60.0, clock=FixedClock()),
        "deadline": NOW + timedelta(seconds=30),
        "incident_id": "inc_01TEST",
    }
    base.update(over)
    return ToolContext(**base)


# --------------------------------------------------------------------------- #
# the property the boundary rests on                                           #
# --------------------------------------------------------------------------- #


def test_read_and_write_tool_sets_are_disjoint() -> None:
    """A tool in both sets could be reached through a read permission."""
    registry = default_registry(ToolDeps())
    reads, writes = registry.read_tools(), registry.write_tools()
    assert not reads & writes
    assert reads | writes == set(registry.names())


def test_every_write_tool_requires_a_validated_action() -> None:
    registry = default_registry(ToolDeps())
    writes = registry.write_tools()
    assert writes == {"execute_validated_action"}
    for name in writes:
        assert registry.spec(name).requires_validated_action
        assert registry.spec(name).mutates == "environment"


def test_no_read_tool_claims_to_mutate_the_environment() -> None:
    registry = default_registry(ToolDeps())
    for name in registry.read_tools():
        assert registry.spec(name).mutates != "environment"
        assert not registry.spec(name).requires_validated_action


def test_write_tools_are_never_retryable() -> None:
    """Replaying a write is how one restart becomes three."""
    registry = default_registry(ToolDeps())
    for name in registry.write_tools():
        assert registry.spec(name).attempts == 1


def test_access_and_mutation_class_must_agree() -> None:
    with pytest.raises(ToolContractError):
        spec(access="write", mutates="nothing", requires_validated_action=True)
    with pytest.raises(ToolContractError):
        spec(access="read", mutates="environment")
    with pytest.raises(ToolContractError):
        spec(access="write", mutates="environment", requires_validated_action=False)


def test_a_write_tool_without_the_requirement_cannot_be_frozen() -> None:
    """Even a hand-built registry cannot ship an ungated write."""
    registry = ToolRegistry()
    entry = spec(
        access="write",
        mutates="environment",
        requires_validated_action=True,
        retryable=False,
    )
    registry.register(entry, handler)
    # Reach past the constructor the way a careless refactor would.
    object.__setattr__(entry, "requires_validated_action", False)
    with pytest.raises(ToolContractError):
        registry.freeze()


# --------------------------------------------------------------------------- #
# declaration hygiene                                                          #
# --------------------------------------------------------------------------- #


def test_duplicate_tool_names_are_rejected() -> None:
    registry = ToolRegistry()
    registry.register(spec(), handler)
    with pytest.raises(ToolContractError):
        registry.register(spec(), handler)


def test_unknown_scope_is_refused_at_declaration() -> None:
    with pytest.raises(ToolContractError):
        spec(scope="telemetry:everything")


def test_unknown_environment_is_refused_at_declaration() -> None:
    with pytest.raises(ToolContractError):
        spec(environments=frozenset({"local", "dr-site"}))
    with pytest.raises(ToolContractError):
        spec(environments=frozenset())


def test_timeout_must_be_bounded() -> None:
    with pytest.raises(ToolContractError):
        spec(timeout_s=0)
    with pytest.raises(ToolContractError):
        spec(timeout_s=3_600)


def test_retryable_requires_idempotent() -> None:
    with pytest.raises(ToolContractError):
        spec(retryable=True, idempotent=False)


def test_a_frozen_registry_cannot_grow() -> None:
    registry = default_registry(ToolDeps())
    with pytest.raises(ToolContractError):
        registry.register(spec(), handler)


# --------------------------------------------------------------------------- #
# untrusted text is structural                                                 #
# --------------------------------------------------------------------------- #


def test_bare_string_log_field_is_refused() -> None:
    class Leaky(ToolOutput):
        line: str

    with pytest.raises(ToolContractError):
        spec(output_model=Leaky)


def test_bare_string_untrusted_field_nested_one_level_down_is_refused() -> None:
    """The dangerous field is usually the one a review stops short of."""

    class LeakyInner(ToolOutput):
        message: str

    class Outer(ToolOutput):
        rows: tuple[LeakyInner, ...] = ()

    with pytest.raises(ToolContractError):
        spec(output_model=Outer)


def test_untrusted_text_field_is_accepted() -> None:
    class Safe(ToolOutput):
        line: UntrustedText

    assert spec(output_model=Safe).output_model is Safe


def test_every_registered_output_model_passes_the_untrusted_check() -> None:
    """Registration runs the check; this states the invariant for the reader."""
    registry = default_registry(ToolDeps())
    assert len(registry) == 36


# --------------------------------------------------------------------------- #
# lookup and permission filtering                                              #
# --------------------------------------------------------------------------- #


def test_unknown_tool_raises_instead_of_returning_none() -> None:
    registry = default_registry(ToolDeps())
    with pytest.raises(ToolNotFound):
        registry.get("drop_database")


def test_permitted_for_fails_closed_on_an_unknown_environment() -> None:
    registry = default_registry(ToolDeps())
    assert registry.permitted_for(context(environment="dr-site")) == ()


def test_permitted_for_grants_nothing_for_an_unknown_scope() -> None:
    """A scope outside the closed vocabulary is not a new permission."""
    caller = CallerIdentity(
        subject="agent:test", actor_type="agent", scopes=frozenset({"telemetry:everything"})
    )
    registry = default_registry(ToolDeps())
    assert registry.permitted_for(context(caller=caller)) == ()
    assert not caller.may("telemetry:everything")


def test_permitted_for_filters_to_held_scopes() -> None:
    caller = CallerIdentity(
        subject="agent:test", actor_type="agent", scopes=frozenset({"telemetry:logs"})
    )
    registry = default_registry(ToolDeps())
    names = {s.name for s in registry.permitted_for(context(caller=caller))}
    assert names == {"error_logs", "log_patterns"}


def test_an_investigator_scope_set_never_reaches_the_write_tool() -> None:
    from aegis.mcp import INVESTIGATION_SCOPES

    caller = CallerIdentity(
        subject="agent:evidence_investigator",
        actor_type="agent",
        scopes=INVESTIGATION_SCOPES,
    )
    registry = default_registry(ToolDeps())
    names = {s.name for s in registry.permitted_for(context(caller=caller))}
    assert "execute_validated_action" not in names
    assert "propose_action" not in names
    assert "error_logs" in names


def test_every_declared_scope_is_in_the_closed_vocabulary() -> None:
    registry = default_registry(ToolDeps())
    assert registry.scopes_in_use() <= SCOPES


# --------------------------------------------------------------------------- #
# the runtime binding                                                          #
# --------------------------------------------------------------------------- #


def test_runtime_tools_bind_only_declared_read_methods() -> None:
    assert runtime_tools.BOUND_METHODS <= RuntimeAdapter.READ_METHODS
    assert not runtime_tools.BOUND_METHODS & RuntimeAdapter.WRITE_METHODS
