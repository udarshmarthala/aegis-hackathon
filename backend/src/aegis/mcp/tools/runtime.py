"""Environment tools. Every one of them is read-only.

``RuntimeAdapter`` splits its surface into ``READ_METHODS`` and
``WRITE_METHODS``. This module binds only the former, and the binding is
asserted at import time: if someone adds a runtime write method to a tool here,
``register`` raises rather than exposing it. The write methods are reachable
only from ``ActionExecutor``, which accepts nothing but a ``ValidatedAction``.

Instance logs are Tier D. A container writes whatever it likes to stdout, and
what it writes is frequently a remote payload it just received.
"""

from __future__ import annotations

from pydantic import Field

from aegis.core.errors import SourceUnavailable
from aegis.domain.enums import EvidenceType, SourceType
from aegis.domain.models import ServiceState, UntrustedText
from aegis.integrations.runtime import RuntimeAdapter
from aegis.mcp.deps import ToolDeps
from aegis.mcp.registry import ToolRegistry
from aegis.mcp.tools import support
from aegis.mcp.types import (
    ENVIRONMENTS,
    ToolContext,
    ToolContractError,
    ToolInput,
    ToolOutcome,
    ToolOutput,
    ToolSpec,
)

MAX_SERVICES = 200
MAX_INSTANCES = 100
MAX_LOG_LINES = 500
MAX_DEPLOYMENTS = 25

# The adapter methods this module is allowed to call. Checked against
# ``RuntimeAdapter.READ_METHODS`` at registration so the two cannot drift.
BOUND_METHODS = frozenset({"list_services", "get_service", "list_instances", "get_logs", "health"})


# --------------------------------------------------------------------------- #
# models                                                                       #
# --------------------------------------------------------------------------- #


class NoArgsInput(ToolInput):
    """Some environment questions genuinely take no argument."""


class ServiceInput(ToolInput):
    service_id: str = Field(min_length=3, max_length=200)


class ServiceStateOut(ToolOutput):
    service_id: str
    name: str
    environment: str
    health: str
    version: str | None = None
    desired_instances: int = 0
    ready_instances: int = 0
    error_rate: float | None = None
    latency_p99_ms: float | None = None
    owner_team: str | None = None
    degraded_replicas: bool = False


class ServiceListOutput(ToolOutput):
    adapter: str
    services: tuple[ServiceStateOut, ...] = ()

    @property
    def is_empty(self) -> bool:
        return not self.services


class InstanceOut(ToolOutput):
    instance_id: str
    service_id: str
    name: str
    raw_status: str
    health: str
    image: str | None = None
    version: str | None = None
    started_at: str | None = None
    restart_count: int = 0
    node: str | None = None


class InstanceListOutput(ToolOutput):
    service_id: str
    instances: tuple[InstanceOut, ...] = ()

    @property
    def is_empty(self) -> bool:
        return not self.instances


class InstanceLogsInput(ToolInput):
    instance_id: str = Field(min_length=1, max_length=200)
    lines: int = Field(default=200, ge=1, le=MAX_LOG_LINES)


class InstanceLogsOutput(ToolOutput):
    instance_id: str
    truncated: bool = False
    # Container stdout. Tier D, always, without exception.
    lines: tuple[UntrustedText, ...] = ()

    @property
    def is_empty(self) -> bool:
        return not self.lines


class ServiceHealthOutput(ToolOutput):
    service_id: str
    health: str
    adapter: str = ""


class CurrentDeploymentOutput(ToolOutput):
    service_id: str
    version: str | None = None
    desired_instances: int = 0
    ready_instances: int = 0
    images: tuple[str, ...] = ()
    adapter: str = ""

    @property
    def is_empty(self) -> bool:
        return self.version is None and not self.images


class DeploymentHistoryInput(ToolInput):
    service_id: str = Field(min_length=3, max_length=200)
    limit: int = Field(default=10, ge=1, le=MAX_DEPLOYMENTS)


