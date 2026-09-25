"""The ports the execution layer requires from the outside world.

Dependencies point inward. ``execution`` declares the narrow surface it needs -
restart an instance, change a replica count, roll a deployment back - and
``integrations`` supplies an adapter that satisfies it. Nothing here imports
from ``integrations``, so the safety-critical code can be tested exhaustively
against fakes and cannot be broken by an integration refactor.

The split between reads and writes is structural, not conventional. A read port
can be handed to an investigating agent freely. A write port is only ever
reachable from ``ActionExecutor``, which in turn only accepts a
``ValidatedAction`` - a type an agent has no way to construct.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol, runtime_checkable

from aegis.domain.enums import ServiceHealth


@dataclass(frozen=True, slots=True)
class InstanceInfo:
    """One running unit - a container, a pod or an ECS task.

    Normalised so Compose, Kubernetes and ECS are indistinguishable to callers.
    An executor written against this works unchanged when the environment
    changes, which is what stops environment-specific branching leaking into
    safety-critical code.
    """

    instance_id: str
    service_id: str
    name: str
    health: ServiceHealth = ServiceHealth.UNKNOWN
    state: str = "unknown"
    image: str | None = None
    version: str | None = None
    started_at: datetime | None = None
    restart_count: int = 0
    labels: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class DeploymentInfo:
    """A released version of a service, as the runtime reports it."""

    deployment_id: str
    service_id: str
    version: str
    image: str | None = None
    replicas_desired: int = 0
    replicas_ready: int = 0
    created_at: datetime | None = None
    commit_sha: str | None = None
    previous_version: str | None = None


@dataclass(frozen=True, slots=True)
class OperationResult:
    """What an environment write actually did.

    ``performed`` records the exact API call or command issued, verbatim, so the
    audit trail says what happened rather than what was intended. ``changed`` is
    false for a no-op - re-running an idempotent action that was already applied
    is a success, but it is not a change, and conflating the two would make a
    retried action look like a second production mutation.
    """

    ok: bool
    performed: str
    changed: bool = True
    detail: dict[str, Any] = field(default_factory=dict)
    error: str | None = None


@runtime_checkable
class RuntimeReadPort(Protocol):
    """Read-only observation of the environment under management.

    Safe to expose to investigation agents and to the read-only MCP tools.
    """

    @property
    def available(self) -> bool:
        """False when the adapter is not configured for this environment.

        An unavailable adapter must raise on use rather than returning empty
        collections: "no instances" and "I cannot see instances" lead to
        opposite operational conclusions.
        """
        ...

    async def list_services(self) -> list[Any]: ...

    async def list_instances(self, service_id: str) -> list[InstanceInfo]: ...

    async def get_instance(self, instance_id: str) -> InstanceInfo: ...

    async def health(self, service_id: str) -> ServiceHealth: ...

    async def current_deployment(self, service_id: str) -> DeploymentInfo | None: ...

    async def deployment_history(
        self, service_id: str, *, limit: int = 10
    ) -> list[DeploymentInfo]: ...


@runtime_checkable
class RuntimeWritePort(Protocol):
    """Mutating operations on the environment.

    Every method takes an ``idempotency_key``. The adapter must make a repeated
    call with the same key a no-op that reports ``changed=False`` rather than
    acting twice - a worker that crashes after acting but before recording will
    retry, and production must not be mutated a second time.

    Implementations must NOT retry internally. Retry is the caller's decision,
    made with knowledge of whether the action is idempotent (CLAUDE.md 4).
    """

    @property
    def available(self) -> bool: ...

    async def restart_instance(
        self, instance_id: str, *, idempotency_key: str, timeout_s: float
    ) -> OperationResult: ...

    async def scale(
        self, service_id: str, *, replicas: int, idempotency_key: str, timeout_s: float
    ) -> OperationResult: ...

    async def drain_instance(
        self, instance_id: str, *, idempotency_key: str, timeout_s: float
    ) -> OperationResult: ...

    async def rollback_deployment(
        self, service_id: str, *, to_version: str, idempotency_key: str, timeout_s: float
    ) -> OperationResult: ...

    async def update_config(
        self,
        service_id: str,
        *,
        changes: dict[str, str],
        idempotency_key: str,
        timeout_s: float,
    ) -> OperationResult: ...


@runtime_checkable
class CachePort(Protocol):
    """The narrow cache surface a tier-1 eviction needs.

    Deliberately key-scoped. There is no flush-all method anywhere in the
    execution layer, so "clear the cache" cannot escalate into "clear every
    cache" through a badly bounded argument.
    """

    @property
    def available(self) -> bool: ...

    async def delete_key(
        self, namespace: str, key: str, *, idempotency_key: str
    ) -> OperationResult: ...


@runtime_checkable
class MetricsPort(Protocol):
    """The metric reads verification needs, independent of Prometheus specifics."""

    async def sample(
        self, metric: str, *, resource_id: str | None, start: float, end: float
    ) -> list[tuple[float, float]]:
        """Return (timestamp, value) pairs, or raise ``SourceUnavailable``.

        An empty list means the metric genuinely has no samples in the window.
        That is a real answer and must stay distinguishable from an exception,
        which means the source could not be consulted at all.
        """
        ...


__all__ = [
    "CachePort",
    "DeploymentInfo",
    "InstanceInfo",
    "MetricsPort",
    "OperationResult",
    "RuntimeReadPort",
    "RuntimeWritePort",
]
