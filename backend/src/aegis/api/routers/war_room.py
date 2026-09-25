"""The war room: one page's worth of live horizon state, plus demo controls.

Reads come from the horizon store (Postgres), the incident repository and
Prometheus. Nothing here decides anything; the page renders what the worker
recorded, labelled with where each number came from.

The three POST controls - inject, reset, kill-worker - are fault injection on
the local reference workload and exist for demonstration. They are refused
outside ``AEGIS_ENV=local`` whatever the caller's role, and inject/reset are
recorded in ``deployment_attempts`` as *operator* deployments, never as agent
actions: the agent's own writes only ever go through the gate chain.
"""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime
from typing import Annotated, Any, Final, Literal, Protocol

import httpx
from fastapi import APIRouter, Depends, Header, Query, Request, Response
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

from aegis.agents.horizon.ports import HorizonStore
from aegis.api.deps import RequireAdmin, RequireResponder, RequireViewer, settings_dep
from aegis.api.security import Principal
from aegis.api.war_room_events import parse_last_event_id, publish_control, war_room_stream
from aegis.core.config import Environment, Settings
from aegis.core.errors import (
    AegisError,
    AuthorizationError,
    NotFoundError,
    SourceUnavailable,
)
from aegis.core.logging import get_logger
from aegis.core.resilience import guarded_call
from aegis.domain.horizon import HorizonEvent, HorizonEventType, HorizonState, MemoryCard
from aegis.domain.models import ServiceRef
from aegis.persistence.patches import DeploymentState
from aegis.telemetry.prometheus import MetricSeries, escape_label

log = get_logger(__name__)
router = APIRouter(prefix="/war-room", tags=["war-room"])

SettingsDep = Annotated[Settings, Depends(settings_dep)]

SERVICES: Final = ("gateway", "checkout", "payment")
POOL_CARD: Final = "db-pool"
SCENARIO_SERVICE: Final = "checkout"
HEALTHY_VERSION: Final = "1.4.1"
BAD_VERSION: Final = "1.4.2"

# The same thresholds verification uses; the card colours must agree with the
# verdict the agent is held to.
P99_OK_MS: Final = 250.0
ERROR_OK: Final = 0.02
POOL_OK: Final = 0.8
P99_CRITICAL_MS: Final = 1000.0
ERROR_CRITICAL: Final = 0.10
POOL_CRITICAL: Final = 0.95

HEALTH_WINDOW_S: Final = 300
HEALTH_STEP_S: Final = 15.0
HEALTH_DEADLINE_S: Final = 6.0
HEALTH_CACHE_S: Final = 4.0
SERIES_POINTS: Final = 20
ADMIN_TIMEOUT_S: Final = 5.0


class WarRoomStore(HorizonStore, Protocol):
    """``HorizonStore`` plus the reads only the war room needs."""

    async def latest_run(self) -> HorizonState | None: ...

    async def mark_idle(self, run_id: str) -> bool: ...

    async def context_series(self, incident_id: str) -> list[dict[str, int]]: ...

    async def events_of_type(
        self, incident_id: str, event_type: HorizonEventType, *, limit: int = 100
    ) -> list[tuple[int, HorizonEvent]]: ...

    async def last_seq(self, incident_id: str | None = None) -> int: ...

    async def get_incident_map(self, card_id: str) -> tuple[bytes, str] | None: ...


# --------------------------------------------------------------------------- #
# dependencies                                                                 #
# --------------------------------------------------------------------------- #


def store_dep(request: Request) -> WarRoomStore:
    store = getattr(request.app.state, "horizon_store", None)
    if store is None:
        raise SourceUnavailable(
            "the horizon store is not available in this process",
            context={"capability": "horizon_store"},
        )
    return store  # type: ignore[no-any-return]


StoreDep = Annotated[WarRoomStore, Depends(store_dep)]


def _container(request: Request) -> Any:
    return getattr(request.app.state, "container", None)


def _redis(request: Request) -> Any:
    return getattr(request.app.state, "redis", None)


def _require_local(settings: Settings, control: str) -> None:
    """Fault injection is a local-environment control, fail closed elsewhere."""
    if settings.aegis_env is not Environment.LOCAL:
        raise AuthorizationError(
            f"{control} is only available in the local environment",
            context={"environment": settings.aegis_env.value},
        )


