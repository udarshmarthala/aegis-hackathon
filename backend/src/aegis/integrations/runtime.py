"""Environment adapters - how Aegis observes and acts on the workload.

One typed surface, three real backends. A caller that can read a ``ServiceState``
from Compose can read the identical shape from Kubernetes or ECS, which is what
lets the same agent reason about a laptop and a cluster.

**Read and write are structurally separate** (CLAUDE.md section 3.4). The reads
are declared in ``RuntimeAdapter.READ_METHODS`` and may be exposed to agents
directly; the writes are declared in ``RuntimeAdapter.WRITE_METHODS`` and must
pass the full gate chain before the MCP tool layer will call one. The two sets
are disjoint and a test asserts it.

Three rules every write here obeys:

* it takes an ``idempotency_key`` and converges rather than repeating - asking
  for three replicas when three are running is a recorded no-op, not a scale-up;
* it is never retried inside this module. ``attempts=1`` on every write call.
  Retrying a restart because a response was slow is how one restart becomes
  four;
* it returns a ``WriteResult`` naming the exact API call performed, because an
  audit trail that says "restarted a container" is not an audit trail.

**No shell.** Everything goes through the Docker Engine API, the Kubernetes API
or the AWS API. Nothing in this module builds a command string.

Missing backends report themselves missing. If the ``kubernetes`` package or a
kubeconfig is absent the adapter is ``available = False`` and every call raises a
typed error naming the reason - it never pretends to have looked.
"""

from __future__ import annotations

import asyncio
import os
import re
from abc import ABC, abstractmethod
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, ClassVar, Final

from aegis.core.config import Settings
from aegis.core.errors import (
    AegisError,
    ConfigError,
    ExternalServiceError,
    NotFoundError,
    SourceUnavailable,
    ValidationError,
)
from aegis.core.logging import get_logger
from aegis.core.resilience import Bulkhead, guarded_call
from aegis.domain.enums import ServiceHealth
from aegis.domain.models import ServiceRef, ServiceState, UntrustedText

log = get_logger(__name__)

# Reads are quick; a write like a rollback recreates a container and needs room.
READ_TIMEOUT_S: Final = 15.0
WRITE_TIMEOUT_S: Final = 60.0

MAX_LOG_LINES: Final = 2000
MAX_REPLICAS: Final = 10

# Compose containers Aegis created itself carry this label, so a scale-down can
# reclaim its own replicas and will never remove the operator's original.
AEGIS_MANAGED_LABEL: Final = "io.aegis.managed"
COMPOSE_PROJECT_LABEL: Final = "com.docker.compose.project"
COMPOSE_SERVICE_LABEL: Final = "com.docker.compose.service"

# infra/docker/docker-compose.yml pins ``name: aegis-2-0``, so that is the
# project a default local install observes. COMPOSE_PROJECT_NAME overrides it
# for anyone running the stack under another name.
DEFAULT_COMPOSE_PROJECT: Final = "aegis-2-0"

_SERVICE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,126}$")
_INSTANCE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,255}$")
_VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")


# --------------------------------------------------------------------------- #
# normalised shapes                                                            #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class InstanceInfo:
    """One running unit - a container, a pod or an ECS task.

    ``raw_status`` keeps the backend's own word for what it is doing, because an
    operator reading the evidence wants "CrashLoopBackOff", not "critical".
    ``health`` is the normalised judgement layered on top of it.
    """

    instance_id: str
    service_id: str
    name: str
    raw_status: str
    health: ServiceHealth
    image: str | None = None
    version: str | None = None
    started_at: datetime | None = None
    restart_count: int = 0
    node: str | None = None


@dataclass(frozen=True, slots=True)
class LogChunk:
    """Instance logs. Tier D - a container writes whatever it likes to stdout."""

    instance_id: str
    lines: tuple[UntrustedText, ...]
    truncated: bool


@dataclass(frozen=True, slots=True)
class WriteResult:
    """The audit record of one write.

    ``performed`` is the literal API call made. ``no_op`` distinguishes "the
    system was already in the requested state" from "we changed it", which the
    verification engine needs in order to interpret a before/after comparison.
    """

    action: str
    adapter: str
    target: str
    performed: str
    idempotency_key: str
    succeeded: bool
    no_op: bool
    detail: str
    started_at: datetime
    completed_at: datetime


# --------------------------------------------------------------------------- #
# shared helpers                                                               #
# --------------------------------------------------------------------------- #


def _as_dict(raw: object) -> dict[str, Any]:
    """Narrow an untyped JSON member to a mapping.

    External payloads are ``Any`` all the way down; funnelling every lookup
    through one narrowing helper keeps the parsing code readable and keeps a
    ``None`` from turning into an AttributeError three frames later.
    """
    return raw if isinstance(raw, dict) else {}


def _now() -> datetime:
    return datetime.now(UTC)


def service_name_of(service_id: str) -> str:
    """Accept either a canonical ``env:workload:name`` id or a bare name.

    Agents hold canonical ids; a human typing into the console holds a name.
    Both resolve here so no call site has to guess which it was given.
    """
    name = service_id.rsplit(":", 1)[-1] if service_id.count(":") == 2 else service_id
    if not _SERVICE_NAME_RE.match(name):
        raise ValidationError("invalid service identifier", context={"service_id": service_id[:96]})
    return name


def _check_instance_id(instance_id: str) -> str:
    if not _INSTANCE_ID_RE.match(instance_id):
        raise ValidationError(
            "invalid instance identifier", context={"instance_id": instance_id[:96]}
        )
    return instance_id


def _check_version(version: str) -> str:
    if not _VERSION_RE.match(version):
        raise ValidationError("invalid version identifier", context={"version": version[:96]})
    return version


def _check_replicas(replicas: int) -> int:
    """Bounded on both ends. Scaling to zero is an outage, not a remediation."""
    if not 1 <= replicas <= MAX_REPLICAS:
        raise ValidationError(
            f"replicas must be between 1 and {MAX_REPLICAS}", context={"replicas": replicas}
        )
    return replicas


def _check_log_lines(lines: int) -> int:
    if lines < 1:
        raise ValidationError("lines must be >= 1", context={"lines": lines})
    return min(lines, MAX_LOG_LINES)


def _aggregate_health(desired: int, ready: int, instances: list[InstanceInfo]) -> ServiceHealth:
    """Service health from instance health. Unknown when there is nothing to see."""
    if desired == 0 and not instances:
        return ServiceHealth.UNKNOWN
    if ready == 0:
        return ServiceHealth.CRITICAL
    if ready < desired or any(i.health is ServiceHealth.CRITICAL for i in instances):
        return ServiceHealth.DEGRADED
    if any(i.health is ServiceHealth.UNKNOWN for i in instances):
        return ServiceHealth.UNKNOWN
    return ServiceHealth.HEALTHY


