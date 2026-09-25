"""FastAPI application factory.

Responsibilities kept deliberately narrow: wiring, lifespan, error translation
and middleware. No business logic lives here.

Boot ordering matters. Postgres is the only hard dependency, so it is
established (and migrated) before the app serves traffic. Every soft dependency
is initialised best-effort: a missing Neo4j or an unconfigured Firebase degrades
a feature and is reported by /health, but never prevents startup.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, ORJSONResponse

from aegis.api.routers import (
    actions,
    alerts,
    approvals,
    audit,
    deployments,
    evaluation,
    graph,
    health,
    incidents,
    integrations,
    investigations,
    policy_admin,
    reliability,
    stream,
    systems,
    tasks,
)
from aegis.container import build_container
from aegis.core.config import Settings, get_settings
from aegis.core.errors import AegisError
from aegis.core.ids import correlation_id
from aegis.core.logging import bind_correlation_id, configure_logging, get_logger
from aegis.persistence.migrate import run_migrations

log = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings: Settings = get_settings()
    configure_logging(settings.log_level, settings.log_format)
    log.info(
        "aegis api starting",
        version=settings.aegis_version,
        environment=settings.aegis_env.value,
        autonomy_enabled=settings.autonomy_enabled,
        autonomy_mode=settings.autonomy_mode.value,
    )

    # One composition root, shared with the worker. Two processes wiring their
    # own components is how an API ends up enforcing a different policy from
    # the worker that actually executes.
    container = build_container(settings)
    await container.connect()
    await run_migrations(container.db)

    app.state.settings = settings
    app.state.container = container
    app.state.db = container.db
    app.state.incidents = container.incidents
    app.state.evidence = container.evidence
    app.state.redis = container.redis

    from aegis.api.security import FirebaseVerifier

    verifier = FirebaseVerifier(settings)
    verifier.initialise()  # best effort; /health reports the outcome
    app.state.verifier = verifier

    unavailable = [
        name for name, cap in container.capabilities.items() if not cap.configured
    ]
    log.info(
        "aegis api ready",
        capabilities_available=len(container.capabilities) - len(unavailable),
        capabilities_unavailable=unavailable,
    )

    try:
        yield
    finally:
        log.info("aegis api shutting down")
        await container.aclose()


def create_app() -> FastAPI:
    settings = get_settings()

    app = FastAPI(
        title="Aegis",
        version=settings.aegis_version,
        description="Evidence-driven AI SRE control plane",
        default_response_class=ORJSONResponse,
        lifespan=lifespan,
        docs_url="/docs" if not settings.is_production else None,
        redoc_url=None,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "X-Aegis-Ingest-Token",
                       "Last-Event-ID"],
        max_age=600,
    )

    @app.middleware("http")
    async def correlation_middleware(request: Request, call_next):  # type: ignore[no-untyped-def]
        """Give every request a correlation id and echo it back.

        The same id lands in logs, audit rows and the OTel trace, so an operator
        can pivot between them from a single value in a response header.
        """
        cid = request.headers.get("X-Correlation-ID") or correlation_id()
        bind_correlation_id(cid)
        request.state.correlation_id = cid
        try:
            response = await call_next(request)
        finally:
            bind_correlation_id(None)
        response.headers["X-Correlation-ID"] = cid
        return response

    @app.exception_handler(AegisError)
    async def aegis_error_handler(request: Request, exc: AegisError) -> JSONResponse:
        """Typed errors become predictable responses.

        Clients get a stable ``code`` they can branch on. Internals are never
        echoed - ``context`` is curated by the raiser, not a stack dump.
        """
        cid = getattr(request.state, "correlation_id", "")
        level = log.warning if exc.http_status < 500 else log.error
        level(
            "request failed",
            path=request.url.path,
            code=exc.code,
            status=exc.http_status,
            error=exc.message,
        )
        return JSONResponse(
            status_code=exc.http_status,
            content={"error": exc.to_dict(), "correlation_id": cid},
            headers={"X-Correlation-ID": cid},
        )

    @app.exception_handler(Exception)
    async def unhandled_handler(request: Request, exc: Exception) -> JSONResponse:
        cid = getattr(request.state, "correlation_id", "")
        log.exception("unhandled error", path=request.url.path, error=str(exc))
        return JSONResponse(
            status_code=500,
            content={
                "error": {"code": "INTERNAL_ERROR", "message": "internal error"},
                "correlation_id": cid,
            },
        )

    app.include_router(health.router)
    for module in (
        alerts, incidents, stream, policy_admin, actions, approvals, audit,
        deployments, evaluation, graph, integrations, investigations, reliability,
        systems, tasks,
    ):
        app.include_router(module.router, prefix="/v1")

    if settings.otel_traces_enabled:
        try:
            from aegis.telemetry.otel import instrument_app

            instrument_app(app, settings)
        except Exception as exc:  # noqa: BLE001 - observability is never fatal
            log.warning("otel instrumentation skipped", error=str(exc))

    return app


app = create_app()
