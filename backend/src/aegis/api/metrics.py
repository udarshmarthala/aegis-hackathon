"""HTTP request metrics for the API, and the long-horizon counters.

Registered on ``telemetry.metrics.REGISTRY`` - the registry the existing
``/metrics`` route in ``routers/health.py`` already serves - so there is one
scrape target and one exposition, not two registries that disagree.

The route label is the matched route *template* (``/v1/incidents/{incident_id}``),
never the raw path: raw paths carry ids, and an unbounded label is how a
metrics backend falls over. Anything that matched no route is ``unmatched``.
"""

from __future__ import annotations

import time
from typing import Any, Final

from fastapi import FastAPI, Request, Response
from prometheus_client import Counter, Histogram
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from aegis.telemetry.metrics import REGISTRY, render

UNMATCHED: Final = "unmatched"

http_requests = Counter(
    "aegis_http_requests_total",
    "API requests by route template, method and status.",
    ["route", "method", "status"],
    registry=REGISTRY,
)

http_request_duration = Histogram(
    "aegis_http_request_duration_seconds",
    "Time to the response head, by route template and method. Streaming "
    "responses are measured to their first byte, not their close.",
    ["route", "method"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10),
    registry=REGISTRY,
)

brain_fallbacks = Counter(
    "aegis_brain_fallbacks_total",
    "Brain steps served by a lower tier than the one tried first.",
    ["tier"],  # the tier that actually answered: gemini | scripted
    registry=REGISTRY,
)

horizon_steps = Counter(
    "aegis_horizon_steps_total",
    "Horizon orchestrator steps completed.",
    registry=REGISTRY,
)

rawtree_dropped = Counter(
    "aegis_rawtree_dropped_total",
    "RawTree metric rows dropped under queue pressure. Events and observations "
    "are never dropped; Postgres holds them.",
    registry=REGISTRY,
)


def _route_of(scope: Scope) -> str:
    route = scope.get("route")
    path = getattr(route, "path", None)
    return path if isinstance(path, str) and path else UNMATCHED


class RequestMetricsMiddleware:
    """Pure ASGI, so a streaming SSE response is never buffered by it."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        started = time.perf_counter()
        method = str(scope.get("method", "GET"))
        recorded = False

        def record(status: int) -> None:
            nonlocal recorded
            if recorded:
                return
            recorded = True
            route = _route_of(scope)
            try:
                http_requests.labels(route=route, method=method, status=str(status)).inc()
                http_request_duration.labels(route=route, method=method).observe(
                    time.perf_counter() - started
                )
            except Exception:  # noqa: BLE001, S110 - a metric never breaks a request
                return

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                record(int(message["status"]))
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        except Exception:
            record(500)
            raise


async def _metrics_endpoint(request: Request) -> Response:
    del request
    payload, content_type = render()
    return Response(content=payload, media_type=content_type)


def install_metrics(app: FastAPI) -> None:
    """Add request metrics to ``app``. Idempotent per app.

    Serves ``/metrics`` only when no route already does: the health router's
    endpoint also refreshes posture gauges at scrape time, so it wins when it
    is included. Call this after the routers are included.
    """
    if getattr(app.state, "aegis_metrics_installed", False):
        return
    app.add_middleware(RequestMetricsMiddleware)
    if not any(getattr(r, "path", None) == "/metrics" for r in app.router.routes):
        app.add_api_route(
            "/metrics", _metrics_endpoint, methods=["GET"], include_in_schema=False
        )
    app.state.aegis_metrics_installed = True


def safe_inc(counter: Any, **labels: str) -> None:
    """Increment without ever raising into the caller (CLAUDE.md invariant 9)."""
    try:
        (counter.labels(**labels) if labels else counter).inc()
    except Exception:  # noqa: BLE001, S110 - observability is not a dependency
        return


__all__ = [
    "RequestMetricsMiddleware",
    "brain_fallbacks",
    "horizon_steps",
    "http_request_duration",
    "http_requests",
    "install_metrics",
    "rawtree_dropped",
    "safe_inc",
]
