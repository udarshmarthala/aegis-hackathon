"""API request metrics: bounded labels, one registry, idempotent install."""

from __future__ import annotations

from fastapi import FastAPI, Response
from fastapi.testclient import TestClient

from aegis.api import metrics as api_metrics
from aegis.telemetry.metrics import REGISTRY


def _count(route: str, method: str = "GET", status: str = "200") -> float:
    value = REGISTRY.get_sample_value(
        "aegis_http_requests_total", {"route": route, "method": method, "status": status}
    )
    return value or 0.0


def _app() -> FastAPI:
    app = FastAPI()

    @app.get("/items/{item_id}")
    async def item(item_id: str) -> dict[str, str]:
        return {"id": item_id}

    @app.get("/boom")
    async def boom() -> dict[str, str]:
        raise RuntimeError("boom")

    return app


def test_requests_are_labelled_by_route_template_not_raw_path() -> None:
    app = _app()
    api_metrics.install_metrics(app)
    before = _count("/items/{item_id}")
    with TestClient(app) as client:
        client.get("/items/42")
        client.get("/items/43")
    assert _count("/items/{item_id}") - before == 2
    # Raw ids never become label values.
    assert REGISTRY.get_sample_value(
        "aegis_http_requests_total", {"route": "/items/42", "method": "GET", "status": "200"}
    ) is None


def test_unmatched_paths_share_one_label() -> None:
    app = _app()
    api_metrics.install_metrics(app)
    before = _count("unmatched", status="404")
    with TestClient(app) as client:
        client.get("/nope/1")
        client.get("/nope/2")
    assert _count("unmatched", status="404") - before == 2


def test_an_unhandled_error_is_counted_as_500() -> None:
    app = _app()
    api_metrics.install_metrics(app)
    before = _count("/boom", status="500")
    with TestClient(app, raise_server_exceptions=False) as client:
        client.get("/boom")
    assert _count("/boom", status="500") - before == 1


def test_install_is_idempotent_and_serves_metrics_once() -> None:
    app = _app()
    api_metrics.install_metrics(app)
    api_metrics.install_metrics(app)
    installed = [m for m in app.user_middleware
                 if getattr(m, "cls", None) is api_metrics.RequestMetricsMiddleware]
    assert len(installed) == 1
    assert sum(1 for r in app.router.routes if getattr(r, "path", "") == "/metrics") == 1

    before = _count("/items/{item_id}")
    with TestClient(app) as client:
        client.get("/items/1")
        text = client.get("/metrics").text
    # Counted once, not once per install.
    assert _count("/items/{item_id}") - before == 1
    for name in ("aegis_http_requests_total", "aegis_http_request_duration_seconds",
                 "aegis_brain_fallbacks_total", "aegis_horizon_steps_total",
                 "aegis_rawtree_dropped_total"):
        assert name in text


def test_an_existing_metrics_route_is_left_in_charge() -> None:
    app = _app()

    @app.get("/metrics")
    async def existing() -> Response:
        return Response(content="existing", media_type="text/plain")

    api_metrics.install_metrics(app)
    with TestClient(app) as client:
        assert client.get("/metrics").text == "existing"


def test_two_apps_share_the_registry_without_duplicate_registration() -> None:
    for _ in range(2):
        api_metrics.install_metrics(_app())


def test_safe_inc_never_raises() -> None:
    before = REGISTRY.get_sample_value("aegis_brain_fallbacks_total", {"tier": "scripted"}) or 0
    api_metrics.safe_inc(api_metrics.brain_fallbacks, tier="scripted")
    api_metrics.safe_inc(api_metrics.brain_fallbacks, wrong_label="x")  # swallowed
    api_metrics.safe_inc(api_metrics.horizon_steps)
    after = REGISTRY.get_sample_value("aegis_brain_fallbacks_total", {"tier": "scripted"})
    assert after == before + 1
