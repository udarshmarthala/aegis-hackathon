"""Shared machinery for tool implementations.

Three jobs, each of which would otherwise be re-implemented slightly
differently in every tool module - and the slight differences are exactly where
an evidence gap turns into a silent empty result:

* ``degraded`` builds the one honest answer for "we could not look": an empty
  typed value, ``degraded=True`` with a reason, and an ``EvidenceGap`` row so
  the incident carries the gap into confidence scoring and into the UI.
* ``record_evidence`` writes an observation with a real ``provenance_uri`` and
  returns its id, so a tool result can cite what it saw.
* ``window`` and ``bounded`` keep every query time-boxed and every result set
  size-boxed. No tool anywhere returns an unbounded collection.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from aegis.core.logging import get_logger
from aegis.domain.enums import EvidenceType, SourceType
from aegis.mcp.deps import ToolDeps
from aegis.mcp.types import ToolContext, ToolOutcome, ToolOutput

log = get_logger(__name__)

# No tool window may exceed a day. A wider window is never a better
# investigation; it is a slower one that buries the signal.
MAX_WINDOW_S = 86_400
MIN_WINDOW_S = 60


def bounded(value: int, low: int, high: int) -> int:
    """Clamp a caller-supplied size into the range the source can serve."""
    return max(low, min(value, high))


def window(deps: ToolDeps, window_s: int) -> tuple[float, float]:
    """A clamped [start, end] epoch-second window ending now."""
    span = bounded(window_s, MIN_WINDOW_S, MAX_WINDOW_S)
    end = deps.clock.now().timestamp()
    return end - span, end


def offset_window(deps: ToolDeps, window_s: int, offset_s: int) -> tuple[float, float]:
    """A window of the same length, shifted back by ``offset_s``."""
    span = bounded(window_s, MIN_WINDOW_S, MAX_WINDOW_S)
    shift = bounded(offset_s, MIN_WINDOW_S, MAX_WINDOW_S * 7)
    end = deps.clock.now().timestamp() - shift
    return end - span, end


async def degraded(
    deps: ToolDeps,
    context: ToolContext,
    *,
    source: str,
    source_type: SourceType,
    reason: str,
    value: ToolOutput,
) -> ToolOutcome:
    """The answer when a source could not be consulted.

    Records an evidence gap when there is an incident to attach it to. The gap
    is what stops the next reader concluding "no anomaly found" from what was
    really "we never asked".
    """
    evidence_ids: tuple[str, ...] = ()
    if deps.evidence is not None and context.incident_id:
        try:
            item = await deps.evidence.record_unavailable(
                incident_id=context.incident_id,
                source=source,
                source_type=source_type,
                reason=reason,
            )
            evidence_ids = (item.id,)
        except Exception as exc:  # noqa: BLE001 - a gap we cannot store is still a gap
            log.error(
                "evidence gap could not be recorded",
                source=source, incident_id=context.incident_id, error=str(exc),
            )
    log.info(
        "tool degraded",
        source=source, reason=reason,
        incident_id=context.incident_id, correlation_id=context.correlation_id,
    )
    return ToolOutcome(
        value=value,
        evidence_ids=evidence_ids,
        provenance=(f"gap://{source}",),
        degraded=True,
        degraded_reason=reason,
    )


async def record_evidence(
    deps: ToolDeps,
    context: ToolContext,
    *,
    source: str,
    source_type: SourceType,
    evidence_type: EvidenceType,
    summary: str,
    structured_value: dict[str, Any],
    provenance_uri: str,
    resource_id: str | None = None,
    content: str | None = None,
    untrusted: bool = False,
    observed_at: datetime | None = None,
) -> tuple[str, ...]:
    """Store one observation and return its evidence id.

    Returns empty when there is no incident to attach to - an external MCP
    client browsing topology is not an investigation and must not write rows
    into someone's incident.
    """
    if deps.evidence is None or not context.incident_id:
        return ()
    try:
        item = await deps.evidence.record(
            incident_id=context.incident_id,
            source=source,
            source_type=source_type,
            evidence_type=evidence_type,
            summary=summary[:1000],
            structured_value=structured_value,
            content=content,
            untrusted=untrusted,
            provenance_uri=provenance_uri,
            resource_id=resource_id,
            observed_at=observed_at,
        )
    except Exception as exc:  # noqa: BLE001 - a tool answer survives a storage fault
        log.error(
            "evidence could not be recorded",
            source=source, incident_id=context.incident_id, error=str(exc),
        )
        return ()
    return (item.id,)


__all__ = [
    "MAX_WINDOW_S",
    "MIN_WINDOW_S",
    "bounded",
    "degraded",
    "offset_window",
    "record_evidence",
    "window",
]
