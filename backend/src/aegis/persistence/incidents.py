"""Incident repository.

Explicit SQL rather than an ORM: the hot paths here are the incident list and
the incident detail fan-out, and both benefit from queries a reviewer can read
and an EXPLAIN can be run against.
"""

from __future__ import annotations

from typing import Any

import asyncpg

from aegis.core.errors import NotFoundError
from aegis.core.ids import new_id
from aegis.core.logging import get_logger
from aegis.domain.enums import IncidentState, Severity
from aegis.domain.models import Incident
from aegis.domain.state_machines import assert_incident_transition
from aegis.persistence.db import Database

log = get_logger(__name__)

_COLUMNS = """
    id, title, severity, state, environment, workload, affected_services,
    suspected_origin, confidence, summary, owner, correlation_id,
    created_at, updated_at, resolved_at
"""


def _to_model(row: asyncpg.Record) -> Incident:
    return Incident(
        id=row["id"],
        title=row["title"],
        severity=Severity(row["severity"]),
        state=IncidentState(row["state"]),
        environment=row["environment"],
        workload=row["workload"],
        affected_services=list(row["affected_services"] or []),
        suspected_origin=row["suspected_origin"],
        confidence=row["confidence"],
        summary=row["summary"],
        owner=row["owner"],
        correlation_id=row["correlation_id"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        resolved_at=row["resolved_at"],
    )


class IncidentRepository:
    __slots__ = ("_db",)

    def __init__(self, db: Database) -> None:
        self._db = db

    async def create(
        self,
        *,
        title: str,
        severity: Severity,
        environment: str,
        workload: str = "default",
        correlation_id: str = "",
        conn: asyncpg.Connection | None = None,
    ) -> Incident:
        """Create an incident.

        Accepts an optional connection so ingestion can create the incident, its
        alert and its workflow job inside one transaction - a job must never be
        able to reference an incident that was rolled back.
        """
        incident_id = new_id("inc")
        query = f"""
            INSERT INTO incidents (id, title, severity, state, environment, workload,
                                   correlation_id)
            VALUES ($1, $2, $3, $4, $5, $6, $7)
            RETURNING {_COLUMNS}
        """
        args = (
            incident_id,
            title,
            severity.value,
            IncidentState.RECEIVED.value,
            environment,
            workload,
            correlation_id,
        )
        row = await (conn.fetchrow(query, *args) if conn else self._db.fetchrow(query, *args))
        assert row is not None
        return _to_model(row)

    async def get(self, incident_id: str) -> Incident:
        row = await self._db.fetchrow(
            f"SELECT {_COLUMNS} FROM incidents WHERE id = $1", incident_id
        )
        if row is None:
            raise NotFoundError(
                f"incident {incident_id} not found", context={"incident_id": incident_id}
            )
        return _to_model(row)

    async def search(
        self,
        *,
        states: list[str] | None = None,
        severities: list[str] | None = None,
        environment: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Incident]:
        """Filtered list. Every value is bound, never interpolated.

        The LIMIT is clamped server-side so a client cannot request an unbounded
        page and exhaust the connection's memory.
        """
        clauses: list[str] = []
        args: list[Any] = []
        if states:
            args.append(states)
            clauses.append(f"state = ANY(${len(args)})")
        if severities:
            args.append(severities)
            clauses.append(f"severity = ANY(${len(args)})")
        if environment:
            args.append(environment)
            clauses.append(f"environment = ${len(args)}")
        where = "WHERE " + " AND ".join(clauses) if clauses else ""

        args.append(max(1, min(limit, 200)))
        args.append(max(0, offset))
        query = f"""
            SELECT {_COLUMNS} FROM incidents
            {where}
            ORDER BY created_at DESC
            LIMIT ${len(args) - 1} OFFSET ${len(args)}
        """
        return [_to_model(r) for r in await self._db.fetch(query, *args)]

    async def count_open(self, environment: str | None = None) -> int:
        if environment:
            value = await self._db.fetchval(
                "SELECT count(*) FROM incidents WHERE resolved_at IS NULL AND environment = $1",
                environment,
            )
        else:
            value = await self._db.fetchval(
                "SELECT count(*) FROM incidents WHERE resolved_at IS NULL"
            )
        return int(value or 0)

    async def transition(
        self,
        incident_id: str,
        to_state: IncidentState,
        *,
        actor: str,
        reason: str = "",
        correlation_id: str | None = None,
    ) -> Incident:
        """Move an incident, enforcing the state machine inside the transaction.

        The row is locked ``FOR UPDATE`` so two workers cannot both read the same
        source state and each conclude their transition is legal. The transition
        row written alongside is what makes the incident replayable (FR-18).
        """
        async with self._db.transaction() as conn:
            current = await conn.fetchval(
                "SELECT state FROM incidents WHERE id = $1 FOR UPDATE", incident_id
            )
            if current is None:
                raise NotFoundError(
                    f"incident {incident_id} not found", context={"incident_id": incident_id}
                )

            src = IncidentState(current)
            assert_incident_transition(src, to_state)

            resolved = ", resolved_at = now()" if to_state.is_terminal else ""
            row = await conn.fetchrow(
                f"""
                UPDATE incidents SET state = $2, updated_at = now(){resolved}
                WHERE id = $1 RETURNING {_COLUMNS}
                """,
                incident_id,
                to_state.value,
            )
            await conn.execute(
                """
                INSERT INTO incident_state_transitions
                    (incident_id, from_state, to_state, actor, reason, correlation_id)
                VALUES ($1, $2, $3, $4, $5, $6)
                """,
                incident_id,
                src.value,
                to_state.value,
                actor,
                reason,
                correlation_id,
            )

        assert row is not None
        log.info(
            "incident transition",
            incident_id=incident_id,
            from_state=src.value,
            to_state=to_state.value,
            actor=actor,
        )
        return _to_model(row)

    async def update_assessment(
        self,
        incident_id: str,
        *,
        summary: str | None = None,
        confidence: float | None = None,
        suspected_origin: str | None = None,
        affected_services: list[str] | None = None,
        severity: Severity | None = None,
    ) -> None:
        """Patch the assessment fields the investigation refines over time.

        Only supplied fields are written, so concurrent updates to disjoint
        fields do not clobber one another.
        """
        sets: list[str] = ["updated_at = now()"]
        args: list[Any] = [incident_id]
        for column, value in (
            ("summary", summary),
            ("confidence", confidence),
            ("suspected_origin", suspected_origin),
            ("affected_services", affected_services),
            ("severity", severity.value if severity else None),
        ):
            if value is not None:
                args.append(value)
                sets.append(f"{column} = ${len(args)}")
        if len(sets) == 1:
            return
        await self._db.execute(f"UPDATE incidents SET {', '.join(sets)} WHERE id = $1", *args)

    async def record_gap(
        self,
        incident_id: str,
        *,
        source: str,
        source_type: str,
        reason: str,
        affects: list[str] | None = None,
    ) -> None:
        """Record that a source could not be consulted.

        Distinct from 'found nothing'. The UI renders these differently and the
        confidence model penalises them (PRD 13).
        """
        await self._db.execute(
            """
            INSERT INTO evidence_gaps (incident_id, source, source_type, reason, affects)
            VALUES ($1, $2, $3, $4, $5)
            """,
            incident_id,
            source,
            source_type,
            reason,
            affects or [],
        )

    async def gaps(self, incident_id: str) -> list[dict[str, Any]]:
        rows = await self._db.fetch(
            """
            SELECT source, source_type, reason, affects, attempted_at
            FROM evidence_gaps WHERE incident_id = $1 ORDER BY attempted_at
            """,
            incident_id,
        )
        return [dict(r) for r in rows]

    async def timeline(self, incident_id: str) -> list[dict[str, Any]]:
        """State transitions, ordered. The forensic spine of the incident page."""
        rows = await self._db.fetch(
            """
            SELECT from_state, to_state, actor, reason, created_at
            FROM incident_state_transitions
            WHERE incident_id = $1 ORDER BY created_at
            """,
            incident_id,
        )
        return [dict(r) for r in rows]