def _version_of(image: str | None) -> str | None:
    """The tag of an image reference, ignoring a registry port's colon."""
    if not image:
        return None
    last = image.rsplit("/", 1)[-1]
    return last.rsplit(":", 1)[-1] if ":" in last else None


@dataclass
class _IdempotencyLedger:
    """Bounded in-process record of writes already applied.

    A courtesy guard, not the authority: Postgres is the system of record and
    the executor's own idempotency check is the one that counts. This exists so
    a retry storm inside one process cannot restart the same container twice
    while the durable record is still being written.
    """

    limit: int = 256
    _entries: OrderedDict[str, WriteResult] = field(default_factory=OrderedDict, init=False)

    def get(self, key: str) -> WriteResult | None:
        result = self._entries.get(key)
        if result is not None:
            self._entries.move_to_end(key)
        return result

    def put(self, key: str, result: WriteResult) -> None:
        self._entries[key] = result
        self._entries.move_to_end(key)
        while len(self._entries) > self.limit:
            self._entries.popitem(last=False)


# --------------------------------------------------------------------------- #
# the boundary                                                                 #
# --------------------------------------------------------------------------- #


class RuntimeAdapter(ABC):
    """The environment boundary. Implementations must be interchangeable.

    ``READ_METHODS`` is the surface the MCP layer may expose freely.
    ``WRITE_METHODS`` is the surface that must pass schema -> evidence -> policy
    -> authz -> lease -> execute -> verify before it is called.
    """

    name: ClassVar[str] = "abstract"

    READ_METHODS: ClassVar[frozenset[str]] = frozenset(
        {"list_services", "get_service", "list_instances", "get_logs", "health"}
    )
    WRITE_METHODS: ClassVar[frozenset[str]] = frozenset(
        {"restart_instance", "scale", "drain_instance", "rollback_deployment"}
    )

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._bulkhead = Bulkhead(f"runtime.{self.name}", limit=6)
        self._ledger = _IdempotencyLedger()

    # ---- capability ---------------------------------------------------------

    @property
    @abstractmethod
    def available(self) -> bool:
        """Whether this adapter has everything it needs to be used at all."""

    @property
    @abstractmethod
    def unavailable_reason(self) -> str:
        """Why not, in words an operator can act on. Empty when available."""

    async def ping(self) -> bool:
        """Cheap liveness probe for the integration-health surface."""
        return self.available

    async def close(self) -> None:
        """Release any client this adapter holds. Safe to call twice."""
        return

    # ---- reads --------------------------------------------------------------

    @abstractmethod
    async def list_services(self) -> list[ServiceState]:
        """Every service in the observed workload."""
        raise NotImplementedError

    @abstractmethod
    async def get_service(self, service_id: str) -> ServiceState:
        """One service. Raises NotFoundError when it does not exist."""
        raise NotImplementedError

    @abstractmethod
    async def list_instances(self, service_id: str) -> list[InstanceInfo]:
        """The instances backing a service. Empty is a valid answer."""
        raise NotImplementedError

    @abstractmethod
    async def get_logs(self, instance_id: str, lines: int = 200) -> LogChunk:
        """Tail one instance's logs. Bounded by ``MAX_LOG_LINES``."""
        raise NotImplementedError

    @abstractmethod
    async def health(self, service_id: str) -> ServiceHealth:
        """Normalised health. ``UNKNOWN`` when the backend cannot say."""
        raise NotImplementedError

    # ---- writes -------------------------------------------------------------

    @abstractmethod
    async def restart_instance(self, instance_id: str, *, idempotency_key: str) -> WriteResult:
        """WRITE. Restart one instance. Never auto-retried."""
        raise NotImplementedError

    @abstractmethod
    async def scale(
        self, service_id: str, replicas: int, *, idempotency_key: str
    ) -> WriteResult:
        """WRITE. Converge a service to ``replicas``. Never auto-retried."""
        raise NotImplementedError

    @abstractmethod
    async def drain_instance(self, instance_id: str, *, idempotency_key: str) -> WriteResult:
        """WRITE. Remove an instance from traffic, then stop it gracefully."""
        raise NotImplementedError

    @abstractmethod
    async def rollback_deployment(
        self, service_id: str, to_version: str, *, idempotency_key: str
    ) -> WriteResult:
        """WRITE. Move a service back to a known version. Never auto-retried."""
        raise NotImplementedError

    # ---- shared plumbing ----------------------------------------------------

    def _require_available(self, operation: str) -> None:
        if not self.available:
            raise SourceUnavailable(
                f"{self.name} adapter not configured: {self.unavailable_reason}",
                context={"adapter": self.name, "operation": operation},
            )

    async def _call(
        self,
        fn: Any,
        *,
        what: str,
        write: bool = False,
        timeout_s: float | None = None,
    ) -> Any:
        """Run a blocking backend SDK call off the event loop, guarded.

        The SDKs for Docker, Kubernetes and boto3 are synchronous, so the work
        goes to a thread. ``asyncio.wait_for`` cannot cancel that thread, which
        is why each client is *also* constructed with its own socket timeout -
        the deadline here bounds the caller, the client's own timeout bounds the
        thread.

        Writes get ``attempts=1`` and a non-retryable error: a write that may
        have partially applied must never be replayed by the transport layer.
        """

        async def _run() -> Any:
            return await asyncio.to_thread(fn)

        try:
            return await guarded_call(
                _run,
                dependency=f"runtime.{self.name}",
                timeout_s=timeout_s or (WRITE_TIMEOUT_S if write else READ_TIMEOUT_S),
                attempts=1 if write else 2,
                bulkhead=self._bulkhead,
            )
        except AegisError:
            raise
        except Exception as exc:
            if write:
                raise ExternalServiceError(
                    f"{self.name} write failed: {type(exc).__name__}",
                    context={"adapter": self.name, "operation": what, "error": str(exc)[:200]},
                    retryable=False,
                ) from exc
            raise SourceUnavailable(
                f"{self.name} unavailable: {type(exc).__name__}",
                context={"adapter": self.name, "operation": what},
            ) from exc

    def _ref(self, name: str) -> ServiceRef:
        """Canonical identity, identical across adapters by construction."""
        return ServiceRef.build(
            self._settings.aegis_environment_name, self._settings.workload_namespace, name
        )

    def _record(
        self,
        *,
        action: str,
        target: str,
        performed: str,
        idempotency_key: str,
        started_at: datetime,
        no_op: bool = False,
        detail: str = "",
        succeeded: bool = True,
    ) -> WriteResult:
        result = WriteResult(
            action=action,
            adapter=self.name,
            target=target,
            performed=performed,
            idempotency_key=idempotency_key,
            succeeded=succeeded,
            no_op=no_op,
            detail=detail,
            started_at=started_at,
            completed_at=_now(),
        )
        self._ledger.put(idempotency_key, result)
        log.info(
            "runtime write",
            adapter=self.name,
            action=action,
            target=target,
            performed=performed,
            no_op=no_op,
        )
        return result

    def _replayed(self, idempotency_key: str) -> WriteResult | None:
        """Return the previous result for a key already applied in this process."""
        if not idempotency_key:
            raise ValidationError("idempotency_key is required for every write")
        previous = self._ledger.get(idempotency_key)
        if previous is not None:
            log.info(
                "runtime write suppressed as duplicate",
                adapter=self.name,
                action=previous.action,
                idempotency_key=idempotency_key,
            )
        return previous


