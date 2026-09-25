"""Concrete executors, one per executable action type.

An executor is deliberately small and dumb. By the time it runs, every question
worth asking has been answered: policy said yes, a human approved it if one was
required, the lease is held, and the citations resolved. The executor's only job
is to perform one bounded operation against the environment adapter and report
precisely what it did.

Two rules shape every method here:

* **Accept nothing but a ``ValidatedAction``.** There is no overload that takes
  an ``ActionProposal``. A model's output cannot reach these functions.
* **Never retry internally.** ``restart_instance`` is idempotent and safe to
  retry; ``promote_patch`` is not. The decision belongs to the caller, which
  knows the action profile; burying a retry loop here would eventually replay a
  non-idempotent write.

Tier-3 action types have no executor in this module at all. There is nothing to
call even if a decision were somehow wrong (CLAUDE.md 3.1).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from aegis.core.errors import ExternalServiceError, ValidationError
from aegis.core.logging import get_logger
from aegis.domain.enums import ActionType
from aegis.execution.ports import CachePort, OperationResult, RuntimeReadPort, RuntimeWritePort
from aegis.execution.validated import ValidatedAction

log = get_logger(__name__)

# A tier-1 scale-up is bounded by definition. Anything larger is a different
# action type with a different risk tier, not the same action with a big number.
MAX_TIER1_SCALE_DELTA = 2
MAX_ABSOLUTE_REPLICAS = 20


@dataclass(frozen=True, slots=True)
class ExecutionOutcome:
    """What an executor actually did, in terms the audit log can store."""

    ok: bool
    performed: str
    changed: bool = True
    detail: dict[str, Any] = field(default_factory=dict)
    error: str | None = None

    @classmethod
    def from_operation(cls, result: OperationResult) -> ExecutionOutcome:
        return cls(
            ok=result.ok,
            performed=result.performed,
            changed=result.changed,
            detail=dict(result.detail),
            error=result.error,
        )

    @classmethod
    def failure(cls, performed: str, error: str) -> ExecutionOutcome:
        return cls(ok=False, performed=performed, changed=False, error=error)


@dataclass(frozen=True, slots=True)
class ExecutionPorts:
    """Everything an executor may touch. Nothing else is reachable."""

    runtime_read: RuntimeReadPort
    runtime_write: RuntimeWritePort
    cache: CachePort | None = None
    timeout_s: float = 60.0


class Executor(Protocol):
    """The executor contract."""

    action_type: ActionType

    async def execute(
        self, validated: ValidatedAction, ports: ExecutionPorts
    ) -> ExecutionOutcome: ...

    async def rollback(
        self, validated: ValidatedAction, ports: ExecutionPorts, outcome: ExecutionOutcome
    ) -> ExecutionOutcome: ...


class _Base:
    """Shared argument handling.

    Action arguments live in ``expected_effect`` and the resource ref rather than
    in free-form model text, and every one of them is re-validated here. An
    argument that arrived out of range is a bug or an attack, and either way the
    executor refuses rather than clamping silently - a clamped value would run a
    different action from the one policy assessed.
    """

    action_type: ActionType

    @staticmethod
    def _require_write_port(ports: ExecutionPorts) -> RuntimeWritePort:
        if not ports.runtime_write.available:
            raise ExternalServiceError(
                "runtime write adapter is not available in this environment",
                code="RUNTIME_UNAVAILABLE",
                retryable=False,
            )
        return ports.runtime_write

    @staticmethod
    def _arg(validated: ValidatedAction, name: str) -> Any:
        """Read one action argument from the typed argument channel.

        Arguments come from ``ActionProposal.arguments``, which pydantic has
        already constrained to bounded scalars. Nothing is read from the
        proposal's free-text ``reason``.
        """
        args = validated.proposal.arguments or validated.action.arguments
        if name not in args:
            raise ValidationError(
                f"action argument {name!r} is missing",
                context={"action_id": validated.action.id, "argument": name},
            )
        return args[name]

    @classmethod
    def _int_arg(cls, validated: ValidatedAction, name: str, *, lo: int, hi: int) -> int:
        """Read an integer argument and refuse anything outside its range.

        Refusing rather than clamping is deliberate: a clamped value would run a
        different action from the one policy assessed, and the audit trail would
        record an argument nobody proposed.
        """
        raw = cls._arg(validated, name)
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            raise ValidationError(
                f"action argument {name!r} is not numeric",
                context={"action_id": validated.action.id, "value": str(raw)},
            )
        value = int(raw)
        if value != raw or not lo <= value <= hi:
            raise ValidationError(
                f"action argument {name!r}={raw} is outside the permitted range "
                f"[{lo}, {hi}]",
                context={"action_id": validated.action.id, "value": str(raw)},
            )
        return value

    @classmethod
    def _str_arg(cls, validated: ValidatedAction, name: str, *, max_len: int = 256) -> str:
        raw = cls._arg(validated, name)
        if not isinstance(raw, str) or not raw.strip():
            raise ValidationError(
                f"action argument {name!r} must be a non-empty string",
                context={"action_id": validated.action.id},
            )
        value = raw.strip()
        if len(value) > max_len:
            raise ValidationError(
                f"action argument {name!r} exceeds {max_len} characters",
                context={"action_id": validated.action.id},
            )
        return value


class RestartInstanceExecutor(_Base):
    """Tier 1. Restart exactly one instance.

    The narrowest useful write in the system: one instance, idempotent, and
    reversible in the sense that matters - the instance comes back. Rollback is
    a no-op because there is no prior state to restore; the compensating action
    if the restart made things worse is to escalate, not to un-restart.
    """

    action_type = ActionType.RESTART_INSTANCE

    async def execute(
        self, validated: ValidatedAction, ports: ExecutionPorts
    ) -> ExecutionOutcome:
        write = self._require_write_port(ports)
        instance_id = validated.target.resource_id
        result = await write.restart_instance(
            instance_id,
            idempotency_key=validated.idempotency_key,
            timeout_s=ports.timeout_s,
        )
        return ExecutionOutcome.from_operation(result)

    async def rollback(
        self, validated: ValidatedAction, ports: ExecutionPorts, outcome: ExecutionOutcome
    ) -> ExecutionOutcome:
        return ExecutionOutcome(
            ok=True,
            performed="no-op",
            changed=False,
            detail={
                "reason": "a restart has no inverse; recovery is escalation, "
                          "not a second restart",
                "instance_id": validated.target.resource_id,
            },
        )


class RerunHealthCheckExecutor(_Base):
    """Tier 1. Re-run a read-only probe.

    Classified as a write action only because it is initiated by Aegis and must
    be audited. It mutates nothing, so it needs no verification plan and has no
    rollback.
    """

    action_type = ActionType.RERUN_HEALTH_CHECK

    async def execute(
        self, validated: ValidatedAction, ports: ExecutionPorts
    ) -> ExecutionOutcome:
        service_id = validated.target.service_id or validated.target.resource_id
        if not ports.runtime_read.available:
            raise ExternalServiceError(
                "runtime adapter is not available; health cannot be re-probed",
                code="RUNTIME_UNAVAILABLE",
                retryable=False,
            )
        health = await ports.runtime_read.health(service_id)
        return ExecutionOutcome(
            ok=True,
            performed=f"health probe {service_id}",
            changed=False,
            detail={"service_id": service_id, "health": health.value},
        )

    async def rollback(
        self, validated: ValidatedAction, ports: ExecutionPorts, outcome: ExecutionOutcome
    ) -> ExecutionOutcome:
        return ExecutionOutcome(ok=True, performed="no-op", changed=False)


class ScaleUpBoundedExecutor(_Base):
    """Tier 1. Add at most ``MAX_TIER1_SCALE_DELTA`` replicas.

    The bound is the whole reason this is tier 1 rather than tier 2. It is
    enforced here against the *observed* current replica count, not against a
    number the proposal supplied, so a proposal claiming a low baseline cannot
    talk the executor into an unbounded scale-up.
    """

    action_type = ActionType.SCALE_UP_BOUNDED

    async def execute(
        self, validated: ValidatedAction, ports: ExecutionPorts
    ) -> ExecutionOutcome:
        write = self._require_write_port(ports)
        service_id = validated.target.service_id or validated.target.resource_id
        delta = self._int_arg(validated, "replica_delta", lo=1, hi=MAX_TIER1_SCALE_DELTA)

        current = await ports.runtime_read.current_deployment(service_id)
        if current is None:
            raise ExternalServiceError(
                f"cannot read the current replica count for {service_id}; "
                "a bounded scale-up requires a known baseline",
                code="BASELINE_UNKNOWN",
                retryable=False,
            )
        target = current.replicas_desired + delta
        if target > MAX_ABSOLUTE_REPLICAS:
            raise ValidationError(
                f"scaling {service_id} to {target} exceeds the absolute cap of "
                f"{MAX_ABSOLUTE_REPLICAS}",
                context={"action_id": validated.action.id, "target": target},
            )

        result = await write.scale(
            service_id,
            replicas=target,
            idempotency_key=validated.idempotency_key,
            timeout_s=ports.timeout_s,
        )
        outcome = ExecutionOutcome.from_operation(result)
        return ExecutionOutcome(
            ok=outcome.ok,
            performed=outcome.performed,
            changed=outcome.changed,
            detail={
                **outcome.detail,
                "from_replicas": current.replicas_desired,
                "to_replicas": target,
            },
            error=outcome.error,
        )

    async def rollback(
        self, validated: ValidatedAction, ports: ExecutionPorts, outcome: ExecutionOutcome
    ) -> ExecutionOutcome:
        """Scale back to the replica count recorded before the change.

        Uses the observed baseline captured in ``execute``. If that is missing -
        because execution failed before reading it - there is nothing to undo.
        """
        original = outcome.detail.get("from_replicas")
        if original is None:
            return ExecutionOutcome(
                ok=True,
                performed="no-op",
                changed=False,
                detail={"reason": "no baseline replica count was recorded"},
            )
        write = self._require_write_port(ports)
        service_id = validated.target.service_id or validated.target.resource_id
        result = await write.scale(
            service_id,
            replicas=int(original),
            idempotency_key=f"{validated.idempotency_key}:rollback",
            timeout_s=ports.timeout_s,
        )
        return ExecutionOutcome.from_operation(result)


class ClearCacheKeyExecutor(_Base):
    """Tier 1. Evict one named key in one namespace.

    There is no flush-all path anywhere in this module. "Clear the cache" cannot
    become "clear every cache" through a wildcard argument, because the key is
    validated as a literal and the port exposes no bulk operation.
    """

    action_type = ActionType.CLEAR_CACHE_KEY

    async def execute(
        self, validated: ValidatedAction, ports: ExecutionPorts
    ) -> ExecutionOutcome:
        if ports.cache is None or not ports.cache.available:
            raise ExternalServiceError(
                "no cache adapter is configured for this environment",
                code="CACHE_UNAVAILABLE",
                retryable=False,
            )
        namespace = self._str_arg(validated, "namespace", max_len=128)
        key = self._str_arg(validated, "key", max_len=256)
        if any(ch in key for ch in "*?[]") or any(ch in namespace for ch in "*?[]"):
            raise ValidationError(
                "cache key and namespace must be literals; glob patterns are refused",
                context={"action_id": validated.action.id},
            )
        result = await ports.cache.delete_key(
            namespace, key, idempotency_key=validated.idempotency_key
        )
        return ExecutionOutcome.from_operation(result)

    async def rollback(
        self, validated: ValidatedAction, ports: ExecutionPorts, outcome: ExecutionOutcome
    ) -> ExecutionOutcome:
        """An evicted key cannot be restored - but it is rebuildable.

        Recorded explicitly rather than silently succeeding, so an operator
        reviewing the audit trail sees that no reversal took place.
        """
        return ExecutionOutcome(
            ok=True,
            performed="no-op",
            changed=False,
            detail={
                "reason": "a cleared cache key cannot be restored; it is "
                          "repopulated from the system of record on next read"
            },
        )


class RollbackDeploymentExecutor(_Base):
    """Tier 2. Return a service to its previous immutable version.

    Requires a human approval by policy. The target version is read from the
    runtime's own deployment history rather than from the proposal, so a
    proposal cannot name a version that was never deployed.
    """

    action_type = ActionType.ROLLBACK_DEPLOYMENT

    async def execute(
        self, validated: ValidatedAction, ports: ExecutionPorts
    ) -> ExecutionOutcome:
        write = self._require_write_port(ports)
        service_id = validated.target.service_id or validated.target.resource_id
        requested = self._str_arg(validated, "to_version", max_len=200)

        history = await ports.runtime_read.deployment_history(service_id, limit=20)
        known = {d.version for d in history}
        if requested not in known:
            raise ValidationError(
                f"{requested!r} is not in the recorded deployment history for "
                f"{service_id}; refusing to roll back to an unknown version",
                context={
                    "action_id": validated.action.id,
                    "known_versions": sorted(known)[:10],
                },
            )
        current = await ports.runtime_read.current_deployment(service_id)
        if current is not None and current.version == requested:
            return ExecutionOutcome(
                ok=True,
                performed=f"rollback {service_id} -> {requested}",
                changed=False,
                detail={"reason": "service is already running the target version"},
            )

        result = await write.rollback_deployment(
            service_id,
            to_version=requested,
            idempotency_key=validated.idempotency_key,
            timeout_s=ports.timeout_s,
        )
        outcome = ExecutionOutcome.from_operation(result)
        return ExecutionOutcome(
            ok=outcome.ok,
            performed=outcome.performed,
            changed=outcome.changed,
            detail={
                **outcome.detail,
                "from_version": current.version if current else None,
                "to_version": requested,
            },
            error=outcome.error,
        )

    async def rollback(
        self, validated: ValidatedAction, ports: ExecutionPorts, outcome: ExecutionOutcome
    ) -> ExecutionOutcome:
        """Roll forward again to the version that was running before.

        Only attempted when the original version was actually observed. Guessing
        would risk deploying a version that was never live on this service.
        """
        original = outcome.detail.get("from_version")
        if not original:
            return ExecutionOutcome(
                ok=False,
                performed="no-op",
                changed=False,
                error="the pre-rollback version was not recorded; "
                      "manual intervention is required",
            )
        write = self._require_write_port(ports)
        service_id = validated.target.service_id or validated.target.resource_id
        result = await write.rollback_deployment(
            service_id,
            to_version=str(original),
            idempotency_key=f"{validated.idempotency_key}:rollback",
            timeout_s=ports.timeout_s,
        )
        return ExecutionOutcome.from_operation(result)


class ScaleServiceExecutor(_Base):
    """Tier 2. Set an absolute replica count outside the tier-1 bound."""

    action_type = ActionType.SCALE_SERVICE

    async def execute(
        self, validated: ValidatedAction, ports: ExecutionPorts
    ) -> ExecutionOutcome:
        write = self._require_write_port(ports)
        service_id = validated.target.service_id or validated.target.resource_id
        replicas = self._int_arg(validated, "replicas", lo=0, hi=MAX_ABSOLUTE_REPLICAS)
        current = await ports.runtime_read.current_deployment(service_id)
        if current is not None and current.replicas_desired == replicas:
            return ExecutionOutcome(
                ok=True,
                performed=f"scale {service_id} -> {replicas}",
                changed=False,
                detail={"reason": "already at the requested replica count"},
            )
        result = await write.scale(
            service_id,
            replicas=replicas,
            idempotency_key=validated.idempotency_key,
            timeout_s=ports.timeout_s,
        )
        outcome = ExecutionOutcome.from_operation(result)
        return ExecutionOutcome(
            ok=outcome.ok,
            performed=outcome.performed,
            changed=outcome.changed,
            detail={
                **outcome.detail,
                "from_replicas": current.replicas_desired if current else None,
                "to_replicas": replicas,
            },
            error=outcome.error,
        )

    async def rollback(
        self, validated: ValidatedAction, ports: ExecutionPorts, outcome: ExecutionOutcome
    ) -> ExecutionOutcome:
        original = outcome.detail.get("from_replicas")
        if original is None:
            return ExecutionOutcome(
                ok=False,
                performed="no-op",
                changed=False,
                error="the pre-scale replica count was not recorded",
            )
        write = self._require_write_port(ports)
        service_id = validated.target.service_id or validated.target.resource_id
        result = await write.scale(
            service_id,
            replicas=int(original),
            idempotency_key=f"{validated.idempotency_key}:rollback",
            timeout_s=ports.timeout_s,
        )
        return ExecutionOutcome.from_operation(result)


class DrainInstanceExecutor(_Base):
    """Tier 2. Take one instance out of rotation."""

    action_type = ActionType.DRAIN_INSTANCE

    async def execute(
        self, validated: ValidatedAction, ports: ExecutionPorts
    ) -> ExecutionOutcome:
        write = self._require_write_port(ports)
        result = await write.drain_instance(
            validated.target.resource_id,
            idempotency_key=validated.idempotency_key,
            timeout_s=ports.timeout_s,
        )
        return ExecutionOutcome.from_operation(result)

    async def rollback(
        self, validated: ValidatedAction, ports: ExecutionPorts, outcome: ExecutionOutcome
    ) -> ExecutionOutcome:
        """A drained instance is returned to service by restarting it.

        The instance is not un-drained in place: the runtime schedules a
        replacement, and forcing the drained one back into rotation would
        contradict what the scheduler now believes.
        """
        write = self._require_write_port(ports)
        result = await write.restart_instance(
            validated.target.resource_id,
            idempotency_key=f"{validated.idempotency_key}:rollback",
            timeout_s=ports.timeout_s,
        )
        return ExecutionOutcome.from_operation(result)


class UpdateConfigExecutor(_Base):
    """Tier 2. Change bounded runtime configuration for one service.

    Only keys the proposal names are changed, each one a scalar. The executor
    captures the previous values so rollback restores exactly what was there
    rather than a remembered default.
    """

    action_type = ActionType.UPDATE_CONFIG

    async def execute(
        self, validated: ValidatedAction, ports: ExecutionPorts
    ) -> ExecutionOutcome:
        write = self._require_write_port(ports)
        service_id = validated.target.service_id or validated.target.resource_id
        args = dict(validated.proposal.arguments or validated.action.arguments)
        changes = {
            k.removeprefix("config."): str(v)
            for k, v in args.items()
            if k.startswith("config.")
        }
        if not changes:
            raise ValidationError(
                "update_config requires at least one 'config.<key>' argument",
                context={"action_id": validated.action.id},
            )
        result = await write.update_config(
            service_id,
            changes=changes,
            idempotency_key=validated.idempotency_key,
            timeout_s=ports.timeout_s,
        )
        outcome = ExecutionOutcome.from_operation(result)
        return ExecutionOutcome(
            ok=outcome.ok,
            performed=outcome.performed,
            changed=outcome.changed,
            detail={**outcome.detail, "applied": changes},
            error=outcome.error,
        )

    async def rollback(
        self, validated: ValidatedAction, ports: ExecutionPorts, outcome: ExecutionOutcome
    ) -> ExecutionOutcome:
        previous = outcome.detail.get("previous")
        if not isinstance(previous, dict) or not previous:
            return ExecutionOutcome(
                ok=False,
                performed="no-op",
                changed=False,
                error="the previous configuration was not captured by the adapter; "
                      "manual restoration is required",
            )
        write = self._require_write_port(ports)
        service_id = validated.target.service_id or validated.target.resource_id
        result = await write.update_config(
            service_id,
            changes={k: str(v) for k, v in previous.items()},
            idempotency_key=f"{validated.idempotency_key}:rollback",
            timeout_s=ports.timeout_s,
        )
        return ExecutionOutcome.from_operation(result)