# --------------------------------------------------------------------------- #
# health                                                                       #
# --------------------------------------------------------------------------- #


def _status(p99: float | None, err: float | None, pool: float | None) -> str:
    values = (p99, err, pool)
    if all(v is None for v in values):
        return "unknown"
    if (
        (p99 is not None and p99 >= P99_CRITICAL_MS)
        or (err is not None and err >= ERROR_CRITICAL)
        or (pool is not None and pool >= POOL_CRITICAL)
    ):
        return "critical"
    if (
        (p99 is not None and p99 >= P99_OK_MS)
        or (err is not None and err >= ERROR_OK)
        or (pool is not None and pool >= POOL_OK)
    ):
        return "degraded"
    return "healthy"


def _values(series: list[MetricSeries], scale: float = 1.0) -> list[float]:
    if not series:
        return []
    return [round(p.value * scale, 4) for p in series[0].points[-SERIES_POINTS:]]


class _Measured:
    """One metric: its series, or ``None`` when Prometheus could not be asked."""

    __slots__ = ("available", "series")

    def __init__(self, series: list[float] | None) -> None:
        self.available = series is not None
        self.series = series or []

    @property
    def latest(self) -> float | None:
        return self.series[-1] if self.series else None


class HealthProbe:
    """Builds the /health payload, shared by every SSE client via a short cache.

    Without the cache, N open war-room tabs would each run ten range queries
    every five seconds against the same Prometheus the heartbeat relies on.
    """

    def __init__(self, prometheus: Any, runtime: Any) -> None:
        self._prometheus = prometheus
        self._runtime = runtime
        self._lock = asyncio.Lock()
        self._cached: dict[str, Any] | None = None
        self._cached_at = 0.0

    async def payload(self) -> dict[str, Any]:
        async with self._lock:
            now = time.monotonic()
            if self._cached is not None and now - self._cached_at < HEALTH_CACHE_S:
                return self._cached
            try:
                payload = await asyncio.wait_for(self._build(), timeout=HEALTH_DEADLINE_S)
            except TimeoutError:
                payload = self._unavailable()
            self._cached, self._cached_at = payload, time.monotonic()
            return payload

    async def _measure(self, call: Any, scale: float = 1.0) -> _Measured:
        if self._prometheus is None:
            return _Measured(None)
        try:
            series: list[MetricSeries] = await call
        except AegisError as exc:
            # "Could not ask" stays distinct from "asked and found nothing".
            log.debug("war-room health query unavailable", error=exc.code)
            return _Measured(None)
        return _Measured(_values(series, scale))

    async def _versions(self) -> dict[str, str | None]:
        if self._runtime is None or not getattr(self._runtime, "available", False):
            return {}
        try:
            states = await asyncio.wait_for(self._runtime.list_services(), timeout=3.0)
        except (AegisError, TimeoutError) as exc:
            log.debug("war-room version lookup failed", error=type(exc).__name__)
            return {}
        return {s.ref.name: s.version for s in states}

    def _pool_query(self) -> str:
        names = "|".join(escape_label(s) for s in SERVICES)
        return (
            f'max(sum by (service) (connection_pool_in_use{{service=~"{names}"}})'
            f' / clamp_min(sum by (service) (connection_pool_size{{service=~"{names}"}}), 1))'
        )

    async def _build(self) -> dict[str, Any]:
        prom = self._prometheus
        if prom is None:
            return self._unavailable()
        calls: list[Any] = []
        for svc in SERVICES:
            calls.append(self._measure(prom.latency_p99(svc, HEALTH_WINDOW_S), 1000.0))
            calls.append(self._measure(prom.error_rate(svc, HEALTH_WINDOW_S)))
            calls.append(self._measure(prom.pool_saturation(svc, HEALTH_WINDOW_S)))
        now = time.time()
        calls.append(
            self._measure(
                prom.query_range(
                    self._pool_query(), start=now - HEALTH_WINDOW_S, end=now, step=HEALTH_STEP_S
                )
            )
        )
        measured, versions = await asyncio.gather(asyncio.gather(*calls), self._versions())

        services: list[dict[str, Any]] = []
        for index, svc in enumerate(SERVICES):
            p99, err, pool = measured[index * 3 : index * 3 + 3]
            services.append(self._card(svc, p99, err, pool, versions.get(svc)))
        services.append(
            self._card(POOL_CARD, _Measured([]), _Measured([]), measured[-1], None, pool_only=True)
        )
        return {"ts": datetime.now(UTC).isoformat(), "services": services}

    @staticmethod
    def _card(
        service: str,
        p99: _Measured,
        err: _Measured,
        pool: _Measured,
        version: str | None,
        *,
        pool_only: bool = False,
    ) -> dict[str, Any]:
        reached = pool.available if pool_only else (
            p99.available or err.available or pool.available
        )
        if not reached:
            return HealthProbe._unavailable_card(service, version)
        return {
            "service": service,
            "p99_ms": None if pool_only else p99.latest,
            "error_rate": None if pool_only else err.latest,
            "pool_utilisation": pool.latest,
            "version": version,
            "status": _status(
                None if pool_only else p99.latest,
                None if pool_only else err.latest,
                pool.latest,
            ),
            "source": "prometheus",
            "series": {
                "p99_ms": [] if pool_only else p99.series,
                "error_rate": [] if pool_only else err.series,
                "pool_utilisation": pool.series,
            },
        }

    @staticmethod
    def _unavailable_card(service: str, version: str | None) -> dict[str, Any]:
        # Nulls, never zeros: a zero error rate is a finding, and Prometheus
        # being down is not evidence that nothing is failing.
        return {
            "service": service,
            "p99_ms": None,
            "error_rate": None,
            "pool_utilisation": None,
            "version": version,
            "status": "unknown",
            "source": "unavailable",
            "series": {"p99_ms": [], "error_rate": [], "pool_utilisation": []},
        }

    def _unavailable(self) -> dict[str, Any]:
        return {
            "ts": datetime.now(UTC).isoformat(),
            "services": [
                self._unavailable_card(s, None) for s in (*SERVICES, POOL_CARD)
            ],
        }


