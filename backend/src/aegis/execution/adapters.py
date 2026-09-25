"""Bridges between the execution ports and the concrete integrations.

``execution.ports`` states what the safety-critical code needs; the adapters in
``integrations.runtime`` state what each environment can actually do. The two
were written against the same contract but not the same signatures, and that is
fine - translating here, in one small and heavily-typed place, is better than
bending either side to the other.

Two things this module deliberately does NOT do:

* **It does not invent capabilities.** An operation the underlying adapter has
  no way to perform raises an explicit "unsupported" error rather than returning
  a successful-looking no-op. An executor that believed a config change had been
  applied when nothing happened would then verify against an unchanged system
  and report a confusing failure.
* **It does not retry.** Retry policy belongs to the caller, which knows whether
  the action is idempotent.

Deployment history comes from Postgres, not from the runtime. Compose has no
concept of release history, and even on Kubernetes the system of record for
"what did Aegis deploy" is Aegis's own table (CLAUDE.md 3.10).
"""

from __future__ import annotations

from typing import Any

from aegis.core.errors import ExternalServiceError, NotFoundError
from aegis.core.logging import get_logger
from aegis.domain.enums import ServiceHealth
from aegis.execution.ports import DeploymentInfo, InstanceInfo, OperationResult
from aegis.integrations.runtime import InstanceInfo as AdapterInstance
from aegis.integrations.runtime import RuntimeAdapter, WriteResult
from aegis.persistence.db import Database

log = get_logger(__name__)


def _to_operation(result: WriteResult) -> OperationResult:
    """Translate an adapter write into the executor's result shape.

    ``no_op`` becomes ``changed=False`` rather than being discarded: the
    verification engine needs it to interpret a before/after comparison, because
    an unchanged system that was already correct looks identical to one that was
    never touched.
    """
    return OperationResult(
        ok=result.succeeded,
        performed=result.performed,
        changed=not result.no_op,
        detail={
            "action": result.action,
            "adapter": result.adapter,
            "target": result.target,
            "idempotency_key": result.idempotency_key,
            "note": result.detail,
            "started_at": result.started_at.isoformat(),
            "completed_at": result.completed_at.isoformat(),
        },
        error=None if result.succeeded else result.detail,
    )


def _to_instance(info: AdapterInstance) -> InstanceInfo:
    return InstanceInfo(
        instance_id=info.instance_id,
        service_id=info.service_id,
        name=info.name,
        health=info.health,
        state=info.raw_status,
        image=info.image,
        version=info.version,
        started_at=info.started_at,
        restart_count=info.restart_count,
        labels={"node": info.node} if info.node else {},
    )