class DeploymentOut(ToolOutput):
    deployment_id: str
    version: str
    status: str
    deployed_at: str | None = None
    commit_sha: str | None = None
    commit_repo: str | None = None
    commit_author: str | None = None


class DeploymentHistoryOutput(ToolOutput):
    service_id: str
    deployments: tuple[DeploymentOut, ...] = ()

    @property
    def is_empty(self) -> bool:
        return not self.deployments


# --------------------------------------------------------------------------- #
# registration                                                                 #
# --------------------------------------------------------------------------- #


def _state_out(state: ServiceState) -> ServiceStateOut:
    """Normalised service state, identical in shape from Compose, K8s or ECS."""
    return ServiceStateOut(
        service_id=state.ref.service_id,
        name=state.ref.name,
        environment=state.ref.environment,
        health=state.health.value,
        version=state.version,
        desired_instances=state.desired_instances,
        ready_instances=state.ready_instances,
        error_rate=state.error_rate,
        latency_p99_ms=state.latency_p99_ms,
        owner_team=state.owner_team,
        degraded_replicas=state.is_degraded,
    )


def register(registry: ToolRegistry, deps: ToolDeps) -> None:
    """Declare the runtime read tools against an injected dependency set."""
    stray = BOUND_METHODS - RuntimeAdapter.READ_METHODS
    if stray:
        # A method that is not a declared read has no business in this module,
        # and finding that out at import time is the whole point.
        raise ToolContractError(
            "runtime tools may only bind declared read methods",
            context={"methods": sorted(stray)},
        )

    async def list_services(context: ToolContext, args: NoArgsInput) -> ToolOutcome:
        _ = args  # the tool takes no arguments; the model is the contract
        adapter_name = deps.runtime.name if deps.runtime is not None else "none"
        empty = ServiceListOutput(adapter=adapter_name)
        if deps.runtime is None:
            return await support.degraded(
                deps, context, source="runtime", source_type=SourceType.RUNTIME,
                reason="no runtime adapter is configured", value=empty,
            )
        if not deps.runtime.available:
            return await support.degraded(
                deps, context, source=f"runtime.{adapter_name}",
                source_type=SourceType.RUNTIME,
                reason=deps.runtime.unavailable_reason or "runtime adapter unavailable",
                value=empty,
            )
        try:
            states = await deps.runtime.list_services()
        except SourceUnavailable as exc:
            return await support.degraded(
                deps, context, source=f"runtime.{adapter_name}",
                source_type=SourceType.RUNTIME, reason=exc.message, value=empty,
            )
        value = ServiceListOutput(
            adapter=adapter_name,
            services=tuple(_state_out(s) for s in states[:MAX_SERVICES]),
        )
        return ToolOutcome(value=value, provenance=(f"runtime://{adapter_name}/services",))

    async def get_service(context: ToolContext, args: ServiceInput) -> ToolOutcome:
        adapter_name = deps.runtime.name if deps.runtime is not None else "none"
        empty = ServiceListOutput(adapter=adapter_name)
        if deps.runtime is None or not deps.runtime.available:
            reason = (
                "no runtime adapter is configured"
                if deps.runtime is None
                else deps.runtime.unavailable_reason or "runtime adapter unavailable"
            )
            return await support.degraded(
                deps, context, source="runtime", source_type=SourceType.RUNTIME,
                reason=reason, value=empty,
            )
        try:
            state = await deps.runtime.get_service(args.service_id)
        except SourceUnavailable as exc:
            return await support.degraded(
                deps, context, source=f"runtime.{adapter_name}",
                source_type=SourceType.RUNTIME, reason=exc.message, value=empty,
            )
        uri = f"runtime://{adapter_name}/service/{args.service_id}"
        value = ServiceListOutput(adapter=adapter_name, services=(_state_out(state),))
        ids = await support.record_evidence(
            deps, context, source=f"runtime.{adapter_name}",
            source_type=SourceType.RUNTIME,
            evidence_type=EvidenceType.INSTANCE_STATE,
            summary=(
                f"{args.service_id}: health={state.health.value} "
                f"ready={state.ready_instances}/{state.desired_instances}"
            ),
            structured_value=value.model_dump(mode="json"),
            provenance_uri=uri, resource_id=args.service_id,
        )
        return ToolOutcome(value=value, evidence_ids=ids, provenance=(uri,))

    async def list_instances(context: ToolContext, args: ServiceInput) -> ToolOutcome:
        adapter_name = deps.runtime.name if deps.runtime is not None else "none"
        empty = InstanceListOutput(service_id=args.service_id)
        if deps.runtime is None or not deps.runtime.available:
            reason = (
                "no runtime adapter is configured"
                if deps.runtime is None
                else deps.runtime.unavailable_reason or "runtime adapter unavailable"
            )
            return await support.degraded(
                deps, context, source="runtime", source_type=SourceType.RUNTIME,
                reason=reason, value=empty,
            )
        try:
            instances = await deps.runtime.list_instances(args.service_id)
        except SourceUnavailable as exc:
            return await support.degraded(
                deps, context, source=f"runtime.{adapter_name}",
                source_type=SourceType.RUNTIME, reason=exc.message, value=empty,
            )
        if not instances:
            return ToolOutcome(value=empty)
        uri = f"runtime://{adapter_name}/service/{args.service_id}/instances"
        value = InstanceListOutput(
            service_id=args.service_id,
            instances=tuple(
                InstanceOut(
                    instance_id=i.instance_id, service_id=i.service_id, name=i.name,
                    raw_status=i.raw_status, health=i.health.value, image=i.image,
                    version=i.version,
                    started_at=i.started_at.isoformat() if i.started_at else None,
                    restart_count=i.restart_count, node=i.node,
                )
                for i in instances[:MAX_INSTANCES]
            ),
        )
        ids = await support.record_evidence(
            deps, context, source=f"runtime.{adapter_name}",
            source_type=SourceType.RUNTIME,
            evidence_type=EvidenceType.INSTANCE_STATE,
            summary=(
                f"{args.service_id}: {len(instances)} instances, "
                f"{sum(i.restart_count for i in instances)} total restarts"
            ),
            structured_value=value.model_dump(mode="json"),
            provenance_uri=uri, resource_id=args.service_id,
        )
        return ToolOutcome(value=value, evidence_ids=ids, provenance=(uri,))

    async def instance_logs(context: ToolContext, args: InstanceLogsInput) -> ToolOutcome:
        adapter_name = deps.runtime.name if deps.runtime is not None else "none"
        empty = InstanceLogsOutput(instance_id=args.instance_id)
        if deps.runtime is None or not deps.runtime.available:
            reason = (
                "no runtime adapter is configured"
                if deps.runtime is None
                else deps.runtime.unavailable_reason or "runtime adapter unavailable"
            )
            return await support.degraded(
                deps, context, source="runtime", source_type=SourceType.RUNTIME,
                reason=reason, value=empty,
            )
        try:
            chunk = await deps.runtime.get_logs(args.instance_id, args.lines)
        except SourceUnavailable as exc:
            return await support.degraded(
                deps, context, source=f"runtime.{adapter_name}",
                source_type=SourceType.LOGS, reason=exc.message, value=empty,
            )
        if not chunk.lines:
            return ToolOutcome(value=empty)
        uri = f"runtime://{adapter_name}/instance/{args.instance_id}/logs?lines={args.lines}"
        value = InstanceLogsOutput(
            instance_id=chunk.instance_id,
            truncated=chunk.truncated,
            lines=chunk.lines[:MAX_LOG_LINES],
        )
        ids = await support.record_evidence(
            deps, context, source=f"runtime.{adapter_name}",
            source_type=SourceType.LOGS, evidence_type=EvidenceType.LOG_MATCH,
            summary=f"{len(chunk.lines)} log lines from instance {args.instance_id}",
            structured_value={
                "instance_id": args.instance_id, "lines": len(chunk.lines),
                "truncated": chunk.truncated,
            },
            provenance_uri=uri, resource_id=args.instance_id,
            content="\n".join(line.text for line in chunk.lines[:200]),
            untrusted=True,
        )
        return ToolOutcome(value=value, evidence_ids=ids, provenance=(uri,))

    async def service_health(context: ToolContext, args: ServiceInput) -> ToolOutcome:
        adapter_name = deps.runtime.name if deps.runtime is not None else "none"
        empty = ServiceHealthOutput(
            service_id=args.service_id, health="unknown", adapter=adapter_name
        )
        if deps.runtime is None or not deps.runtime.available:
            reason = (
                "no runtime adapter is configured"
                if deps.runtime is None
                else deps.runtime.unavailable_reason or "runtime adapter unavailable"
            )
            # "unknown health" and "we could not ask" must not look identical to
            # the reader, so the gap travels with the value.
            return await support.degraded(
                deps, context, source="runtime", source_type=SourceType.RUNTIME,
                reason=reason, value=empty,
            )
        try:
            health = await deps.runtime.health(args.service_id)
        except SourceUnavailable as exc:
            return await support.degraded(
                deps, context, source=f"runtime.{adapter_name}",
                source_type=SourceType.RUNTIME, reason=exc.message, value=empty,
            )
        uri = f"runtime://{adapter_name}/service/{args.service_id}/health"
        return ToolOutcome(
            value=ServiceHealthOutput(
                service_id=args.service_id, health=health.value, adapter=adapter_name
            ),
            provenance=(uri,),
        )

    async def current_deployment(context: ToolContext, args: ServiceInput) -> ToolOutcome:
        adapter_name = deps.runtime.name if deps.runtime is not None else "none"
        empty = CurrentDeploymentOutput(service_id=args.service_id, adapter=adapter_name)
        if deps.runtime is None or not deps.runtime.available:
            reason = (
                "no runtime adapter is configured"
                if deps.runtime is None
                else deps.runtime.unavailable_reason or "runtime adapter unavailable"
            )
            return await support.degraded(
                deps, context, source="runtime", source_type=SourceType.RUNTIME,
                reason=reason, value=empty,
            )
        try:
            state = await deps.runtime.get_service(args.service_id)
            instances = await deps.runtime.list_instances(args.service_id)
        except SourceUnavailable as exc:
            return await support.degraded(
                deps, context, source=f"runtime.{adapter_name}",
                source_type=SourceType.RUNTIME, reason=exc.message, value=empty,
            )
        uri = f"runtime://{adapter_name}/service/{args.service_id}/deployment"
        value = CurrentDeploymentOutput(
            service_id=args.service_id,
            version=state.version,
            desired_instances=state.desired_instances,
            ready_instances=state.ready_instances,
            images=tuple(sorted({i.image for i in instances if i.image})),
            adapter=adapter_name,
        )
        ids = await support.record_evidence(
            deps, context, source=f"runtime.{adapter_name}",
            source_type=SourceType.DEPLOYMENT,
            evidence_type=EvidenceType.DEPLOYMENT_EVENT,
            summary=f"{args.service_id} currently running version {state.version}",
            structured_value=value.model_dump(mode="json"),
            provenance_uri=uri, resource_id=args.service_id,
        )
        return ToolOutcome(value=value, evidence_ids=ids, provenance=(uri,))

    async def deployment_history(
        context: ToolContext, args: DeploymentHistoryInput
    ) -> ToolOutcome:
        empty = DeploymentHistoryOutput(service_id=args.service_id)
        if deps.traversal is None:
            return await support.degraded(
                deps, context, source="neo4j", source_type=SourceType.DEPLOYMENT,
                reason="deployment history needs the topology graph, which is "
                       "not configured",
                value=empty,
            )
        try:
            rows = await deps.traversal.recent_deployments_for(
                args.service_id, args.limit
            )
        except SourceUnavailable as exc:
            return await support.degraded(
                deps, context, source="neo4j", source_type=SourceType.DEPLOYMENT,
                reason=exc.message, value=empty,
            )
        if not rows:
            return ToolOutcome(value=empty)
        uri = f"graph://deployments/{args.service_id}?limit={args.limit}"
        value = DeploymentHistoryOutput(
            service_id=args.service_id,
            deployments=tuple(
                DeploymentOut(
                    deployment_id=d.deployment_id, version=d.version, status=d.status,
                    deployed_at=d.deployed_at.isoformat() if d.deployed_at else None,
                    commit_sha=d.commit_sha, commit_repo=d.commit_repo,
                    commit_author=d.commit_author,
                )
                for d in rows[:MAX_DEPLOYMENTS]
            ),
        )
        ids = await support.record_evidence(
            deps, context, source="neo4j", source_type=SourceType.DEPLOYMENT,
            evidence_type=EvidenceType.DEPLOYMENT_EVENT,
            summary=f"{len(rows)} recent deployments for {args.service_id}",
            structured_value=value.model_dump(mode="json"),
            provenance_uri=uri, resource_id=args.service_id,
        )
        return ToolOutcome(value=value, evidence_ids=ids, provenance=(uri,))

    # ---- specs ----------------------------------------------------------- #

    def _spec(
        name: str,
        description: str,
        input_model: type[ToolInput],
        output_model: type[ToolOutput],
        *,
        timeout_s: float = 15.0,
    ) -> ToolSpec:
        """Every runtime tool is a bounded, idempotent, retryable read."""
        return ToolSpec(
            name=name,
            description=description,
            server="runtime",
            input_model=input_model,
            output_model=output_model,
            access="read",
            mutates="nothing",
            scope="runtime:read",
            environments=ENVIRONMENTS,
            timeout_s=timeout_s,
            retryable=True,
            idempotent=True,
            cost_hint="cheap",
        )

    registry.register(
        _spec(
            "list_services",
            "Every service the observed workload is running, with health and replicas.",
            NoArgsInput,
            ServiceListOutput,
            timeout_s=20.0,
        ),
        list_services,
    )
    registry.register(
        _spec(
            "get_service",
            "Normalised state of one service: health, version and replica counts.",
            ServiceInput,
            ServiceListOutput,
        ),
        get_service,
    )
    registry.register(
        _spec(
            "list_instances",
            "Running units behind a service, with restart counts and raw status.",
            ServiceInput,
            InstanceListOutput,
        ),
        list_instances,
    )
    registry.register(
        _spec(
            "instance_logs",
            "Recent stdout from one instance, returned as UntrustedText.",
            InstanceLogsInput,
            InstanceLogsOutput,
            timeout_s=20.0,
        ),
        instance_logs,
    )
    registry.register(
        _spec(
            "service_health",
            "Normalised health verdict for one service.",
            ServiceInput,
            ServiceHealthOutput,
            timeout_s=10.0,
        ),
        service_health,
    )
    registry.register(
        _spec(
            "current_deployment",
            "The version and images a service is running right now.",
            ServiceInput,
            CurrentDeploymentOutput,
            timeout_s=20.0,
        ),
        current_deployment,
    )
    registry.register(
        _spec(
            "deployment_history",
            "Recent deployments for a service, newest first, with the commit each carried.",
            DeploymentHistoryInput,
            DeploymentHistoryOutput,
            timeout_s=20.0,
        ),
        deployment_history,
    )


# ``NotFoundError`` from an adapter is deliberately not caught here: a service
# that does not exist is neither an empty result nor an outage, and the invoker
# turns it into a typed NOT_FOUND result the caller can act on.

__all__ = ["BOUND_METHODS", "register"]