# --------------------------------------------------------------------------- #
# compose - the supported local reference environment                          #
# --------------------------------------------------------------------------- #


class ComposeAdapter(RuntimeAdapter):
    """Docker Compose via the Docker Engine API.

    Compose has no server-side control plane: ``docker compose`` is a CLI that
    drives the same Engine API this adapter uses. Aegis will not shell out, so
    scale and rollback are implemented as container-level convergence - clone a
    template container to scale up, recreate on a new image tag to roll back.
    Every container Aegis creates is labelled, so a scale-down can only ever
    reclaim its own replicas.
    """

    name: ClassVar[str] = "compose"

    def __init__(
        self,
        settings: Settings,
        *,
        project: str | None = None,
        client: Any | None = None,
    ) -> None:
        super().__init__(settings)
        self._project = project or os.environ.get(
            "COMPOSE_PROJECT_NAME", DEFAULT_COMPOSE_PROJECT
        )
        # An injected client is how tests drive this adapter without a daemon.
        self._client = client
        self._import_error = ""

    @property
    def project(self) -> str:
        return self._project

    @property
    def available(self) -> bool:
        if self._client is not None:
            return True
        try:
            import docker  # noqa: F401
        except ImportError as exc:
            self._import_error = str(exc)
            return False
        return bool(self._settings.sandbox_docker_host)

    @property
    def unavailable_reason(self) -> str:
        if self.available:
            return ""
        if self._import_error:
            return f"the docker package is not installed ({self._import_error})"
        return "sandbox_docker_host is not set"

    def _docker(self) -> Any:
        if self._client is None:
            import docker

            # The socket timeout bounds the worker thread; the deadline in
            # ``_call`` bounds the caller. Both are needed.
            self._client = docker.DockerClient(
                base_url=self._settings.sandbox_docker_host, timeout=int(READ_TIMEOUT_S)
            )
        return self._client

    async def ping(self) -> bool:
        if not self.available:
            return False
        try:
            return bool(await self._call(lambda: self._docker().ping(), what="ping"))
        except AegisError:
            return False

    async def close(self) -> None:
        client, self._client = self._client, None
        if client is not None and hasattr(client, "close"):
            await asyncio.to_thread(client.close)

    # ---- reads --------------------------------------------------------------

    def _list_containers(self, service: str | None = None) -> Any:
        labels = [f"{COMPOSE_PROJECT_LABEL}={self._project}"]
        if service is not None:
            labels.append(f"{COMPOSE_SERVICE_LABEL}={service}")
        return self._docker().containers.list(all=True, filters={"label": labels})

    @staticmethod
    def _container_health(container: Any) -> tuple[str, ServiceHealth]:
        """Map Docker's state onto normalised health.

        A container with a healthcheck reports it; one without only reports that
        it is running, which is weaker evidence but still an observation - so it
        maps to HEALTHY rather than UNKNOWN.
        """
        attrs = getattr(container, "attrs", {}) or {}
        state = _as_dict(attrs.get("State"))
        status = str(getattr(container, "status", "") or state.get("Status", "") or "unknown")
        health_block = _as_dict(state.get("Health"))
        docker_health = str(health_block.get("Status", "") or "")

        if status == "running":
            if docker_health == "unhealthy":
                return f"running ({docker_health})", ServiceHealth.CRITICAL
            if docker_health == "starting":
                return f"running ({docker_health})", ServiceHealth.DEGRADED
            return status, ServiceHealth.HEALTHY
        if status in ("exited", "dead"):
            return status, ServiceHealth.CRITICAL
        if status in ("created", "restarting", "paused", "removing"):
            return status, ServiceHealth.DEGRADED
        return status, ServiceHealth.UNKNOWN

    def _instance(self, container: Any, service: str) -> InstanceInfo:
        attrs = getattr(container, "attrs", {}) or {}
        state = _as_dict(attrs.get("State"))
        config = _as_dict(attrs.get("Config"))
        raw_status, health = self._container_health(container)
        image = str(config.get("Image", "") or "") or None
        started = state.get("StartedAt")
        started_at: datetime | None = None
        if isinstance(started, str) and started and not started.startswith("0001-"):
            try:
                started_at = datetime.fromisoformat(started.replace("Z", "+00:00"))
            except ValueError:
                started_at = None
        return InstanceInfo(
            instance_id=str(getattr(container, "id", ""))[:12],
            service_id=self._ref(service).service_id,
            name=str(getattr(container, "name", "")),
            raw_status=raw_status,
            health=health,
            image=image,
            version=_version_of(image),
            started_at=started_at,
            restart_count=int(attrs.get("RestartCount", 0) or 0),
            node=None,
        )

    def _states(self, containers: Any) -> list[ServiceState]:
        grouped: dict[str, list[InstanceInfo]] = {}
        for container in containers:
            labels = getattr(container, "labels", {}) or {}
            service = str(labels.get(COMPOSE_SERVICE_LABEL, "") or "")
            if not service:
                continue
            grouped.setdefault(service, []).append(self._instance(container, service))

        out: list[ServiceState] = []
        for service, instances in sorted(grouped.items()):
            ready = sum(1 for i in instances if i.health is ServiceHealth.HEALTHY)
            version = next((i.version for i in instances if i.version), None)
            out.append(
                ServiceState(
                    ref=self._ref(service),
                    health=_aggregate_health(len(instances), ready, instances),
                    version=version,
                    desired_instances=len(instances),
                    ready_instances=ready,
                )
            )
        return out

    async def list_services(self) -> list[ServiceState]:
        self._require_available("list_services")
        containers = await self._call(self._list_containers, what="list_services")
        return self._states(containers)

    async def get_service(self, service_id: str) -> ServiceState:
        self._require_available("get_service")
        service = service_name_of(service_id)
        containers = await self._call(
            lambda: self._list_containers(service), what="get_service"
        )
        states = self._states(containers)
        if not states:
            raise NotFoundError(
                "service not present in the compose project",
                context={"service": service, "project": self._project},
            )
        return states[0]

    async def list_instances(self, service_id: str) -> list[InstanceInfo]:
        self._require_available("list_instances")
        service = service_name_of(service_id)
        containers = await self._call(
            lambda: self._list_containers(service), what="list_instances"
        )
        return [self._instance(c, service) for c in containers]

    async def get_logs(self, instance_id: str, lines: int = 200) -> LogChunk:
        self._require_available("get_logs")
        instance_id = _check_instance_id(instance_id)
        tail = _check_log_lines(lines)

        def _fetch() -> bytes:
            container = self._docker().containers.get(instance_id)
            raw = container.logs(tail=tail, timestamps=True, stdout=True, stderr=True)
            return raw if isinstance(raw, bytes) else str(raw).encode("utf-8")

        raw = await self._call(_fetch, what="get_logs")
        decoded = raw.decode("utf-8", errors="replace").splitlines()
        return LogChunk(
            instance_id=instance_id,
            # Tier D: container stdout is whatever the process chose to print.
            lines=tuple(
                UntrustedText(text=line, origin="container_log") for line in decoded[-tail:]
            ),
            truncated=len(decoded) > tail,
        )

    async def health(self, service_id: str) -> ServiceHealth:
        self._require_available("health")
        try:
            return (await self.get_service(service_id)).health
        except NotFoundError:
            # A service the project does not contain is unknown, not unhealthy.
            return ServiceHealth.UNKNOWN

    # ---- writes -------------------------------------------------------------

    async def restart_instance(self, instance_id: str, *, idempotency_key: str) -> WriteResult:
        """WRITE. Restart one container.

        Restart converges on 'running', so replaying it is harmless - but it is
        still never auto-retried, because a retry that races the first attempt
        produces two restarts and a second outage window.
        """
        self._require_available("restart_instance")
        if (previous := self._replayed(idempotency_key)) is not None:
            return previous
        instance_id = _check_instance_id(instance_id)
        started = _now()

        def _restart() -> None:
            self._docker().containers.get(instance_id).restart(timeout=10)

        await self._call(_restart, what="restart_instance", write=True)
        return self._record(
            action="restart_instance",
            target=instance_id,
            performed=f"POST /containers/{instance_id}/restart?t=10",
            idempotency_key=idempotency_key,
            started_at=started,
            detail="container restarted with a 10s SIGTERM grace period",
        )

    async def scale(self, service_id: str, replicas: int, *, idempotency_key: str) -> WriteResult:
        """WRITE. Converge the container count for a compose service.

        Scaling up clones the configuration of a running container, because the
        Engine API has no notion of a compose service definition. Scaling down
        only ever removes containers Aegis itself created - the operator's
        original container is not Aegis's to reclaim.
        """
        self._require_available("scale")
        if (previous := self._replayed(idempotency_key)) is not None:
            return previous
        service = service_name_of(service_id)
        target = _check_replicas(replicas)
        started = _now()

        def _converge() -> tuple[str, str, bool]:
            client = self._docker()
            containers = [
                c
                for c in self._list_containers(service)
                if str(getattr(c, "status", "")) == "running"
            ]
            current = len(containers)
            if current == target:
                return (
                    f"GET /containers/json?label={COMPOSE_SERVICE_LABEL}={service}",
                    f"already running {current} replica(s)",
                    True,
                )
            if current == 0:
                raise ExternalServiceError(
                    "cannot scale a service with no running container to clone",
                    context={"service": service, "project": self._project},
                    retryable=False,
                )

            if target < current:
                managed = [
                    c
                    for c in containers
                    if str((getattr(c, "labels", {}) or {}).get(AEGIS_MANAGED_LABEL, ""))
                    == "true"
                ]
                removable = managed[: current - target]
                if len(removable) < current - target:
                    raise ExternalServiceError(
                        "refusing to remove containers Aegis did not create",
                        context={
                            "service": service,
                            "requested": target,
                            "aegis_managed": len(managed),
                            "running": current,
                        },
                        retryable=False,
                    )
                removed = []
                for container in removable:
                    container.stop(timeout=10)
                    container.remove(force=False)
                    removed.append(str(getattr(container, "id", ""))[:12])
                return (
                    f"POST /containers/{{id}}/stop + DELETE /containers/{{id}} x{len(removed)}",
                    f"removed aegis-managed replicas {', '.join(removed)}",
                    False,
                )

            template = containers[0]
            created = [
                self._clone(client, template, service, index)
                for index in range(current, target)
            ]
            return (
                f"POST /containers/create + /start x{len(created)}",
                f"created replicas {', '.join(created)}",
                False,
            )

        performed, detail, no_op = await self._call(_converge, what="scale", write=True)
        return self._record(
            action="scale",
            target=service,
            performed=performed,
            idempotency_key=idempotency_key,
            started_at=started,
            no_op=no_op,
            detail=detail,
        )

    def _clone(self, client: Any, template: Any, service: str, index: int) -> str:
        """Create one more container from a template container's configuration."""
        attrs = getattr(template, "attrs", {}) or {}
        config = _as_dict(attrs.get("Config"))
        attached = list((_as_dict(attrs.get("NetworkSettings")).get("Networks") or {}).keys())
        labels = dict(getattr(template, "labels", {}) or {})
        labels[AEGIS_MANAGED_LABEL] = "true"
        labels[COMPOSE_PROJECT_LABEL] = self._project
        labels[COMPOSE_SERVICE_LABEL] = service

        container = client.containers.run(
            config.get("Image"),
            detach=True,
            name=f"{self._project}-{service}-aegis-{index + 1}",
            environment=list(config.get("Env") or []),
            labels=labels,
            network=attached[0] if attached else None,
            # Aegis-created replicas are disposable; an always-restart policy
            # would outlive the incident that justified them.
            restart_policy={"Name": "no"},
        )
        return str(getattr(container, "id", ""))[:12]

    async def drain_instance(self, instance_id: str, *, idempotency_key: str) -> WriteResult:
        """WRITE. Take a container out of service DNS, then stop it gracefully.

        Disconnecting the networks first is what makes this a drain rather than
        a kill: compose resolves a service name through the network's DNS, so a
        disconnected container stops receiving new requests while the SIGTERM
        grace period lets it finish the ones it already has.
        """
        self._require_available("drain_instance")
        if (previous := self._replayed(idempotency_key)) is not None:
            return previous
        instance_id = _check_instance_id(instance_id)
        started = _now()

        def _drain() -> tuple[list[str], bool]:
            client = self._docker()
            container = client.containers.get(instance_id)
            if str(getattr(container, "status", "")) not in ("running", "restarting", "paused"):
                return [], True
            attrs = getattr(container, "attrs", {}) or {}
            names = list((_as_dict(attrs.get("NetworkSettings")).get("Networks") or {}).keys())
            for network_name in names:
                client.networks.get(network_name).disconnect(container)
            container.stop(timeout=30)
            return names, False

        networks, no_op = await self._call(_drain, what="drain_instance", write=True)
        detail = (
            "container was already stopped"
            if no_op
            else f"disconnected from {', '.join(networks) or 'no networks'}, then stopped"
        )
        return self._record(
            action="drain_instance",
            target=instance_id,
            performed=(
                f"POST /networks/{{name}}/disconnect x{len(networks)} then "
                f"POST /containers/{instance_id}/stop?t=30"
            ),
            idempotency_key=idempotency_key,
            started_at=started,
            no_op=no_op,
            detail=detail,
        )

    async def rollback_deployment(
        self, service_id: str, to_version: str, *, idempotency_key: str
    ) -> WriteResult:
        """WRITE. Recreate a service's containers on an earlier image tag.

        The Engine API cannot change a running container's image, so the
        replacement is started and confirmed running *before* the old container
        is stopped. Getting that order wrong turns a rollback into an outage.
        """
        self._require_available("rollback_deployment")
        if (previous := self._replayed(idempotency_key)) is not None:
            return previous
        service = service_name_of(service_id)
        version = _check_version(to_version)
        started = _now()

        def _rollback() -> tuple[str, bool, str]:
            client = self._docker()
            containers = [
                c
                for c in self._list_containers(service)
                if str(getattr(c, "status", "")) == "running"
            ]
            if not containers:
                raise NotFoundError(
                    "no running container for this service",
                    context={"service": service, "project": self._project},
                )

            replaced: list[str] = []
            for index, old in enumerate(containers):
                attrs = getattr(old, "attrs", {}) or {}
                config = _as_dict(attrs.get("Config"))
                image = str(config.get("Image", "") or "")
                repository = image.rsplit(":", 1)[0] if ":" in image.rsplit("/", 1)[-1] else image
                if _version_of(image) == version:
                    continue
                new_id = self._clone_with_image(
                    client, old, service, f"{repository}:{version}", index
                )
                old.stop(timeout=30)
                old.remove(force=False)
                replaced.append(f"{str(getattr(old, 'id', ''))[:12]}->{new_id}")

            if not replaced:
                return (
                    f"GET /containers/json?label={COMPOSE_SERVICE_LABEL}={service}",
                    True,
                    f"every container already runs version {version}",
                )
            return (
                f"POST /containers/create image=*:{version} + /start, then stop+remove old",
                False,
                f"replaced {', '.join(replaced)}",
            )

        performed, no_op, detail = await self._call(
            _rollback, what="rollback_deployment", write=True
        )
        return self._record(
            action="rollback_deployment",
            target=service,
            performed=performed,
            idempotency_key=idempotency_key,
            started_at=started,
            no_op=no_op,
            detail=detail,
        )

    def _clone_with_image(
        self, client: Any, template: Any, service: str, image: str, index: int
    ) -> str:
        attrs = getattr(template, "attrs", {}) or {}
        config = _as_dict(attrs.get("Config"))
        attached = list((_as_dict(attrs.get("NetworkSettings")).get("Networks") or {}).keys())
        labels = dict(getattr(template, "labels", {}) or {})
        labels[AEGIS_MANAGED_LABEL] = "true"
        container = client.containers.run(
            image,
            detach=True,
            name=f"{self._project}-{service}-rollback-{index + 1}",
            environment=list(config.get("Env") or []),
            labels=labels,
            network=attached[0] if attached else None,
            restart_policy={"Name": "unless-stopped"},
        )
        return str(getattr(container, "id", ""))[:12]


