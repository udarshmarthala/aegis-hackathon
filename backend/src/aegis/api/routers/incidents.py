"""Incident read and command surface.

Read endpoints require VIEWER; anything that changes state requires RESPONDER or
higher. Authority is re-checked here on every call rather than trusted from the
client.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel, Field

from aegis.api.deps import EvidenceDep, IncidentsDep, RequireResponder, RequireViewer
from aegis.core.logging import get_logger
from aegis.domain.enums import IncidentState
from aegis.domain.models import Incident
from aegis.persistence.jobs import JobQueue

log = get_logger(__name__)
router = APIRouter(prefix="/incidents", tags=["incidents"])


class IncidentList(BaseModel):
    items: list[Incident]
    total_open: int
    limit: int
    offset: int


class EvidenceView(BaseModel):
    """Evidence shaped for the UI.

    ``status`` is surfaced verbatim so the client can render SOURCE_UNAVAILABLE
    differently from an absence of results - the distinction the whole evidence
    model exists to preserve.
    """

    id: str
    source: str
    source_type: str
    evidence_type: str
    status: str
    trust_class: str
    summary: str
    structured_value: dict[str, Any]
    provenance_uri: str
    resource_id: str | None
    observed_at: Any
    retrieved_at: Any
    untrusted: bool


@router.get("", response_model=IncidentList, summary="List incidents")
async def list_incidents(
    _: RequireViewer,
    incidents: IncidentsDep,
    state: Annotated[list[str] | None, Query()] = None,
    severity: Annotated[list[str] | None, Query()] = None,
    environment: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> IncidentList:
    items = await incidents.search(
        states=state, severities=severity, environment=environment,
        limit=limit, offset=offset,
    )
    return IncidentList(
        items=items,
        total_open=await incidents.count_open(environment),
        limit=limit,
        offset=offset,
    )


@router.get("/{incident_id}", response_model=Incident, summary="Get one incident")
async def get_incident(
    incident_id: str, _: RequireViewer, incidents: IncidentsDep
) -> Incident:
    return await incidents.get(incident_id)


@router.get("/{incident_id}/evidence", summary="Evidence for an incident")
async def get_evidence(
    incident_id: str,
    _: RequireViewer,
    evidence: EvidenceDep,
    include_gaps: Annotated[bool, Query()] = True,
) -> dict[str, Any]:
    items = await evidence.list_for_incident(incident_id, include_gaps=include_gaps)
    gaps = [i for i in items if i.is_gap]
    return {
        "items": [
            EvidenceView(
                id=i.id,
                source=i.source,
                source_type=i.source_type.value,
                evidence_type=i.evidence_type.value,
                status=i.status.value,
                trust_class=i.trust_class.value,
                summary=i.summary,
                structured_value=i.structured_value,
                provenance_uri=i.provenance_uri,
                resource_id=i.resource_id,
                observed_at=i.observed_at,
                retrieved_at=i.retrieved_at,
                untrusted=i.trust_class.value == "TIER_D",
            ).model_dump()
            for i in items
        ],
        "counts": {
            "total": len(items),
            "usable": len(items) - len(gaps),
            "unavailable_sources": len(gaps),
            "by_trust": await evidence.counts_by_trust(incident_id),
        },
    }


@router.get("/{incident_id}/timeline", summary="Incident timeline")
async def get_timeline(
    incident_id: str, _: RequireViewer, incidents: IncidentsDep, request: Request
) -> dict[str, Any]:
    """Merged forensic record: transitions, agent activity and tool calls.

    Tool calls are capped so a pathological investigation cannot return an
    unbounded payload to the browser.
    """
    db = request.app.state.db
    transitions = await incidents.timeline(incident_id)

    agent_rows = await db.fetch(
        """
        SELECT id, agent_role, status, task, result_summary, evidence_ids,
               duration_ms, started_at
        FROM agent_runs WHERE incident_id = $1 ORDER BY started_at LIMIT 500
        """,
        incident_id,
    )
    tool_rows = await db.fetch(
        """
        SELECT id, server, tool, access, ok, result_summary, duration_ms, created_at
        FROM tool_calls WHERE incident_id = $1 ORDER BY created_at LIMIT 500
        """,
        incident_id,
    )

    events: list[dict[str, Any]] = []
    for t in transitions:
        prior = t["from_state"] or "new"
        events.append({
            "kind": "STATE",
            "at": t["created_at"],
            "title": f"{prior} -> {t['to_state']}",
            "detail": t["reason"],
            "actor": t["actor"],
        })
    for a in agent_rows:
        events.append({
            "kind": "AI",
            "at": a["started_at"],
            "title": a["agent_role"],
            "detail": a["result_summary"] or a["task"],
            "evidence_ids": list(a["evidence_ids"] or []),
            "duration_ms": a["duration_ms"],
            "status": a["status"],
        })
    for c in tool_rows:
        events.append({
            "kind": "TOOL",
            "at": c["created_at"],
            "title": f"{c['server']}.{c['tool']}",
            "detail": c["result_summary"],
            "ok": c["ok"],
            "access": c["access"],
            "duration_ms": c["duration_ms"],
        })

    events.sort(key=lambda e: e["at"])
    return {"events": events, "gaps": await incidents.gaps(incident_id)}


@router.get("/{incident_id}/hypotheses", summary="Hypothesis stack")
async def get_hypotheses(
    incident_id: str, _: RequireViewer, request: Request
) -> dict[str, Any]:
    """The competing-hypothesis stack plus how confidence moved over time."""
    db = request.app.state.db
    rows = await db.fetch(
        """
        SELECT id, label, statement, state, confidence, supporting, contradicting,
               missing, predictions, affected_services, rejected_reason, updated_at
        FROM hypotheses WHERE incident_id = $1 ORDER BY confidence DESC
        """,
        incident_id,
    )
    history = await db.fetch(
        """
        SELECT h.label, c.confidence, c.recorded_at
        FROM hypothesis_confidence_history c
        JOIN hypotheses h ON h.id = c.hypothesis_id
        WHERE h.incident_id = $1 ORDER BY c.recorded_at
        """,
        incident_id,
    )
    return {
        "items": [dict(r) for r in rows],
        "confidence_history": [dict(r) for r in history],
    }


@router.get("/{incident_id}/diagnosis", summary="Current diagnosis")
async def get_diagnosis(
    incident_id: str, _: RequireViewer, request: Request
) -> dict[str, Any] | None:
    row = await request.app.state.db.fetchrow(
        "SELECT * FROM diagnoses WHERE incident_id = $1 ORDER BY created_at DESC LIMIT 1",
        incident_id,
    )
    return dict(row) if row else None


class ReinvestigateIn(BaseModel):
    reason: str = Field(default="", max_length=1000)


@router.post("/{incident_id}/reinvestigate", summary="Queue another investigation pass")
async def reinvestigate(
    incident_id: str,
    body: ReinvestigateIn,
    principal: RequireResponder,
    incidents: IncidentsDep,
    request: Request,
) -> dict[str, Any]:
    incident = await incidents.get(incident_id)
    if incident.state.is_terminal:
        return {"scheduled": False, "reason": "incident is resolved"}

    jobs = JobQueue(request.app.state.db)
    job_id = await jobs.enqueue(
        incident_id=incident_id,
        kind="investigate",
        payload={"trigger": "manual", "actor": principal.uid, "reason": body.reason},
    )
    return {
        "scheduled": job_id is not None,
        "reason": "" if job_id else "an investigation is already pending",
    }


class ResolveIn(BaseModel):
    reason: str = Field(default="resolved by operator", max_length=1000)


@router.post("/{incident_id}/resolve", response_model=Incident, summary="Resolve")
async def resolve(
    incident_id: str,
    body: ResolveIn,
    principal: RequireResponder,
    incidents: IncidentsDep,
) -> Incident:
    return await incidents.transition(
        incident_id, IncidentState.RESOLVED, actor=principal.uid, reason=body.reason
    )