def probe_dep(request: Request) -> HealthProbe:
    probe = getattr(request.app.state, "war_room_health", None)
    if probe is None:
        container = _container(request)
        probe = HealthProbe(
            getattr(container, "prometheus", None),
            getattr(container, "runtime_adapter", None),
        )
        request.app.state.war_room_health = probe
    return probe


ProbeDep = Annotated[HealthProbe, Depends(probe_dep)]


# --------------------------------------------------------------------------- #
# state                                                                        #
# --------------------------------------------------------------------------- #


def _integrations(settings: Settings) -> dict[str, dict[str, Any]]:
    """Which integrations are configured. Counts and reasons, never values."""

    def entry(configured: bool, reason: str) -> dict[str, Any]:
        return {"configured": configured, "reason": "" if configured else reason}

    has_aws = bool(settings.aws_profile or settings.aws_access_key_id.get_secret_value())
    brain_keys = len(settings.gemini_pool_keys("brain"))
    compactor_keys = len(settings.gemini_pool_keys("compactor"))
    rawtree_write = bool(settings.rawtree_write_key.get_secret_value())
    rawtree_read = bool(settings.rawtree_read_key.get_secret_value())
    return {
        "bedrock": entry(
            bool(settings.bedrock_model_id) and has_aws,
            "BEDROCK_MODEL_ID or AWS credentials are not set",
        ),
        "gemini": {
            "configured": brain_keys + compactor_keys > 0,
            "reason": f"brain pool {brain_keys} key(s), compactor pool {compactor_keys} key(s)",
        },
        "rawtree": entry(
            rawtree_write and rawtree_read,
            f"write key {'set' if rawtree_write else 'missing'}, "
            f"read key {'set' if rawtree_read else 'missing'}",
        ),
        "nimble": entry(
            bool(settings.nimble_api_key.get_secret_value()),
            "NIMBLE_API_KEY is not set; the recorded fixture is used",
        ),
        "flux": entry(
            bool(settings.bfl_api_key.get_secret_value()),
            "BFL_API_KEY is not set; incident maps are unavailable",
        ),
        "prometheus": entry(bool(settings.prometheus_url), "PROMETHEUS_URL is not set"),
    }


def _brain_status(request: Request) -> dict[str, Any]:
    brain = getattr(request.app.state, "brain", None)
    if brain is None:
        return {"available": False, "reason": "the brain runs in the worker process"}
    try:
        status: dict[str, Any] = brain.status()
    except Exception as exc:  # noqa: BLE001 - a status badge must not 500 the page
        return {"available": False, "reason": f"status unavailable: {type(exc).__name__}"}
    return status