# --------------------------------------------------------------------------- #
# kubernetes                                                                   #
# --------------------------------------------------------------------------- #


class KubernetesAdapter(RuntimeAdapter):
    """Kubernetes via the official python client.

    The ``kubernetes`` package is an optional extra: Aegis's supported local
    environment is Compose, and a cluster install adds it. When the package or a
    usable kubeconfig is missing this adapter is honestly unavailable - it never
    returns an empty service list, which would read as "the cluster is empty".
    """

    name: ClassVar[str] = "kubernetes"

    def __init__(self, settings: Settings, *, apis: tuple[Any, Any] | None = None) -> None:
        super().__init__(settings)
        self._apis = apis
        self._reason = ""

    @property
    def namespace(self) -> str:
        return self._settings.workload_namespace

    @property
    def available(self) -> bool:
        if self._apis is not None:
            return True
        try:
            import kubernetes  # noqa: F401
        except ImportError:
            self._reason = "the kubernetes package is not installed"
            return False
        if not self._kubeconfig_present():
            self._reason = "no kubeconfig and no in-cluster service account"
            return False
        return True

    def _kubeconfig_present(self) -> bool:
        explicit = os.environ.get("KUBECONFIG")
        if explicit and os.path.exists(explicit):
            return True
        default = os.path.join(os.path.expanduser("~"), ".kube", "config")
        in_cluster = "/var/run/secrets/kubernetes.io/serviceaccount/token"
        return os.path.exists(default) or os.path.exists(in_cluster)

    @property
    def unavailable_reason(self) -> str:
        if self.available:
            return ""
        return self._reason or "kubernetes is not configured"

    def _load(self) -> tuple[Any, Any]:
        if self._apis is None:
            try:
                from kubernetes import client as k8s_client
                from kubernetes import config as k8s_config
            except ImportError as exc:
                raise ConfigError(
                    "kubernetes adapter not configured: the kubernetes package is not installed",
                    context={"adapter": self.name},
                ) from exc
            try:
                k8s_config.load_kube_config(context=self._settings.kube_context)
            except Exception:  # noqa: BLE001 - fall through to in-cluster below
                try:
                    k8s_config.load_incluster_config()
                except Exception as exc:  # noqa: BLE001
                    raise ConfigError(
                        "kubernetes adapter not configured: no usable kubeconfig "
                        f"for context {self._settings.kube_context!r}",
                        context={"adapter": self.name, "error": str(exc)[:200]},
                    ) from exc
            self._apis = (k8s_client.CoreV1Api(), k8s_client.AppsV1Api())
        return self._apis

    def _core(self) -> Any:
        return self._load()[0]

    def _apps(self) -> Any:
        return self._load()[1]

    async def ping(self) -> bool:
        if not self.available:
            return False
        try:
            await self._call(
                lambda: self._core().list_namespaced_pod(self.namespace, limit=1), what="ping"
            )
        except AegisError:
            return False
        return True

    # ---- reads --------------------------------------------------------------

    def _deployment_state(self, deployment: Any) -> ServiceState:
        spec, status = deployment.spec, deployment.status
        desired = int(getattr(spec, "replicas", 0) or 0)
        ready = int(getattr(status, "ready_replicas", 0) or 0)
        containers = getattr(getattr(spec.template, "spec", None), "containers", []) or []
        image = str(getattr(containers[0], "image", "")) if containers else ""
        health = (
            ServiceHealth.HEALTHY
            if desired and ready == desired
            else ServiceHealth.CRITICAL
            if ready == 0
            else ServiceHealth.DEGRADED
        )
        return ServiceState(
            ref=self._ref(str(deployment.metadata.name)),
            health=health,
            version=_version_of(image),
            desired_instances=desired,
            ready_instances=ready,
        )

    async def list_services(self) -> list[ServiceState]:
        self._require_available("list_services")
        result = await self._call(
            lambda: self._apps().list_namespaced_deployment(self.namespace),
            what="list_services",
        )
        return [self._deployment_state(d) for d in getattr(result, "items", [])]

    async def get_service(self, service_id: str) -> ServiceState:
        self._require_available("get_service")
        service = service_name_of(service_id)
        try:
            deployment = await self._call(
                lambda: self._apps().read_namespaced_deployment(service, self.namespace),
                what="get_service",
            )
        except AegisError as exc:
            raise NotFoundError(
                "deployment not found",
                context={"service": service, "namespace": self.namespace},
            ) from exc
        return self._deployment_state(deployment)

    async def list_instances(self, service_id: str) -> list[InstanceInfo]:
        self._require_available("list_instances")
        service = service_name_of(service_id)
        result = await self._call(
            lambda: self._core().list_namespaced_pod(
                self.namespace, label_selector=f"app={service}"
            ),
            what="list_instances",
        )
        out: list[InstanceInfo] = []
        for pod in getattr(result, "items", []):
            status = pod.status
            phase = str(getattr(status, "phase", "") or "")
            statuses = getattr(status, "container_statuses", None) or []
            ready = bool(statuses) and all(bool(getattr(s, "ready", False)) for s in statuses)
            restarts = sum(int(getattr(s, "restart_count", 0) or 0) for s in statuses)
            waiting_reason = ""
            for container_status in statuses:
                waiting = getattr(getattr(container_status, "state", None), "waiting", None)
                if waiting is not None and getattr(waiting, "reason", None):
                    waiting_reason = str(waiting.reason)
                    break
            containers = getattr(pod.spec, "containers", []) or []
            image = str(getattr(containers[0], "image", "")) if containers else ""
            health = (
                ServiceHealth.HEALTHY
                if phase == "Running" and ready
                else ServiceHealth.CRITICAL
                if phase in ("Failed", "Unknown") or waiting_reason == "CrashLoopBackOff"
                else ServiceHealth.DEGRADED
            )
            out.append(
                InstanceInfo(
                    instance_id=str(pod.metadata.name),
                    service_id=self._ref(service).service_id,
                    name=str(pod.metadata.name),
                    raw_status=waiting_reason or phase,
                    health=health,
                    image=image or None,
                    version=_version_of(image),
                    started_at=getattr(status, "start_time", None),
                    restart_count=restarts,
                    node=str(getattr(pod.spec, "node_name", "") or "") or None,
                )
            )
        return out

    async def get_logs(self, instance_id: str, lines: int = 200) -> LogChunk:
        self._require_available("get_logs")
        pod = _check_instance_id(instance_id)
        tail = _check_log_lines(lines)
        raw = await self._call(
            lambda: self._core().read_namespaced_pod_log(
                pod, self.namespace, tail_lines=tail, timestamps=True
            ),
            what="get_logs",
        )
        decoded = str(raw or "").splitlines()
        return LogChunk(
            instance_id=pod,
            lines=tuple(UntrustedText(text=line, origin="pod_log") for line in decoded[-tail:]),
            truncated=len(decoded) > tail,
        )

    async def health(self, service_id: str) -> ServiceHealth:
        self._require_available("health")
        try:
            return (await self.get_service(service_id)).health
        except NotFoundError:
            return ServiceHealth.UNKNOWN

    # ---- writes -------------------------------------------------------------

    async def restart_instance(self, instance_id: str, *, idempotency_key: str) -> WriteResult:
        """WRITE. Delete the pod and let its controller recreate it."""
        self._require_available("restart_instance")
        if (previous := self._replayed(idempotency_key)) is not None:
            return previous
        pod = _check_instance_id(instance_id)
        started = _now()
        await self._call(
            lambda: self._core().delete_namespaced_pod(
                pod, self.namespace, grace_period_seconds=30
            ),
            what="restart_instance",
            write=True,
        )
        return self._record(
            action="restart_instance",
            target=pod,
            performed=(
                f"DELETE /api/v1/namespaces/{self.namespace}/pods/{pod}"
                "?gracePeriodSeconds=30"
            ),
            idempotency_key=idempotency_key,
            started_at=started,
            detail="pod deleted; its controller recreates it",
        )

    async def scale(self, service_id: str, replicas: int, *, idempotency_key: str) -> WriteResult:
        """WRITE. Patch the deployment's scale subresource.

        Naturally idempotent - the patch declares the desired state, so applying
        it twice leaves the same number of replicas.
        """
        self._require_available("scale")
        if (previous := self._replayed(idempotency_key)) is not None:
            return previous
        service = service_name_of(service_id)
        target = _check_replicas(replicas)
        started = _now()
        await self._call(
            lambda: self._apps().patch_namespaced_deployment_scale(
                service, self.namespace, {"spec": {"replicas": target}}
            ),
            what="scale",
            write=True,
        )
        return self._record(
            action="scale",
            target=service,
            performed=(
                f"PATCH /apis/apps/v1/namespaces/{self.namespace}/deployments/"
                f"{service}/scale spec.replicas={target}"
            ),
            idempotency_key=idempotency_key,
            started_at=started,
            detail=f"desired replicas set to {target}",
        )

    async def drain_instance(self, instance_id: str, *, idempotency_key: str) -> WriteResult:
        """WRITE. Orphan the pod from its Service, then delete it gracefully.

        Removing the ``app`` label drops the pod out of the Service's endpoints
        immediately, so it stops receiving traffic while the grace period lets
        in-flight requests finish. It also tells the ReplicaSet the pod is gone,
        which brings a replacement up before this one dies.
        """
        self._require_available("drain_instance")
        if (previous := self._replayed(idempotency_key)) is not None:
            return previous
        pod = _check_instance_id(instance_id)
        started = _now()

        def _drain() -> None:
            core = self._core()
            core.patch_namespaced_pod(
                pod,
                self.namespace,
                {"metadata": {"labels": {"app": None, "aegis.io/draining": "true"}}},
            )
            core.delete_namespaced_pod(pod, self.namespace, grace_period_seconds=60)

        await self._call(_drain, what="drain_instance", write=True)
        return self._record(
            action="drain_instance",
            target=pod,
            performed=(
                f"PATCH /api/v1/namespaces/{self.namespace}/pods/{pod} labels.app=null then "
                f"DELETE the same pod ?gracePeriodSeconds=60"
            ),
            idempotency_key=idempotency_key,
            started_at=started,
            detail="pod removed from Service endpoints, then deleted gracefully",
        )

    async def rollback_deployment(
        self, service_id: str, to_version: str, *, idempotency_key: str
    ) -> WriteResult:
        """WRITE. Patch the deployment's container image back to a known tag."""
        self._require_available("rollback_deployment")
        if (previous := self._replayed(idempotency_key)) is not None:
            return previous
        service = service_name_of(service_id)
        version = _check_version(to_version)
        started = _now()

        def _rollback() -> tuple[str, bool]:
            apps = self._apps()
            deployment = apps.read_namespaced_deployment(service, self.namespace)
            containers = getattr(deployment.spec.template.spec, "containers", []) or []
            if not containers:
                raise NotFoundError(
                    "deployment declares no container",
                    context={"service": service, "namespace": self.namespace},
                )
            current = str(getattr(containers[0], "image", ""))
            if _version_of(current) == version:
                return current, True
            repository = (
                current.rsplit(":", 1)[0] if ":" in current.rsplit("/", 1)[-1] else current
            )
            image = f"{repository}:{version}"
            apps.patch_namespaced_deployment(
                service,
                self.namespace,
                {
                    "spec": {
                        "template": {
                            "spec": {
                                "containers": [
                                    {"name": containers[0].name, "image": image}
                                ]
                            }
                        }
                    }
                },
            )
            return image, False

        image, no_op = await self._call(_rollback, what="rollback_deployment", write=True)
        return self._record(
            action="rollback_deployment",
            target=service,
            performed=(
                f"PATCH /apis/apps/v1/namespaces/{self.namespace}/deployments/{service} "
                f"image={image}"
            ),
            idempotency_key=idempotency_key,
            started_at=started,
            no_op=no_op,
            detail=f"already at {image}" if no_op else f"rolled forward-record to {image}",
        )


