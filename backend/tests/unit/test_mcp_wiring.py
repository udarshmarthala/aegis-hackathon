"""The tool boundary is wired into the process, and wired in fail-closed.

Registry and invoker are exercised elsewhere. What is asserted here is the
wiring around them, which is where a safe component gets attached unsafely:

* the composition root builds one frozen catalogue and one invoker, and reports
  the catalogue as a capability with its size;
* a catalogue that cannot be built leaves the process with *no* invoker rather
  than a partial one, so no tool call is authorised at all;
* the scopes an API caller gets are derived from Aegis roles only, a VIEWER gets
  reads and nothing else, and no role short of ADMIN can reach execute;
* the write tools stay unreachable for every read-only caller;
* the write audit event has exactly one spelling in the codebase.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from aegis.api.deps import scopes_for
from aegis.api.security import Principal, Role
from aegis.container import Container
from aegis.core.config import Settings
from aegis.core.errors import ValidationError
from aegis.mcp import INVESTIGATION_SCOPES, SCOPES, CallerIdentity, ToolContext
from aegis.mcp.server import server_status
from aegis.mcp.types import TOOL_WRITE_EVENT
from aegis.persistence.audit import AuditEvent

NOW = datetime(2026, 9, 21, 12, 0, tzinfo=UTC)

WRITE_SCOPES = frozenset(
    {"remediation:propose", "remediation:approval", "remediation:execute", "sandbox:run"}
)


def _container() -> Container:
    container = Container(settings=Settings(postgres_password="x"))
    container.build_core()
    container.build_optional()
    container.build_execution()
    return container


def _principal(*roles: Role) -> Principal:
    return Principal(
        uid="uid_1", email="operator@example.com", roles=frozenset(r.value for r in roles)
    )


class _Budget:
    """A ledger that can be read and spent, and has no way to be widened."""

    def __init__(self, calls: int = 10) -> None:
        self._calls = calls

    def view(self) -> _Budget:
        return self

    @property
    def tool_calls_remaining(self) -> int:
        return self._calls

    @property
    def seconds_remaining(self) -> float:
        return 60.0

    @property
    def exhausted(self) -> bool:
        return self._calls <= 0

    def charge_tool(self) -> None:
        self._calls -= 1


def _context(scopes: frozenset[str]) -> ToolContext:
    return ToolContext(
        environment="local",
        caller=CallerIdentity(subject="uid_1", actor_type="human", scopes=scopes),
        budget=_Budget(),
        deadline=NOW + timedelta(seconds=30),
    )


# --------------------------------------------------------------------------- #
# composition root                                                             #
# --------------------------------------------------------------------------- #


def test_the_container_builds_one_frozen_catalogue_and_one_invoker() -> None:
    container = _container()

    assert container.tool_registry is not None
    assert container.tools is not None
    assert container.tool_registry.frozen
    assert container.tools.registry is container.tool_registry
    assert len(container.tool_registry) > 0


def test_the_catalogue_is_reported_as_a_capability_with_its_size() -> None:
    container = _container()
    assert container.tool_registry is not None

    capability = container.capabilities["tools"]
    assert capability.configured is True
    assert str(len(container.tool_registry)) in capability.reason


def test_a_catalogue_that_cannot_be_built_leaves_no_invoker() -> None:
    """Fail closed: no boundary means no authorised call, not a direct one."""
    container = Container(settings=Settings(postgres_password="x"))
    container.build_core()
    container.build_optional()

    import aegis.container as container_module

    original = container_module.default_registry
    try:
        def _explode(deps: object) -> None:
            raise ValidationError("a tool declared an unknown scope")

        container_module.default_registry = _explode  # type: ignore[assignment]
        container.build_execution()
    finally:
        container_module.default_registry = original  # type: ignore[assignment]

    assert container.tools is None
    assert container.tool_registry is None
    assert container.capabilities["tools"].configured is False
    assert container.capabilities["tools"].reason


def test_write_tools_are_registered_and_demand_a_validated_action() -> None:
    """The safety spine: reaching the environment and needing the gate chain's
    token are the same condition, asserted on the catalogue this process serves."""
    container = _container()
    assert container.tool_registry is not None
    registry = container.tool_registry

    assert registry.write_tools()
    for name in registry.write_tools():
        assert registry.spec(name).requires_validated_action
    assert not (registry.read_tools() & registry.write_tools())


# --------------------------------------------------------------------------- #
# scopes from roles                                                            #
# --------------------------------------------------------------------------- #


def test_a_viewer_gets_reads_and_nothing_else() -> None:
    granted = scopes_for(_principal(Role.VIEWER))

    assert granted == INVESTIGATION_SCOPES
    assert not granted & WRITE_SCOPES


def test_the_role_ladder_is_additive_and_execute_is_admin_only() -> None:
    viewer = scopes_for(_principal(Role.VIEWER))
    responder = scopes_for(_principal(Role.RESPONDER))
    approver = scopes_for(_principal(Role.APPROVER))
    admin = scopes_for(_principal(Role.ADMIN))

    assert viewer < responder < approver < admin
    assert "remediation:execute" not in approver
    assert "remediation:execute" in admin
    assert "remediation:propose" in responder
    assert "remediation:approval" not in responder


def test_an_unknown_role_grants_nothing() -> None:
    stranger = Principal(uid="uid_2", email="x@example.com", roles=frozenset({"superuser"}))
    assert scopes_for(stranger) == frozenset()


def test_every_granted_scope_is_in_the_closed_vocabulary() -> None:
    for role in Role:
        assert scopes_for(_principal(role)) <= SCOPES


def test_a_read_only_caller_is_offered_no_write_tool() -> None:
    container = _container()
    assert container.tool_registry is not None
    registry = container.tool_registry

    offered = {
        spec.name for spec in registry.permitted_for(_context(scopes_for(_principal(Role.VIEWER))))
    }

    assert offered
    assert not offered & registry.write_tools()


def test_an_unknown_environment_is_offered_nothing_at_all() -> None:
    """An unrecognised environment is never treated as a new staging cluster."""
    container = _container()
    assert container.tool_registry is not None

    context = ToolContext(
        environment="prod-eu-2",
        caller=CallerIdentity(subject="uid_1", actor_type="human", scopes=SCOPES),
        budget=_Budget(),
        deadline=NOW + timedelta(seconds=30),
    )
    assert container.tool_registry.permitted_for(context) == ()


# --------------------------------------------------------------------------- #
# audit vocabulary and optional transport                                      #
# --------------------------------------------------------------------------- #


def test_the_write_audit_event_has_one_definition() -> None:
    """Two spellings of an event type is how a compliance query loses rows."""
    assert TOOL_WRITE_EVENT == AuditEvent.TOOL_WRITE_INVOKED
    assert AuditEvent.TOOL_WRITE_INVOKED == "tool.write_invoked"
    assert AuditEvent.TOOL_INVOKED == "tool.invoked"


def test_the_optional_mcp_transport_reports_itself_cleanly() -> None:
    """Present or absent, the answer is typed - and absent carries a reason."""
    status = server_status()

    assert isinstance(status["available"], bool)
    if status["available"]:
        assert status["reason"] == ""
    else:
        assert status["reason"]


@pytest.mark.parametrize("role", list(Role))
def test_scopes_are_stable_across_repeated_derivation(role: Role) -> None:
    """Derivation is a pure function of the principal - nothing accumulates."""
    principal = _principal(role)
    assert scopes_for(principal) == scopes_for(principal)
