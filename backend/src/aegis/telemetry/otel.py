"""OpenTelemetry wiring.

Observability is never a control-plane dependency (ESD 34). Every function here
swallows its own failures: a missing collector degrades tracing and nothing
else. The value Aegis actually needs from OTel is a shared correlation id so an
operator can pivot between the infrastructure view and the AI view.
"""

from __future__ import annotations

from typing import Any

from aegis.core.config import Settings
from aegis.core.logging import get_logger

log = get_logger(__name__)

_initialised = False


def setup_tracing(settings: Settings) -> bool:
    """Install the tracer provider. Idempotent and failure-tolerant."""
    global _initialised
    if _initialised or not settings.otel_traces_enabled:
        return _initialised
    try:
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        resource = Resource.create({
            "service.name": settings.otel_service_name,
            "service.version": settings.aegis_version,
            "deployment.environment": settings.aegis_env.value,
        })
        provider = TracerProvider(resource=resource)
        provider.add_span_processor(
            BatchSpanProcessor(
                OTLPSpanExporter(
                    endpoint=settings.otel_exporter_otlp_endpoint, insecure=True
                )
            )
        )
        trace.set_tracer_provider(provider)
        _initialised = True
        log.info("otel tracing enabled", endpoint=settings.otel_exporter_otlp_endpoint)
    except Exception as exc:  # noqa: BLE001
        log.warning("otel tracing unavailable", error=str(exc))
    return _initialised


def instrument_app(app: Any, settings: Settings) -> None:
    """Instrument FastAPI, asyncpg and httpx if the SDK is present."""
    if not setup_tracing(settings):
        return
    try:
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

        FastAPIInstrumentor.instrument_app(
            app, excluded_urls="health,health/live,health/ready"
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("fastapi instrumentation skipped", error=str(exc))

    for name, importer in (
        ("asyncpg", "opentelemetry.instrumentation.asyncpg:AsyncPGInstrumentor"),
        ("httpx", "opentelemetry.instrumentation.httpx:HTTPXClientInstrumentor"),
    ):
        try:
            module_path, cls_name = importer.split(":")
            module = __import__(module_path, fromlist=[cls_name])
            getattr(module, cls_name)().instrument()
        except Exception as exc:  # noqa: BLE001
            log.warning("instrumentation skipped", target=name, error=str(exc))


def get_tracer(name: str) -> Any:
    """Return a tracer, or a no-op if tracing never initialised."""
    try:
        from opentelemetry import trace

        return trace.get_tracer(name)
    except Exception:  # noqa: BLE001
        class _NoopSpan:
            def __enter__(self) -> _NoopSpan:
                return self

            def __exit__(self, *_: object) -> None:
                return None

            def set_attribute(self, *_: object) -> None:
                return None

        class _NoopTracer:
            def start_as_current_span(self, *_: object, **__: object) -> _NoopSpan:
                return _NoopSpan()

        return _NoopTracer()