# --------------------------------------------------------------------------- #
# ecs                                                                          #
# --------------------------------------------------------------------------- #


class EcsAdapter(RuntimeAdapter):
    """AWS ECS via boto3. Unavailable unless boto3 is installed and a cluster set."""

    name: ClassVar[str] = "ecs"

    def __init__(self, settings: Settings, *, clients: dict[str, Any] | None = None) -> None:
        super().__init__(settings)
        self._clients = clients
        self._reason = ""

    @property
    def cluster(self) -> str:
        return self._settings.ecs_cluster

    @property
    def available(self) -> bool:
        if self._clients is not None:
            return True
        if not self.cluster:
            self._reason = "ecs_cluster is not set"
            return False
        try:
            import boto3  # noqa: F401
        except ImportError:
            self._reason = "the boto3 package is not installed"
            return False
        return True

    @property
    def unavailable_reason(self) -> str:
        if self.available:
            return ""
        return self._reason or "ecs is not configured"

    def _aws(self, service: str) -> Any:
        if self._clients is None:
            try:
                import boto3
            except ImportError as exc:
                raise ConfigError(
                    "ecs adapter not configured: the boto3 package is not installed",
                    context={"adapter": self.name},
                ) from exc
            session = boto3.session.Session(region_name=self._settings.aws_region)
            self._clients = {
                "ecs": session.client("ecs"),
                "logs": session.client("logs"),
            }
        if service not in self._clients:
            raise ConfigError(
                f"ecs adapter has no {service} client", context={"adapter": self.name}
            )
        return self._clients[service]

    async def ping(self) -> bool:
        if not self.available:
            return False
        try:
            await self._call(
                lambda: self._aws("ecs").describe_clusters(clusters=[self.cluster]), what="ping"
            )
        except AegisError:
            return False
        return True

    # ---- reads --------------------------------------------------------------

    def _service_state(self, described: dict[str, Any]) -> ServiceState:
        desired = int(described.get("desiredCount", 0) or 0)
        running = int(described.get("runningCount", 0) or 0)
        task_def = str(described.get("taskDefinition", ""))
        return ServiceState(
            ref=self._ref(str(described.get("serviceName", ""))),
            health=_aggregate_health(desired, running, []),
            version=task_def.rsplit(":", 1)[-1] if ":" in task_def else None,
            desired_instances=desired,
            ready_instances=running,
        )

    async def list_services(self) -> list[ServiceState]:
        self._require_available("list_services")

        def _fetch() -> list[dict[str, Any]]:
            ecs = self._aws("ecs")
            arns = ecs.list_services(cluster=self.cluster, maxResults=100).get(
                "serviceArns", []
            )
            if not arns:
                return []
            # describe_services accepts ten at a time; the page size above keeps
            # this loop bounded at ten calls.
            described: list[dict[str, Any]] = []
            for offset in range(0, len(arns), 10):
                described.extend(
                    ecs.describe_services(
                        cluster=self.cluster, services=arns[offset : offset + 10]
                    ).get("services", [])
                )
            return described

        return [self._service_state(s) for s in await self._call(_fetch, what="list_services")]

    async def get_service(self, service_id: str) -> ServiceState:
        self._require_available("get_service")
        service = service_name_of(service_id)
        described = await self._call(
            lambda: self._aws("ecs").describe_services(
                cluster=self.cluster, services=[service]
            ),
            what="get_service",
        )
        services = described.get("services", []) if isinstance(described, dict) else []
        if not services:
            raise NotFoundError(
                "ecs service not found", context={"service": service, "cluster": self.cluster}
            )
        return self._service_state(services[0])

    async def list_instances(self, service_id: str) -> list[InstanceInfo]:
        self._require_available("list_instances")
        service = service_name_of(service_id)

        def _fetch() -> list[dict[str, Any]]:
            ecs = self._aws("ecs")
            arns = ecs.list_tasks(
                cluster=self.cluster, serviceName=service, maxResults=100
            ).get("taskArns", [])
            if not arns:
                return []
            return list(ecs.describe_tasks(cluster=self.cluster, tasks=arns).get("tasks", []))

        out: list[InstanceInfo] = []
        for task in await self._call(_fetch, what="list_instances"):
            last_status = str(task.get("lastStatus", ""))
            health_status = str(task.get("healthStatus", "UNKNOWN"))
            containers = task.get("containers", []) or []
            image = str(containers[0].get("image", "")) if containers else ""
            health = (
                ServiceHealth.HEALTHY
                if last_status == "RUNNING" and health_status in ("HEALTHY", "UNKNOWN")
                else ServiceHealth.CRITICAL
                if last_status in ("STOPPED", "DEPROVISIONING")
                else ServiceHealth.DEGRADED
            )
            out.append(
                InstanceInfo(
                    instance_id=str(task.get("taskArn", "")),
                    service_id=self._ref(service).service_id,
                    name=str(task.get("taskArn", "")).rsplit("/", 1)[-1],
                    raw_status=last_status,
                    health=health,
                    image=image or None,
                    version=_version_of(image),
                    started_at=task.get("startedAt"),
                    restart_count=0,
                    node=str(task.get("containerInstanceArn", "") or "") or None,
                )
            )
        return out

    async def get_logs(self, instance_id: str, lines: int = 200) -> LogChunk:
        """Read the task's CloudWatch stream.

        ECS has no log API of its own, so the awslogs configuration on the task
        definition is what makes this possible. A task configured for any other
        log driver is an explicit gap, not an empty log.
        """
        self._require_available("get_logs")
        task_arn = _check_instance_id(instance_id)
        tail = _check_log_lines(lines)

        def _fetch() -> list[dict[str, Any]]:
            ecs, logs = self._aws("ecs"), self._aws("logs")
            tasks = ecs.describe_tasks(cluster=self.cluster, tasks=[task_arn]).get("tasks", [])
            if not tasks:
                raise NotFoundError("ecs task not found", context={"task": task_arn[:96]})
            task = tasks[0]
            definition = ecs.describe_task_definition(
                taskDefinition=task["taskDefinitionArn"]
            )["taskDefinition"]
            containers = definition.get("containerDefinitions", []) or []
            if not containers:
                raise NotFoundError(
                    "task definition declares no container", context={"task": task_arn[:96]}
                )
            config = containers[0].get("logConfiguration") or {}
            if config.get("logDriver") != "awslogs":
                raise SourceUnavailable(
                    "task does not use the awslogs driver, so logs are not readable here",
                    context={"task": task_arn[:96], "driver": str(config.get("logDriver", ""))},
                )
            options = config.get("options", {})
            stream = "/".join(
                [
                    options.get("awslogs-stream-prefix", ""),
                    str(containers[0].get("name", "")),
                    task_arn.rsplit("/", 1)[-1],
                ]
            )
            return list(
                logs.get_log_events(
                    logGroupName=options["awslogs-group"],
                    logStreamName=stream,
                    limit=tail,
                    startFromHead=False,
                ).get("events", [])
            )

        events = await self._call(_fetch, what="get_logs")
        return LogChunk(
            instance_id=task_arn,
            lines=tuple(
                UntrustedText(text=str(e.get("message", "")), origin="ecs_log")
                for e in events[-tail:]
            ),
            truncated=len(events) > tail,
        )

    async def health(self, service_id: str) -> ServiceHealth:
        self._require_available("health")
        try:
            return (await self.get_service(service_id)).health
        except NotFoundError:
            return ServiceHealth.UNKNOWN

    # ---- writes -------------------------------------------------------------

    async def restart_instance(self, instance_id: str, *, idempotency_key: str) -> WriteResult:
        """WRITE. Stop the task; the service scheduler replaces it."""
        self._require_available("restart_instance")
        if (previous := self._replayed(idempotency_key)) is not None:
            return previous
        task_arn = _check_instance_id(instance_id)
        started = _now()
        await self._call(
            lambda: self._aws("ecs").stop_task(
                cluster=self.cluster, task=task_arn, reason="aegis restart_instance"
            ),
            what="restart_instance",
            write=True,
        )
        return self._record(
            action="restart_instance",
            target=task_arn,
            performed=f"ecs:StopTask cluster={self.cluster} task={task_arn}",
            idempotency_key=idempotency_key,
            started_at=started,
            detail="task stopped; the service scheduler starts a replacement",
        )

    async def scale(self, service_id: str, replicas: int, *, idempotency_key: str) -> WriteResult:
        """WRITE. Set desiredCount. Declarative, so replay is harmless."""
        self._require_available("scale")
        if (previous := self._replayed(idempotency_key)) is not None:
            return previous
        service = service_name_of(service_id)
        target = _check_replicas(replicas)
        started = _now()
        await self._call(
            lambda: self._aws("ecs").update_service(
                cluster=self.cluster, service=service, desiredCount=target
            ),
            what="scale",
            write=True,
        )
        return self._record(
            action="scale",
            target=service,
            performed=(
                f"ecs:UpdateService cluster={self.cluster} service={service} "
                f"desiredCount={target}"
            ),
            idempotency_key=idempotency_key,
            started_at=started,
            detail=f"desired count set to {target}",
        )

    async def drain_instance(self, instance_id: str, *, idempotency_key: str) -> WriteResult:
        """WRITE. Stop the task so its target group deregistration delay drains it."""
        self._require_available("drain_instance")
        if (previous := self._replayed(idempotency_key)) is not None:
            return previous
        task_arn = _check_instance_id(instance_id)
        started = _now()
        await self._call(
            lambda: self._aws("ecs").stop_task(
                cluster=self.cluster, task=task_arn, reason="aegis drain_instance"
            ),
            what="drain_instance",
            write=True,
        )
        return self._record(
            action="drain_instance",
            target=task_arn,
            performed=f"ecs:StopTask cluster={self.cluster} task={task_arn} (drain)",
            idempotency_key=idempotency_key,
            started_at=started,
            detail="task stopped; the load balancer drains it over its deregistration delay",
        )

    async def rollback_deployment(
        self, service_id: str, to_version: str, *, idempotency_key: str
    ) -> WriteResult:
        """WRITE. Point the service at an earlier task definition revision."""
        self._require_available("rollback_deployment")
        if (previous := self._replayed(idempotency_key)) is not None:
            return previous
        service = service_name_of(service_id)
        version = _check_version(to_version)
        started = _now()

        def _rollback() -> tuple[str, bool]:
            ecs = self._aws("ecs")
            described = ecs.describe_services(cluster=self.cluster, services=[service])
            services = described.get("services", [])
            if not services:
                raise NotFoundError(
                    "ecs service not found",
                    context={"service": service, "cluster": self.cluster},
                )
            current = str(services[0].get("taskDefinition", ""))
            family = current.rsplit(":", 1)[0] if ":" in current else current
            target = f"{family.rsplit('/', 1)[-1]}:{version}"
            if current.endswith(f":{version}"):
                return target, True
            ecs.update_service(
                cluster=self.cluster, service=service, taskDefinition=target
            )
            return target, False

        target, no_op = await self._call(_rollback, what="rollback_deployment", write=True)
        return self._record(
            action="rollback_deployment",
            target=service,
            performed=(
                f"ecs:UpdateService cluster={self.cluster} service={service} "
                f"taskDefinition={target}"
            ),
            idempotency_key=idempotency_key,
            started_at=started,
            no_op=no_op,
            detail=f"already on {target}" if no_op else f"task definition set to {target}",
        )


