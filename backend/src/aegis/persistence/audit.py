"""Append-only audit log.

Every consequential event lands here: a policy decision, a lease acquisition, an
execution attempt, an approval, a rollback. Three properties make it useful
during a post-incident review rather than merely present:

* **Append-only.** There is no update or delete path in this module. A row that
  can be edited is not evidence of anything.
* **Correlated.** Each row carries the ``correlation_id`` that also appears in
  structured logs, OTel spans and the LangSmith run, so one identifier walks an
  investigator across all four systems.
* **Non-blocking on failure.** Losing an audit row must not abort an in-flight
  remediation, but it must be loud. Writes that fail are logged at ERROR and
  counted; they are never silently dropped.

The caller decides ``actor_type``. An agent-proposed action is ``agent``; a
human decision is ``human``; an expiry sweep is ``system``. Collapsing these
would make it impossible to answer "did a person authorise this?" - the single
most important question after an autonomous system touches production.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final, Literal

from aegis.core.logging import get_logger
from aegis.persistence.db import Database

log = get_logger(__name__)

ActorType = Literal["human", "agent", "system"]


class AuditEvent:
    """Stable event-type vocabulary.

    A closed set rather than free strings: audit queries and the compliance view
    both filter on these, and a typo would silently hide an event from a review.
    """

    INCIDENT_CREATED: Final = "incident.created"
    INCIDENT_STATE_CHANGED: Final = "incident.state_changed"
    INCIDENT_RESOLVED: Final = "incident.resolved"

    EVIDENCE_RECORDED: Final = "evidence.recorded"
    EVIDENCE_GAP: Final = "evidence.gap"

    DIAGNOSIS_PRODUCED: Final = "diagnosis.produced"
    DIAGNOSIS_ABSTAINED: Final = "diagnosis.abstained"

    ACTION_PROPOSED: Final = "action.proposed"
    ACTION_POLICY_DECIDED: Final = "action.policy_decided"
    ACTION_BLOCKED: Final = "action.blocked"
    ACTION_VALIDATED: Final = "action.validated"
    ACTION_EXECUTION_STARTED: Final = "action.execution_started"
    ACTION_EXECUTION_SUCCEEDED: Final = "action.execution_succeeded"
    ACTION_EXECUTION_FAILED: Final = "action.execution_failed"
    ACTION_ROLLED_BACK: Final = "action.rolled_back"
    ACTION_ROLLBACK_FAILED: Final = "action.rollback_failed"
    ACTION_EXPIRED: Final = "action.expired"

    APPROVAL_REQUESTED: Final = "approval.requested"
    APPROVAL_GRANTED: Final = "approval.granted"
    APPROVAL_REJECTED: Final = "approval.rejected"
    APPROVAL_EXPIRED: Final = "approval.expired"
    APPROVAL_MORE_EVIDENCE: Final = "approval.more_evidence_requested"

    LEASE_ACQUIRED: Final = "lease.acquired"
    LEASE_RELEASED: Final = "lease.released"
    LEASE_DENIED: Final = "lease.denied"
    LEASE_EXPIRED: Final = "lease.expired"

    VERIFICATION_STARTED: Final = "verification.started"
    VERIFICATION_COMPLETED: Final = "verification.completed"
    VERIFICATION_REGRESSION: Final = "verification.regression_detected"

    SANDBOX_STARTED: Final = "sandbox.started"
    SANDBOX_COMPLETED: Final = "sandbox.completed"
    SANDBOX_KILLED: Final = "sandbox.killed"

    DEPLOY_STAGING: Final = "deploy.staging"
    DEPLOY_PRODUCTION: Final = "deploy.production"

    KILL_SWITCH_ENGAGED: Final = "kill_switch.engaged"
    KILL_SWITCH_RELEASED: Final = "kill_switch.released"

    MEMORY_WRITTEN: Final = "memory.written"

    # The tool boundary. ``aegis.mcp.types.TOOL_WRITE_EVENT`` aliases
    # TOOL_WRITE_INVOKED rather than repeating the literal, so the string an
    # auditor filters on has exactly one definition in the codebase.
    TOOL_INVOKED: Final = "tool.invoked"
    TOOL_WRITE_INVOKED: Final = "tool.write_invoked"


@dataclass(frozen=True, slots=True)
class AuditRecord:
    """One audit row as read back for the audit surface."""

    id: int
    incident_id: str | None
    actor: str
    actor_type: str
    event_type: str
    resource_type: str | None
    resource_id: str | None
    detail: dict[str, Any]
    correlation_id: str | None
    created_at: Any


class AuditLog:
    """Writer and reader for ``audit_log``. No update or delete exists."""

    __slots__ = ("_db", "_write_failures")

    def __init__(self, db: Database) -> None:
        self._db = db
        self._write_failures = 0

    @property
    def write_failures(self) -> int:
        """Exposed on the health surface: a non-zero value means the audit trail
        has holes and any compliance claim about it is now qualified."""
        return self._write_failures

    async def record(
        self,
        *,
        event_type: str,
        actor: str,
        actor_type: ActorType,
        incident_id: str | None = None,
        resource_type: str | None = None,
        resource_id: str | None = None,
        detail: dict[str, Any] | None = None,
        correlation_id: str | None = None,
    ) -> None:
        """Append one event.

        Never raises. An audit failure is logged at ERROR and counted so it is
        visible, but it cannot abort the operation being audited - failing a
        rollback because its audit row could not be written would turn a
        recoverable incident into an unrecoverable one.
        """
        try:
            await self._db.execute(
                """
                INSERT INTO audit_log
                    (incident_id, actor, actor_type, event_type,
                     resource_type, resource_id, detail, correlation_id)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8)
                """,
                incident_id, actor, actor_type, event_type,
                resource_type, resource_id, detail or {}, correlation_id,
            )
        except Exception as exc:  # noqa: BLE001 - audit must never propagate
            self._write_failures += 1
            log.error(
                "audit write failed",
                event_type=event_type,
                incident_id=incident_id,
                correlation_id=correlation_id,
                error=str(exc),
                total_failures=self._write_failures,
            )

    async def record_in(
        self,
        conn: Any,
        *,
        event_type: str,
        actor: str,
        actor_type: ActorType,
        incident_id: str | None = None,
        resource_type: str | None = None,
        resource_id: str | None = None,
        detail: dict[str, Any] | None = None,
        correlation_id: str | None = None,
    ) -> None:
        """Append inside a caller-supplied transaction.

        Used where the audit row must commit atomically with the state change it
        describes - a lease acquisition, for example, where an audit row for a
        lease that was rolled back would be a lie. Unlike ``record`` this DOES
        propagate, because the caller has chosen atomicity deliberately.
        """
        await conn.execute(
            """
            INSERT INTO audit_log
                (incident_id, actor, actor_type, event_type,
                 resource_type, resource_id, detail, correlation_id)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8)
            """,
            incident_id, actor, actor_type, event_type,
            resource_type, resource_id, detail or {}, correlation_id,
        )

    async def for_incident(self, incident_id: str, *, limit: int = 500) -> list[AuditRecord]:
        rows = await self._db.fetch(
            """
            SELECT id, incident_id, actor, actor_type, event_type, resource_type,
                   resource_id, detail, correlation_id, created_at
            FROM audit_log
            WHERE incident_id = $1
            ORDER BY created_at ASC, id ASC
            LIMIT $2
            """,
            incident_id, min(limit, 2000),
        )
        return [self._row(r) for r in rows]

    async def recent(
        self,
        *,
        limit: int = 100,
        event_type: str | None = None,
        actor_type: str | None = None,
        correlation_id: str | None = None,
    ) -> list[AuditRecord]:
        """Filtered tail of the log for the operator audit surface."""
        rows = await self._db.fetch(
            """
            SELECT id, incident_id, actor, actor_type, event_type, resource_type,
                   resource_id, detail, correlation_id, created_at
            FROM audit_log
            WHERE ($2::text IS NULL OR event_type = $2)
              AND ($3::text IS NULL OR actor_type = $3)
              AND ($4::text IS NULL OR correlation_id = $4)
            ORDER BY created_at DESC, id DESC
            LIMIT $1
            """,
            min(limit, 1000), event_type, actor_type, correlation_id,
        )
        return [self._row(r) for r in rows]

    @staticmethod
    def _row(row: Any) -> AuditRecord:
        return AuditRecord(
            id=int(row["id"]),
            incident_id=row["incident_id"],
            actor=row["actor"],
            actor_type=row["actor_type"],
            event_type=row["event_type"],
            resource_type=row["resource_type"],
            resource_id=row["resource_id"],
            detail=row["detail"] or {},
            correlation_id=row["correlation_id"],
            created_at=row["created_at"],
        )


__all__ = ["ActorType", "AuditEvent", "AuditLog", "AuditRecord"]
