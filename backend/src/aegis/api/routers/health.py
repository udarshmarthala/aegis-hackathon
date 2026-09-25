"""Liveness, readiness and integration health.

Three distinct questions, three endpoints - conflating them causes Kubernetes to
restart a process that is merely waiting on a dependency:

* ``/health/live``  - is the process alive? Never touches a dependency.
* ``/health/ready`` - can it serve traffic? Checks the hard dependency only.
* ``/health``       - full integration picture for the UI health surface.
"""

from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, Request, Response

from aegis.agents.llm import PROVIDER as LLM_PROVIDER
from aegis.api.deps import DbDep, SettingsDep
from aegis.core.logging import get_logger
from aegis.core.resilience import breaker_states

log = get_logger(__name__)
router = APIRouter(tags=["health"])

_STARTED_AT = time.monotonic()


@router.get("/health/live")
async def live() -> dict[str, Any]:
    """Process liveness. Deliberately dependency-free.

    If this fails the process is genuinely wedged and restarting is correct.
    """
    return {"status": "alive", "uptime_s": round(time.monotonic() - _STARTED_AT, 1)}


@router.get("/health/ready")
async def ready(db: DbDep, response: Response) -> dict[str, Any]:
    """Readiness. Postgres is the only hard dependency (HLD section 3)."""
    ok = await db.healthy()
    if not ok:
        response.status_code = 503
    return {"status": "ready" if ok else "not_ready", "postgres": ok}


@router.get("/health")
async def health(db: DbDep, settings: SettingsDep) -> dict[str, Any]:
    """Full dependency picture backing the Integration Health surface.

    Each entry distinguishes hard from soft, so the UI can show that Aegis is
    still operating with reduced confidence rather than implying an outage.
    """
    components: dict[str, dict[str, Any]] = {}

    postgres_ok = await db.healthy()
    components["postgres"] = {
        "status": "healthy" if postgres_ok else "unavailable",
        "hard_dependency": True,
        "affects": ["everything"],
    }

    components["neo4j"] = await _probe_neo4j(settings)
    components["redis"] = await _probe_redis(settings)
    components["prometheus"] = await _probe_http(
        f"{settings.prometheus_url}/-/healthy", "prometheus",
        ["metric evidence", "verification"],
    )
    components["tempo"] = await _probe_http(
        f"{settings.tempo_url}/ready", "tempo", ["trace evidence", "causal paths"],
    )
    components["loki"] = await _probe_http(
        f"{settings.loki_url}/ready", "loki", ["log evidence"],
    )

    # Reported per key, not just per provider: three exhausted free-tier keys
    # and one healthy one is a real degradation an operator needs to see before
    # the fourth goes, and "configured" alone would hide it entirely.
    keys = settings.google_api_keys
    components["llm_provider"] = {
        "status": "configured" if keys else "unconfigured",
        "hard_dependency": False,
        "provider": LLM_PROVIDER,
        "keys_configured": len(keys),
        "models": {
            "fast": settings.llm_model_fast,
            "reasoning": settings.llm_model_reasoning,
            "code": settings.llm_model_code,
            "embedding": settings.llm_embedding_model,
        },
        "affects": ["hypothesis generation", "diagnosis", "debugging"],
    }
    components["langsmith"] = {
        "status": "configured" if settings.langsmith_api_key.get_secret_value() else "disabled",
        "hard_dependency": False,
        "affects": ["AI tracing and evaluation only"],
    }

    degraded = [k for k, v in components.items()
                if v["status"] not in ("healthy", "configured")]
    overall = (
        "unavailable" if not postgres_ok
        else "degraded" if degraded
        else "healthy"
    )

    return {
        "status": overall,
        "version": settings.aegis_version,
        "environment": settings.aegis_env.value,
        "autonomy": {
            "enabled": settings.autonomy_enabled,
            "mode": settings.autonomy_mode.value,
            "allowed_tiers": sorted(settings.allowed_tiers),
        },
        "components": components,
        "circuit_breakers": breaker_states(),
        "degraded_components": degraded,
    }


async def _probe_http(url: str, component: str, affects: list[str]) -> dict[str, Any]:
    """Soft probe with a short timeout - health must never hang."""
    import httpx

    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            resp = await client.get(url)
        healthy = resp.status_code < 400
        return {
            "status": "healthy" if healthy else "degraded",
            "hard_dependency": False,
            "component": component,
            "affects": affects,
            "detail": None if healthy else f"HTTP {resp.status_code}",
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "status": "unavailable",
            "hard_dependency": False,
            "component": component,
            "affects": affects,
            "detail": type(exc).__name__,
        }


async def _probe_neo4j(settings: SettingsDep) -> dict[str, Any]:
    affects = ["topology", "blast radius", "causal paths"]
    try:
        from aegis.graph.client import Neo4jClient

        client = Neo4jClient(settings)
        ok = await client.healthy()
        await client.close()
        return {
            "status": "healthy" if ok else "unavailable",
            "hard_dependency": False,
            "affects": affects,
        }
    except Exception as exc:  # noqa: BLE001
        return {"status": "unavailable", "hard_dependency": False,
                "affects": affects, "detail": type(exc).__name__}


async def _probe_redis(settings: SettingsDep) -> dict[str, Any]:
    affects = ["live streaming", "rate limits", "cache"]
    try:
        import redis.asyncio as aioredis

        client = aioredis.from_url(settings.redis_url, decode_responses=True)
        await client.ping()
        await client.aclose()
        return {"status": "healthy", "hard_dependency": False, "affects": affects}
    except Exception as exc:  # noqa: BLE001
        return {"status": "unavailable", "hard_dependency": False,
                "affects": affects, "detail": type(exc).__name__}

@router.get(
    "/metrics",
    summary="Prometheus scrape endpoint",
    include_in_schema=False,
    response_class=Response,
)
async def metrics(request: Request) -> Response:
    """Aegis's own metrics.

    Unauthenticated on purpose, like every other Prometheus target: the scraper
    holds no user identity, and the exported series carry counts and posture
    gauges only - never incident content, never model output, and no unbounded
    label such as an incident id. In a deployed environment this port is reached
    from inside the VPC, not from the internet.

    Gauges that mirror live state are refreshed here, at scrape time, rather
    than on every change. A breaker that flaps would otherwise produce more
    metric churn than signal, and the scrape is the only moment the value is
    actually read.
    """
    from aegis.telemetry import metrics as m

    container = getattr(request.app.state, "container", None)
    if container is not None:
        m.safe(m.observe_breakers, breaker_states())
        try:
            kill_switch = await container.policy.load_kill_switches()
            m.safe(
                m.observe_posture,
                autonomy=container.settings.autonomy_enabled,
                kill_switch=kill_switch.any_engaged or kill_switch.degraded,
                audit_failures=container.audit.write_failures,
            )
            rows = await container.db.fetch(
                """
                SELECT severity, count(*) AS n FROM incidents
                 WHERE state <> 'RESOLVED' GROUP BY severity
                """
            )
            for row in rows:
                m.safe(
                    m.open_incidents.labels(severity=row["severity"]).set,
                    float(row["n"]),
                )
            queued = await container.db.fetch(
                """
                SELECT kind, count(*) AS n FROM workflow_jobs
                 WHERE status = 'queued' GROUP BY kind
                """
            )
            for row in queued:
                m.safe(m.queue_depth.labels(kind=row["kind"]).set, float(row["n"]))
        except Exception as exc:  # noqa: BLE001 - a scrape must never 500
            log.warning("metrics refresh incomplete", error=str(exc))

    payload, content_type = m.render()
    return Response(content=payload, media_type=content_type)