# --------------------------------------------------------------------------- #
# factory                                                                      #
# --------------------------------------------------------------------------- #

_ADAPTERS: dict[str, type[RuntimeAdapter]] = {
    ComposeAdapter.name: ComposeAdapter,
    KubernetesAdapter.name: KubernetesAdapter,
    EcsAdapter.name: EcsAdapter,
}


def get_adapter(settings: Settings) -> RuntimeAdapter:
    """Select the adapter named by ``settings.workload_adapter``.

    Fails closed. Pydantic already constrains the setting, but this function is
    also reachable from a reloaded config or a test double, and an unrecognised
    environment must never silently fall back to Compose - that would point
    write actions at the wrong system.
    """
    raw = str(getattr(settings, "workload_adapter", "") or "")
    adapter_cls = _ADAPTERS.get(raw)
    if adapter_cls is None:
        raise ConfigError(
            f"unknown workload_adapter {raw!r}",
            context={"known": sorted(_ADAPTERS)},
        )
    return adapter_cls(settings)


__all__ = [
    "AEGIS_MANAGED_LABEL",
    "DEFAULT_COMPOSE_PROJECT",
    "MAX_LOG_LINES",
    "MAX_REPLICAS",
    "ComposeAdapter",
    "EcsAdapter",
    "InstanceInfo",
    "KubernetesAdapter",
    "LogChunk",
    "RuntimeAdapter",
    "WriteResult",
    "get_adapter",
    "service_name_of",
]