def _stats(run: HorizonState | None) -> dict[str, Any]:
    if run is None:
        return {
            "compression_ratio": 0.0, "cache_hits": 0, "cache_read_tokens": 0,
            "fallbacks_used": 0, "steps": 0, "context_tokens": 0, "naive_tokens": 0,
        }
    t = run.tokens
    ratio = (
        round(t.compacted_raw_tokens / t.compacted_card_tokens, 2)
        if t.compacted_card_tokens
        else 0.0
    )
    return {
        "compression_ratio": ratio,
        "cache_hits": t.cache_hits,
        "cache_read_tokens": t.cache_read_tokens,
        "fallbacks_used": t.fallbacks_used,
        "steps": run.step,
        "context_tokens": t.context_tokens,
        "naive_tokens": t.naive_tokens,
    }


async def _incident(request: Request, run: HorizonState | None) -> dict[str, Any] | None:
    if run is None:
        return None
    repo = getattr(_container(request), "incidents", None)
    if repo is None:
        return None
    try:
        incident = await repo.get(run.incident_id)
    except NotFoundError:
        return None
    return {
        "id": incident.id,
        "title": incident.title,
        "severity": str(incident.severity),
        "state": str(incident.state),
        "service": run.service,
    }


async def build_state(
    request: Request, settings: Settings, store: WarRoomStore
) -> dict[str, Any]:
    run = await store.latest_run()
    cards: list[MemoryCard] = await store.memory_cards(limit=20)
    return {
        "mode": settings.aegis_mode,
        "run": run.model_dump(mode="json") if run is not None else None,
        "incident": await _incident(request, run),
        "brain": _brain_status(request),
        "integrations": _integrations(settings),
        "memory_cards": [c.model_dump(mode="json") for c in cards],
        "stats": _stats(run),
        "last_seq": await store.last_seq(),
    }


async def _scope(store: WarRoomStore, incident_id: str | None) -> str | None:
    """The incident a read is about: the one asked for, else the latest run's."""
    if incident_id:
        return incident_id
    run = await store.latest_run()
    return run.incident_id if run is not None else None


# --------------------------------------------------------------------------- #
# reads                                                                        #
# --------------------------------------------------------------------------- #


@router.get("/state", summary="War-room snapshot")
async def state(
    request: Request, _: RequireViewer, settings: SettingsDep, store: StoreDep
) -> dict[str, Any]:
    return await build_state(request, settings, store)


@router.get("/events", summary="Horizon events after a sequence number")
async def events(
    _: RequireViewer,
    store: StoreDep,
    incident_id: Annotated[str | None, Query(max_length=64)] = None,
    after: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=1000)] = 500,
) -> dict[str, Any]:
    scope = await _scope(store, incident_id)
    if scope is None:
        return {"events": []}
    rows = await store.events(scope, after_seq=after, limit=limit)
    return {"events": [{"seq": s, "event": e.model_dump(mode="json")} for s, e in rows]}


@router.get("/context-series", summary="Context vs naive tokens per step")
async def context_series(
    _: RequireViewer,
    store: StoreDep,
    incident_id: Annotated[str | None, Query(max_length=64)] = None,
) -> dict[str, Any]:
    scope = await _scope(store, incident_id)
    return {"points": await store.context_series(scope) if scope else []}


@router.get("/health", summary="Health cards for the reference workload")
async def health(_: RequireViewer, probe: ProbeDep) -> dict[str, Any]:
    return await probe.payload()


@router.get("/rawtree-queries", summary="RawTree queries the agent and heartbeat ran")
async def rawtree_queries(
    _: RequireViewer,
    store: StoreDep,
    incident_id: Annotated[str | None, Query(max_length=64)] = None,
) -> dict[str, Any]:
    scope = await _scope(store, incident_id)
    if scope is None:
        return {"queries": []}
    rows = await store.events_of_type(scope, HorizonEventType.RAWTREE_QUERY, limit=100)
    out: list[dict[str, Any]] = []
    for _seq, event in rows:
        p = event.payload
        raw_rows = p.get("rows", 0)
        out.append(
            {
                "ts": event.ts.isoformat(),
                "name": str(p.get("name", event.tool or "")),
                "sql": str(p.get("sql", "")),
                "rows": len(raw_rows) if isinstance(raw_rows, list) else int(raw_rows or 0),
                "source": str(p.get("source", event.source.value)),
                "duration_ms": int(p.get("duration_ms", event.duration_ms) or 0),
                "error": p.get("error"),
            }
        )
    return {"queries": out}


