"""Alert ingestion (PRD FR-1).

Contract: acknowledge fast and durably, never investigate inline. The endpoint
persists the incident, the alert and the workflow job in a single transaction
and returns 202. A worker crash immediately afterwards loses nothing.

Idempotency is structural. ``incident_alerts (source, external_id)`` is UNIQUE,
so Alertmanager retrying the same alert attaches to the existing incident rather
than creating a second one.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Header, Request, Response, status
from pydantic import BaseModel, Field

from aegis.api.deps import IncidentsDep, SettingsDep
from aegis.api.security import verify_ingest_token
from aegis.core.ids import correlation_id
from aegis.core.logging import get_logger
from aegis.domain.enums import Severity
from aegis.persistence.jobs import JobQueue

log = get_logger(__name__)
router = APIRouter(tags=["ingestion"])


class AlertIn(BaseModel):
    """Inbound alert. Text fields are treated as untrusted downstream."""

    external_id: str = Field(min_length=1, max_length=256)
    source: str = Field(default="alertmanager", max_length=64)
    title: str = Field(min_length=1, max_length=512)
    severity: Severity = Severity.P3
    environment: str = Field(default="local", max_length=64)
    workload: str = Field(default="default", max_length=64)
    service_hint: str | None = Field(default=None, max_length=256)
    started_at: datetime | None = None
    labels: dict[str, str] = Field(default_factory=dict)
    annotations: dict[str, str] = Field(default_factory=dict)
    raw_payload: dict[str, Any] = Field(default_factory=dict)


class AlertAccepted(BaseModel):
    incident_id: str
    alert_id: str
    deduplicated: bool
    job_scheduled: bool
    correlation_id: str


@router.post(
    "/alerts",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=AlertAccepted,
    summary="Ingest an alert and open or attach to an incident",
)
async def ingest_alert(
    alert: AlertIn,
    request: Request,
    response: Response,
    settings: SettingsDep,
    incidents: IncidentsDep,
    x_aegis_ingest_token: Annotated[str | None, Header()] = None,
) -> AlertAccepted:
    # Machine-to-machine auth: a shared token compared in constant time, not a
    # user session. Alertmanager has no Firebase identity.
    verify_ingest_token(x_aegis_ingest_token, settings)

    cid = getattr(request.state, "correlation_id", correlation_id())
    db = request.app.state.db
    jobs = JobQueue(db)

    async with db.transaction() as conn:
        # Dedup first. A repeated alert must attach, never create.
        existing = await conn.fetchrow(
            "SELECT id, incident_id FROM incident_alerts WHERE source=$1 AND external_id=$2",
            alert.source,
            alert.external_id,
        )
        if existing is not None:
            log.info(
                "alert deduplicated",
                source=alert.source,
                external_id=alert.external_id,
                incident_id=existing["incident_id"],
            )
            response.status_code = status.HTTP_200_OK
            return AlertAccepted(
                incident_id=existing["incident_id"],
                alert_id=existing["id"],
                deduplicated=True,
                job_scheduled=False,
                correlation_id=cid,
            )

        incident = await incidents.create(
            title=alert.title,
            severity=alert.severity,
            environment=alert.environment,
            workload=alert.workload,
            correlation_id=cid,
            conn=conn,
        )

        alert_id = f"alr_{correlation_id()}"
        await conn.execute(
            """
            INSERT INTO incident_alerts
                (id, incident_id, source, external_id, title, severity,
                 service_hint, labels, annotations, raw_payload, started_at)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)
            """,
            alert_id,
            incident.id,
            alert.source,
            alert.external_id,
            alert.title,
            alert.severity.value,
            alert.service_hint,
            alert.labels,
            alert.annotations,
            alert.raw_payload,
            alert.started_at,
        )

        await conn.execute(
            """
            INSERT INTO audit_log
                (incident_id, actor, actor_type, event_type, resource_type,
                 resource_id, detail, correlation_id)
            VALUES ($1,$2,'system','alert_ingested','alert',$3,$4,$5)
            """,
            incident.id,
            alert.source,
            alert_id,
            {"external_id": alert.external_id, "severity": alert.severity.value},
            cid,
        )

        # Same transaction: the job cannot outlive a rolled-back incident.
        job_id = await jobs.enqueue(
            incident_id=incident.id,
            kind="investigate",
            payload={"trigger": "alert", "alert_id": alert_id},
            conn=conn,
        )

    log.info(
        "incident opened",
        incident_id=incident.id,
        severity=alert.severity.value,
        environment=alert.environment,
        source=alert.source,
    )
    return AlertAccepted(
        incident_id=incident.id,
        alert_id=alert_id,
        deduplicated=False,
        job_scheduled=job_id is not None,
        correlation_id=cid,
    )
