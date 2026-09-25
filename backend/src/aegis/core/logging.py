"""Structured logging with mandatory redaction.

Two properties matter operationally:

1. Every line carries ``correlation_id`` and, when known, ``incident_id``, so a
   single incident can be reconstructed across API, worker and agent processes.
2. Secrets never reach a log sink. The redaction processor runs last, after
   every other processor, so it also covers values injected by libraries.
"""

from __future__ import annotations

import logging
import re
import sys
from collections.abc import MutableMapping
from contextvars import ContextVar
from typing import Any

import structlog

_correlation_id: ContextVar[str | None] = ContextVar("correlation_id", default=None)
_incident_id: ContextVar[str | None] = ContextVar("incident_id", default=None)

# Patterns are deliberately broad: a false positive costs a masked log line, a
# false negative leaks a credential.
_SECRET_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"sk-or-v1-[A-Za-z0-9_-]{8,}"),
    re.compile(r"sk-[A-Za-z0-9]{20,}"),
    re.compile(r"lsv2_(?:pt|sk)_[A-Za-z0-9]{8,}"),
    re.compile(r"AIza[A-Za-z0-9_-]{20,}"),
    re.compile(r"AQ\.[A-Za-z0-9_.-]{16,}"),
    re.compile(r"gh[pousr]_[A-Za-z0-9]{16,}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"),
)

_SECRET_KEYS = frozenset(
    {
        "password", "passwd", "secret", "token", "api_key", "apikey",
        "authorization", "private_key", "credentials", "access_key",
        "id_token", "refresh_token", "bearer", "signing_key",
    }
)

_MASK = "[REDACTED]"


def _scrub_value(value: Any) -> Any:
    if isinstance(value, str):
        for pat in _SECRET_PATTERNS:
            value = pat.sub(_MASK, value)
        return value
    if isinstance(value, dict):
        return {k: (_MASK if k.lower() in _SECRET_KEYS else _scrub_value(v))
                for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return type(value)(_scrub_value(v) for v in value)
    return value


def redact_processor(
    _logger: Any, _name: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    """Final processor: mask secret-shaped values by key and by content."""
    return {
        key: (_MASK if key.lower() in _SECRET_KEYS else _scrub_value(val))
        for key, val in event_dict.items()
    }


def context_processor(
    _logger: Any, _name: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    """Attach ambient correlation identifiers to every event."""
    if (cid := _correlation_id.get()) is not None:
        event_dict.setdefault("correlation_id", cid)
    if (iid := _incident_id.get()) is not None:
        event_dict.setdefault("incident_id", iid)
    return event_dict


def bind_correlation_id(value: str | None) -> None:
    _correlation_id.set(value)


def bind_incident_id(value: str | None) -> None:
    _incident_id.set(value)


def get_correlation_id() -> str | None:
    return _correlation_id.get()


def configure_logging(level: str = "INFO", fmt: str = "json") -> None:
    """Idempotent logging setup. Safe to call from API, worker and tests."""
    renderer: Any = (
        structlog.processors.JSONRenderer()
        if fmt == "json"
        else structlog.dev.ConsoleRenderer(colors=False)
    )

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            context_processor,
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            redact_processor,  # must stay last before the renderer
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            logging.getLevelNamesMapping()[level]
        ),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stdout),
        cache_logger_on_first_use=True,
    )

    # Route stdlib logging (uvicorn, asyncpg, httpx) through the same pipeline.
    logging.basicConfig(format="%(message)s", stream=sys.stdout, level=level, force=True)
    for noisy in ("uvicorn.access", "httpx", "httpcore", "neo4j"):
        logging.getLogger(noisy).setLevel(
            max(logging.WARNING, logging.getLevelNamesMapping()[level])
        )


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)  # type: ignore[no-any-return]