@router.get(
    "/incident-maps/{card_id}",
    summary="A memory card's incident map image",
    response_class=Response,
)
async def incident_map(card_id: str, _: RequireViewer, store: StoreDep) -> Response:
    found = await store.get_incident_map(card_id)
    if found is None:
        raise NotFoundError("no incident map for this card", context={"card_id": card_id[:64]})
    image, mime = found
    return Response(
        content=image,
        media_type=mime,
        headers={"Cache-Control": "private, max-age=300", "X-Content-Type-Options": "nosniff"},
    )


@router.get("/stream", summary="Live war-room stream (SSE)")
async def stream(
    request: Request,
    _: RequireViewer,
    settings: SettingsDep,
    store: StoreDep,
    probe: ProbeDep,
    last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
) -> EventSourceResponse:
    async def snapshot() -> dict[str, Any]:
        return await build_state(request, settings, store)

    async def replay(after: int, limit: int) -> list[dict[str, Any]]:
        scope = await _scope(store, None)
        if scope is None:
            return []
        rows = await store.events(scope, after_seq=after, limit=limit)
        return [{"seq": s, "event": e.model_dump(mode="json")} for s, e in rows]

    return EventSourceResponse(
        war_room_stream(
            snapshot=snapshot,
            health=probe.payload,
            replay=replay,
            redis=_redis(request),
            is_disconnected=request.is_disconnected,
            last_event_id=parse_last_event_id(last_event_id),
        ),
        send_timeout=30,
    )


# --------------------------------------------------------------------------- #
# demo controls (local only)                                                   #
# --------------------------------------------------------------------------- #


class InjectRequest(BaseModel):
    scenario: Literal["INC-043"] = "INC-043"


class WorkloadAdmin:
    """Clears a workload fault through its own ``/admin/fault`` endpoint.

    URLs come from ``WORKLOAD_METRICS_TARGETS`` only; nothing a caller sends
    selects a host.
    """

    def __init__(self, settings: Settings, *, transport: httpx.AsyncBaseTransport | None = None):
        self._targets = settings.metrics_targets
        self._transport = transport

    def _admin_url(self, service: str) -> str | None:
        url = self._targets.get(service)
        if not url:
            return None
        base = url[: -len("/metrics")] if url.endswith("/metrics") else url.rstrip("/")
        return f"{base}/admin/fault"

    async def clear_fault(self, service: str) -> bool:
        url = self._admin_url(service)
        if url is None:
            return False

        async def _post() -> int:
            async with httpx.AsyncClient(
                timeout=ADMIN_TIMEOUT_S, transport=self._transport
            ) as client:
                resp = await client.post(url, json={"mode": "none"})
                return resp.status_code

        try:
            status = await guarded_call(
                _post, dependency=f"workload-admin-{service}", timeout_s=ADMIN_TIMEOUT_S,
                attempts=2,
            )
        except Exception as exc:  # noqa: BLE001 - reported to the operator, not raised
            log.warning("workload fault clear failed", service=service, error=type(exc).__name__)
            return False
        return 200 <= status < 300


def _workload_admin(request: Request, settings: Settings) -> WorkloadAdmin:
    admin = getattr(request.app.state, "workload_admin", None)
    return admin if admin is not None else WorkloadAdmin(settings)


