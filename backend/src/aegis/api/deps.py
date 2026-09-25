"""Dependency wiring.

Long-lived resources (pool, clients, verifier) live on ``app.state`` and are
created once during lifespan. Request handlers receive them through these
dependencies, so nothing reaches for a module-level global and tests can
substitute any component.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Annotated

from fastapi import Depends, Header, Request

from aegis.api.security import FirebaseVerifier, Principal, Role
from aegis.container import Container
from aegis.core.config import Settings, get_settings
from aegis.core.errors import AuthenticationError, AuthorizationError
from aegis.core.errors import ConfigError as AegisConfigError
from aegis.core.logging import get_logger
from aegis.evidence.store import EvidenceStore
from aegis.mcp import INVESTIGATION_SCOPES, CallerIdentity, ToolInvoker, ToolRegistry
from aegis.mcp.invoker import require_scopes
from aegis.persistence.db import Database
from aegis.persistence.incidents import IncidentRepository

log = get_logger(__name__)

# What each Aegis role adds to the scopes below it. The ladder mirrors
# ``security.Role.implied`` deliberately: a VIEWER may read telemetry, topology
# and knowledge and nothing else, and no role short of ADMIN can reach the
# execute scope. A scope missing from this table is never granted, so adding a
# tool with a new scope grants nobody anything until this table says so.
_ROLE_SCOPES: dict[Role, frozenset[str]] = {
    Role.VIEWER: INVESTIGATION_SCOPES,
    Role.RESPONDER: require_scopes("sandbox:run", "remediation:propose"),
    Role.APPROVER: require_scopes("remediation:approval"),
    Role.ADMIN: require_scopes("remediation:execute"),
}


def settings_dep() -> Settings:
    return get_settings()


def db_dep(request: Request) -> Database:
    return request.app.state.db  # type: ignore[no-any-return]


def incidents_dep(request: Request) -> IncidentRepository:
    return request.app.state.incidents  # type: ignore[no-any-return]


def evidence_dep(request: Request) -> EvidenceStore:
    return request.app.state.evidence  # type: ignore[no-any-return]


def verifier_dep(request: Request) -> FirebaseVerifier:
    return request.app.state.verifier  # type: ignore[no-any-return]


def container_dep(request: Request) -> Container:
    """The process-wide object graph.

    Handlers reach optional capabilities through this rather than constructing
    clients per request: a router that built its own Neo4j driver would open a
    connection pool per call and would not share the circuit breaker that stops
    a failing dependency from consuming every worker.
    """
    return request.app.state.container  # type: ignore[no-any-return]


def tool_registry_dep(request: Request) -> ToolRegistry:
    """The frozen tool catalogue, or a typed refusal.

    ``None`` means the catalogue could not be built at boot. Serving an empty
    catalogue instead would read as "this deployment has no tools", which is a
    different and much more comfortable claim than the true one.
    """
    container: Container = request.app.state.container
    if container.tool_registry is None:
        raise AegisConfigError(
            "the tool catalogue is unavailable in this process",
            context={"capability": "tools"},
        )
    return container.tool_registry


def tool_invoker_dep(request: Request) -> ToolInvoker:
    """The single enforcement point. Absent means no tool call is authorised."""
    container: Container = request.app.state.container
    if container.tools is None:
        raise AegisConfigError(
            "the tool boundary is unavailable in this process",
            context={"capability": "tools"},
        )
    return container.tools


SettingsDep = Annotated[Settings, Depends(settings_dep)]
DbDep = Annotated[Database, Depends(db_dep)]
IncidentsDep = Annotated[IncidentRepository, Depends(incidents_dep)]
EvidenceDep = Annotated[EvidenceStore, Depends(evidence_dep)]
ContainerDep = Annotated[Container, Depends(container_dep)]


def _bearer(authorization: str | None) -> str | None:
    if not authorization:
        return None
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        return None
    return token.strip()


async def current_principal(
    request: Request,
    settings: SettingsDep,
    authorization: Annotated[str | None, Header()] = None,
) -> Principal:
    """Authenticate the caller.

    The dev bypass exists so the stack is usable before Firebase is wired up. It
    is refused outright in production by ``Settings._production_hardening``, and
    additionally guarded here so a misconfiguration cannot open a hole.
    """
    token = _bearer(authorization)
    if token is None:
        raise AuthenticationError("missing bearer token")

    if settings.auth_dev_mode and not settings.is_production:
        expected = settings.auth_dev_bypass_token.get_secret_value()
        if expected and token == expected:
            log.debug("dev-mode principal accepted")
            return Principal(
                uid="dev-user",
                email="dev@localhost",
                roles=frozenset({Role.ADMIN.value}),
                display_name="Local Developer",
            )

    verifier: FirebaseVerifier = request.app.state.verifier
    principal = verifier.verify(token)
    request.state.principal = principal
    return principal


PrincipalDep = Annotated[Principal, Depends(current_principal)]


def require_role(role: Role) -> Callable[[Principal], Awaitable[Principal]]:
    """Authorization dependency factory.

    Authority is always re-checked here, server side, at the moment of the
    request. A client claiming a role in its payload is irrelevant.
    """

    async def _check(principal: PrincipalDep) -> Principal:
        if not principal.has(role):
            log.warning(
                "authorization denied",
                uid=principal.uid,
                required=role.value,
                held=sorted(principal.effective_roles),
            )
            raise AuthorizationError(
                f"role {role.value} is required",
                context={"required": role.value, "held": sorted(principal.effective_roles)},
            )
        return principal

    return _check


def scopes_for(principal: Principal) -> frozenset[str]:
    """Tool scopes this principal holds, derived from Aegis roles alone.

    Fail closed in both directions: a principal with no recognised role gets no
    scopes at all, and a role is only ever additive through ``_ROLE_SCOPES``.
    Nothing a request body, a header or a prompt contains reaches this function,
    because a permission that can be asked for is not a permission.
    """
    granted: set[str] = set()
    for raw in principal.effective_roles:
        try:
            role = Role(raw)
        except ValueError:
            continue  # unknown role grants nothing
        granted |= _ROLE_SCOPES.get(role, frozenset())
    # Re-validated against the closed vocabulary so a typo in the table above
    # becomes a boot-time failure rather than a silently unenforceable scope.
    return require_scopes(*sorted(granted))


def caller_identity(principal: Principal) -> CallerIdentity:
    """The tool-layer identity for an authenticated human operator."""
    return CallerIdentity(
        subject=principal.uid,
        actor_type="human",
        scopes=scopes_for(principal),
    )


async def current_caller(principal: PrincipalDep) -> CallerIdentity:
    return caller_identity(principal)


ToolRegistryDep = Annotated[ToolRegistry, Depends(tool_registry_dep)]
ToolInvokerDep = Annotated[ToolInvoker, Depends(tool_invoker_dep)]
CallerDep = Annotated[CallerIdentity, Depends(current_caller)]

RequireViewer = Annotated[Principal, Depends(require_role(Role.VIEWER))]
RequireResponder = Annotated[Principal, Depends(require_role(Role.RESPONDER))]
RequireApprover = Annotated[Principal, Depends(require_role(Role.APPROVER))]
RequireAdmin = Annotated[Principal, Depends(require_role(Role.ADMIN))]