class RuntimePortBridge:
    """Satisfies both ``RuntimeReadPort`` and ``RuntimeWritePort``.

    One object implements both halves because a single adapter backs them, but
    the two Protocols stay separate so an investigation agent can be handed the
    read side alone and have no reachable method that mutates anything.
    """

    __slots__ = ("_adapter", "_db")

    def __init__(self, adapter: RuntimeAdapter, *, db: Database | None = None) -> None:
        self._adapter = adapter
        self._db = db

    @property
    def available(self) -> bool:
        return self._adapter.available

    @property
    def unavailable_reason(self) -> str:
        return self._adapter.unavailable_reason

    def _require(self, operation: str) -> RuntimeAdapter:
        if not self._adapter.available:
            raise ExternalServiceError(
                f"{operation} is unavailable: {self._adapter.unavailable_reason}",
                code="RUNTIME_UNAVAILABLE",
                retryable=False,
            )
        return self._adapter

    # ---- read ------------------------------------------------------------- #

    async def list_services(self) -> list[Any]:
        return await self._require("list_services").list_services()

    async def list_instances(self, service_id: str) -> list[InstanceInfo]:
        raw = await self._require("list_instances").list_instances(service_id)
        return [_to_instance(i) for i in raw]

    async def get_instance(self, instance_id: str) -> InstanceInfo:
        """Find one instance across services.

        The adapters index instances by service, so this scans. Bounded by the
        number of services in the environment, which is small by construction -
        an environment with thousands would need a different adapter anyway.
        """
        adapter = self._require("get_instance")
        for state in await adapter.list_services():
            for info in await adapter.list_instances(state.ref.service_id):
                if info.instance_id == instance_id or info.name == instance_id:
                    return _to_instance(info)
        raise ExternalServiceError(
            f"instance {instance_id!r} was not found in this environment",
            code="INSTANCE_NOT_FOUND",
            retryable=False,
        )

    async def health(self, service_id: str) -> ServiceHealth:
        return await self._require("health").health(service_id)

    async def current_deployment(self, service_id: str) -> DeploymentInfo | None:
        """What the runtime reports as running right now.

        Returns ``None`` only when the service is genuinely absent. An adapter
        that cannot be reached raises instead, so "not deployed" never masks
        "we cannot see the environment".
        """
        adapter = self._require("current_deployment")
        try:
            state = await adapter.get_service(service_id)
        except NotFoundError:
            # The adapter reached the environment and the service was not
            # there. That is an answer, not a failure. An adapter that could
            # not look raises something else and is left to propagate.
            return None
        return DeploymentInfo(
            deployment_id=f"runtime:{state.ref.service_id}",
            service_id=state.ref.service_id,
            version=state.version or "unknown",
            replicas_desired=state.desired_instances,
            replicas_ready=state.ready_instances,
        )

    async def deployment_history(
        self, service_id: str, *, limit: int = 10
    ) -> list[DeploymentInfo]:
        """Past deployments, from Aegis's own records plus what is live now.

        Postgres is the system of record. A rollback target is checked against
        versions Aegis actually observed being deployed, so a proposal cannot
        name a version that never existed on this service.
        """
        history: list[DeploymentInfo] = []
        if self._db is not None:
            rows = await self._db.fetch(
                """
                SELECT id, service_id, to_version, from_version, environment,
                       started_at, state
                  FROM deployment_attempts
                 WHERE service_id = $1
                   AND state IN ('DEPLOYED','VERIFIED','ROLLED_BACK')
                   AND to_version IS NOT NULL
                 ORDER BY started_at DESC
                 LIMIT $2
                """,
                service_id, min(limit, 50),
            )
            history.extend(
                DeploymentInfo(
                    deployment_id=r["id"],
                    service_id=r["service_id"],
                    version=r["to_version"],
                    previous_version=r["from_version"],
                    created_at=r["started_at"],
                )
                for r in rows
            )

        # The live version counts as known history even when Aegis did not
        # deploy it: rolling back to what is currently running is a valid no-op,
        # and refusing it would be surprising.
        try:
            current = await self.current_deployment(service_id)
        except ExternalServiceError:
            current = None
        if current is not None and all(d.version != current.version for d in history):
            history.insert(0, current)
        return history[:limit]

    # ---- write ------------------------------------------------------------ #

    async def restart_instance(
        self, instance_id: str, *, idempotency_key: str, timeout_s: float
    ) -> OperationResult:
        del timeout_s  # the adapter owns its own deadline via guarded_call
        result = await self._require("restart_instance").restart_instance(
            instance_id, idempotency_key=idempotency_key
        )
        return _to_operation(result)

    async def scale(
        self, service_id: str, *, replicas: int, idempotency_key: str, timeout_s: float
    ) -> OperationResult:
        del timeout_s
        result = await self._require("scale").scale(
            service_id, replicas, idempotency_key=idempotency_key
        )
        return _to_operation(result)

    async def drain_instance(
        self, instance_id: str, *, idempotency_key: str, timeout_s: float
    ) -> OperationResult:
        del timeout_s
        result = await self._require("drain_instance").drain_instance(
            instance_id, idempotency_key=idempotency_key
        )
        return _to_operation(result)

    async def rollback_deployment(
        self, service_id: str, *, to_version: str, idempotency_key: str, timeout_s: float
    ) -> OperationResult:
        del timeout_s
        result = await self._require("rollback_deployment").rollback_deployment(
            service_id, to_version, idempotency_key=idempotency_key
        )
        return _to_operation(result)

    async def update_config(
        self,
        service_id: str,
        *,
        changes: dict[str, str],
        idempotency_key: str,
        timeout_s: float,
    ) -> OperationResult:
        """Not supported by any current runtime adapter.

        Raising is the correct behaviour. Returning a successful no-op would
        make the executor believe a config change landed, and verification would
        then measure an unchanged system and report a baffling failure. An
        explicit "unsupported" tells the operator exactly what to do instead.
        """
        del changes, idempotency_key, timeout_s
        raise ExternalServiceError(
            f"the {type(self._adapter).__name__} adapter cannot change runtime "
            f"configuration for {service_id}; apply the change through the "
            "deployment pipeline instead",
            code="OPERATION_UNSUPPORTED",
            retryable=False,
        )


class RedisCachePort:
    """Key-scoped cache eviction backed by Redis.

    Exposes exactly one operation. There is no flush, no pattern delete and no
    key scan, so a tier-1 "clear this key" action has no reachable path to
    becoming "clear every key" - the capability simply is not here.
    """

    __slots__ = ("_redis",)

    def __init__(self, redis: Any) -> None:
        self._redis = redis

    @property
    def available(self) -> bool:
        return self._redis is not None

    async def delete_key(
        self, namespace: str, key: str, *, idempotency_key: str
    ) -> OperationResult:
        if self._redis is None:
            raise ExternalServiceError(
                "no cache client is configured",
                code="CACHE_UNAVAILABLE",
                retryable=False,
            )
        full_key = f"{namespace}:{key}"
        try:
            removed = int(await self._redis.delete(full_key))
        except Exception as exc:  # noqa: BLE001 - normalised for the executor
            raise ExternalServiceError(
                f"cache delete failed: {type(exc).__name__}",
                code="CACHE_WRITE_FAILED",
                retryable=False,
            ) from exc
        # Deleting an absent key is a success but not a change. Reporting it as
        # a change would make a no-op look like a remediation in the audit log.
        return OperationResult(
            ok=True,
            performed=f"DEL {full_key}",
            changed=removed > 0,
            detail={"keys_removed": removed, "idempotency_key": idempotency_key},
        )


__all__ = ["RedisCachePort", "RuntimePortBridge"]