async def _operator_deploy(
    request: Request,
    settings: Settings,
    principal: Principal,
    *,
    version: str,
    kind: str,
) -> tuple[bool, str, str | None]:
    """Recreate checkout on ``version`` and record it as an operator deploy.

    The attempt row is opened before the change and closed with the observed
    outcome, so a crash mid-deploy leaves an IN_PROGRESS row rather than no
    trace - and the row is what later gives the agent a rollback target.
    """
    container = _container(request)
    adapter = getattr(container, "runtime_adapter", None)
    deployments = getattr(container, "deployments", None)
    if adapter is None or not getattr(adapter, "available", False):
        reason = getattr(adapter, "unavailable_reason", "") or "no runtime adapter"
        return False, f"runtime unavailable: {reason}", None
    if deployments is None:
        return False, "deployment records unavailable", None

    service_id = ServiceRef.build(
        settings.aegis_environment_name, settings.workload_namespace, SCENARIO_SERVICE
    ).service_id
    try:
        current = (await adapter.get_service(SCENARIO_SERVICE)).version
    except AegisError:
        current = None
    detail = {
        "actor": f"operator:{principal.uid}",
        "kind": kind,
        "fault_injection": kind == "deploy",
        "source": "war-room",
    }
    attempt = await deployments.start(
        environment=settings.aegis_environment_name,
        service_id=service_id,
        from_version=current,
        to_version=version,
        strategy="recreate",
        detail=detail,
    )
    try:
        result = await adapter.rollback_deployment(
            SCENARIO_SERVICE, version, idempotency_key=f"war-room:{kind}:{attempt.id}"
        )
    except Exception as exc:
        await deployments.finish(
            attempt.id, state=DeploymentState.FAILED, error=f"{type(exc).__name__}: {exc}"
        )
        if isinstance(exc, AegisError):
            return False, f"{SCENARIO_SERVICE} -> {version} failed: {exc.message}", attempt.id
        raise
    await deployments.finish(
        attempt.id,
        state=DeploymentState.DEPLOYED,
        from_version=current,
        to_version=version,
        detail={"no_op": result.no_op, "performed": result.performed},
    )
    log.info(
        "operator deployment recorded",
        deployment_id=attempt.id,
        service=SCENARIO_SERVICE,
        version=version,
        kind=kind,
        actor=detail["actor"],
    )
    return True, f"{SCENARIO_SERVICE} now on {version} ({result.detail})", attempt.id


@router.post("/inject", summary="Fault injection: deploy the leaking checkout build")
async def inject(
    body: InjectRequest,
    request: Request,
    principal: RequireResponder,
    settings: SettingsDep,
) -> dict[str, Any]:
    _require_local(settings, "fault injection")
    ok, detail, deployment_id = await _operator_deploy(
        request, settings, principal, version=BAD_VERSION, kind="deploy"
    )
    log.warning("war-room fault injected", scenario=body.scenario, ok=ok, actor=principal.uid)
    return {"ok": ok, "detail": f"[fault injection {body.scenario}] {detail}",
            "deployment_id": deployment_id}


@router.post("/reset", summary="Return the demo workload to its healthy build")
async def reset(
    request: Request,
    principal: RequireResponder,
    settings: SettingsDep,
    store: StoreDep,
) -> dict[str, Any]:
    _require_local(settings, "reset")
    parts: list[str] = []
    ok, detail, _dep = await _operator_deploy(
        request, settings, principal, version=HEALTHY_VERSION, kind="reset"
    )
    parts.append(detail)

    admin = _workload_admin(request, settings)
    cleared = await asyncio.gather(*(admin.clear_fault(s) for s in SERVICES))
    failed = [s for s, done in zip(SERVICES, cleared, strict=True) if not done]
    parts.append(f"fault cleared on {len(SERVICES) - len(failed)}/{len(SERVICES)} services"
                 + (f" (failed: {', '.join(failed)})" if failed else ""))
    ok = ok and not failed

    run = await store.latest_run()
    if run is not None and await store.mark_idle(run.run_id):
        parts.append(f"run {run.run_id} marked IDLE")
    else:
        parts.append("no active run")
    return {"ok": ok, "detail": "; ".join(parts)}


@router.post("/kill-worker", summary="Crash the worker to demonstrate resume")
async def kill_worker(
    request: Request, _: RequireAdmin, settings: SettingsDep
) -> dict[str, Any]:
    _require_local(settings, "kill-worker")
    redis = _redis(request)
    if redis is None:
        return {"ok": False, "detail": "redis unavailable; no control channel"}
    try:
        receivers = await publish_control(redis, "crash")
    except Exception as exc:  # noqa: BLE001 - reported, never raised to the page
        return {"ok": False, "detail": f"control publish failed: {type(exc).__name__}"}
    if receivers == 0:
        return {"ok": False, "detail": "no worker is subscribed to the control channel"}
    return {"ok": True, "detail": f"crash delivered to {receivers} worker(s)"}


__all__ = ["HealthProbe", "WarRoomStore", "WorkloadAdmin", "build_state", "router"]
