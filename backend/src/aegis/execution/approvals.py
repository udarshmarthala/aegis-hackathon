"""Human approval lifecycle for tier-2 actions.

Three rules make an approval meaningful rather than ceremonial:

1. **Approvals expire.** An operator who approved a rollback twenty minutes ago
   approved it against the system as it was then. Executing that decision now,
   after the incident has moved on, is not the thing they authorised. Expiry is
   enforced here at grant time and re-checked at execution time (ESD 20).

2. **An approval is bound to one action id.** It is not a token for "restart
   something"; it authorises exactly the proposal that was shown, including its
   target, its diff and its blast radius. A modified proposal needs a new
   approval.

3. **The decision is recorded with the decider.** ``actor_type='human'`` in the
   audit log is what makes "a person authorised this" answerable afterwards. No
   code path in Aegis writes a human approval on behalf of a model.

``more_evidence`` is a first-class third outcome alongside approve and reject.
An operator who cannot yet decide should be able to say so and send the
investigation back for more work, rather than being forced into a binary.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Literal

from aegis.core.clock import SYSTEM_CLOCK, Clock
from aegis.core.errors import DomainError, NotFoundError
from aegis.core.ids import APPROVAL, new_id
from aegis.core.logging import get_logger
from aegis.persistence.audit import AuditEvent, AuditLog
from aegis.persistence.db import Database

log = get_logger(__name__)

Decision = Literal["approved", "rejected", "more_evidence"]


@dataclass(frozen=True, slots=True)
class ApprovalRequest:
    """An open or closed approval as stored."""

    id: str
    action_id: str
    incident_id: str
    requested_at: datetime
    expires_at: datetime
    decision: Decision | None
    decided_by: str | None
    decided_at: datetime | None
    note: str

    def is_expired(self, now: datetime) -> bool:
        return self.decision is None and now >= self.expires_at

    def is_usable(self, now: datetime) -> bool:
        """Only a live, granted approval authorises execution.

        Checked again immediately before the write action runs, not only when
        the approval was granted - the gap between grant and execution is
        exactly where a stale authorisation would slip through.
        """
        return self.decision == "approved" and now < self.expires_at


class ApprovalStore:
    """Create approval requests and record human decisions."""

    __slots__ = ("_audit", "_clock", "_db", "_ttl")

    def __init__(
        self,
        db: Database,
        audit: AuditLog,
        *,
        ttl_seconds: int,
        clock: Clock = SYSTEM_CLOCK,
    ) -> None:
        self._db = db
        self._audit = audit
        self._ttl = ttl_seconds
        self._clock = clock

    async def request(
        self,
        *,
        action_id: str,
        incident_id: str,
        requested_by: str = "agent:remediation_planner",
        ttl_seconds: int | None = None,
        correlation_id: str | None = None,
    ) -> ApprovalRequest:
        """Open an approval request, or return the one already open.

        The partial unique index ``approvals_one_open_idx`` guarantees at most
        one open request per action. Returning the existing one on conflict
        makes a retried workflow idempotent instead of raising an error an
        operator would have to interpret.
        """
        now = self._clock.now()
        expires_at = now + timedelta(seconds=ttl_seconds or self._ttl)

        row = await self._db.fetchrow(
            """
            INSERT INTO approvals (id, action_id, incident_id, requested_at, expires_at)
            VALUES ($1,$2,$3,$4,$5)
            ON CONFLICT DO NOTHING
            RETURNING id, action_id, incident_id, requested_at, expires_at,
                      decision, decided_by, decided_at, note
            """,
            new_id(APPROVAL), action_id, incident_id, now, expires_at,
        )
        if row is None:
            existing = await self.open_for_action(action_id)
            if existing is not None:
                return existing
            raise DomainError(
                "approval could not be opened and none is outstanding",
                context={"action_id": action_id},
            )

        await self._audit.record(
            event_type=AuditEvent.APPROVAL_REQUESTED,
            actor=requested_by,
            actor_type="agent",
            incident_id=incident_id,
            resource_type="action",
            resource_id=action_id,
            detail={"approval_id": row["id"], "expires_at": expires_at.isoformat()},
            correlation_id=correlation_id,
        )
        log.info(
            "approval requested",
            approval_id=row["id"],
            action_id=action_id,
            incident_id=incident_id,
            expires_in_s=(expires_at - now).total_seconds(),
        )
        return self._row(row)

    async def decide(
        self,
        approval_id: str,
        *,
        decision: Decision,
        decided_by: str,
        note: str = "",
        correlation_id: str | None = None,
    ) -> ApprovalRequest:
        """Record a human decision.

        The UPDATE is conditional on the request still being open AND unexpired,
        so a decision arriving after expiry is refused rather than silently
        reviving a lapsed request. Two operators deciding simultaneously produce
        one winner and one ``DomainError``.
        """
        now = self._clock.now()
        row = await self._db.fetchrow(
            """
            UPDATE approvals
               SET decision = $2, decided_by = $3, decided_at = $4, note = $5
             WHERE id = $1 AND decision IS NULL AND expires_at > $4
            RETURNING id, action_id, incident_id, requested_at, expires_at,
                      decision, decided_by, decided_at, note
            """,
            approval_id, decision, decided_by, now, note,
        )
        if row is None:
            current = await self.get(approval_id)
            if current is None:
                raise NotFoundError(
                    "approval not found", context={"approval_id": approval_id}
                )
            if current.decision is not None:
                raise DomainError(
                    f"approval was already {current.decision}",
                    context={"approval_id": approval_id, "decision": current.decision},
                )
            raise DomainError(
                "approval has expired and can no longer be decided",
                context={
                    "approval_id": approval_id,
                    "expired_at": current.expires_at.isoformat(),
                },
            )

        event = {
            "approved": AuditEvent.APPROVAL_GRANTED,
            "rejected": AuditEvent.APPROVAL_REJECTED,
            "more_evidence": AuditEvent.APPROVAL_MORE_EVIDENCE,
        }[decision]
        await self._audit.record(
            event_type=event,
            actor=decided_by,
            actor_type="human",
            incident_id=row["incident_id"],
            resource_type="action",
            resource_id=row["action_id"],
            detail={"approval_id": approval_id, "note": note[:1000]},
            correlation_id=correlation_id,
        )
        log.info(
            "approval decided",
            approval_id=approval_id,
            decision=decision,
            action_id=row["action_id"],
        )
        return self._row(row)

    async def get(self, approval_id: str) -> ApprovalRequest | None:
        row = await self._db.fetchrow(
            """
            SELECT id, action_id, incident_id, requested_at, expires_at,
                   decision, decided_by, decided_at, note
              FROM approvals WHERE id = $1
            """,
            approval_id,
        )
        return self._row(row) if row else None

    async def open_for_action(self, action_id: str) -> ApprovalRequest | None:
        row = await self._db.fetchrow(
            """
            SELECT id, action_id, incident_id, requested_at, expires_at,
                   decision, decided_by, decided_at, note
              FROM approvals WHERE action_id = $1 AND decision IS NULL
            """,
            action_id,
        )
        return self._row(row) if row else None

    async def granted_for_action(self, action_id: str) -> ApprovalRequest | None:
        """The most recent granted approval, if any.

        Returns it regardless of expiry. Callers must test ``is_usable`` - the
        difference between "nobody approved this" and "somebody approved this
        but it lapsed" produces different operator messages and must not be
        collapsed here.
        """
        row = await self._db.fetchrow(
            """
            SELECT id, action_id, incident_id, requested_at, expires_at,
                   decision, decided_by, decided_at, note
              FROM approvals
             WHERE action_id = $1 AND decision = 'approved'
             ORDER BY decided_at DESC
             LIMIT 1
            """,
            action_id,
        )
        return self._row(row) if row else None

    async def pending(self, *, limit: int = 100) -> list[dict[str, Any]]:
        """Open, unexpired requests joined with enough context to decide.

        The approvals queue must give a human the incident, the root cause, the
        exact change, the blast radius and the verification plan without making
        them reconstruct the investigation themselves (PRD approval surface).
        """
        rows = await self._db.fetch(
            """
            SELECT a.id            AS approval_id,
                   a.action_id,
                   a.incident_id,
                   a.requested_at,
                   a.expires_at,
                   r.action_type,
                   r.resource_type,
                   r.resource_id,
                   r.service_id,
                   r.environment,
                   r.reason,
                   r.supporting_evidence,
                   r.blast_radius,
                   r.rollback_plan,
                   r.verification_plan,
                   r.expected_effect,
                   i.title         AS incident_title,
                   i.severity      AS incident_severity,
                   i.confidence    AS incident_confidence,
                   d.statement     AS diagnosis_statement,
                   d.confidence    AS diagnosis_confidence
              FROM approvals a
              JOIN remediation_actions r ON r.id = a.action_id
              JOIN incidents i           ON i.id = a.incident_id
              LEFT JOIN LATERAL (
                   SELECT statement, confidence FROM diagnoses
                    WHERE incident_id = a.incident_id
                    ORDER BY created_at DESC LIMIT 1
              ) d ON TRUE
             WHERE a.decision IS NULL AND a.expires_at > $1
             ORDER BY a.requested_at ASC
             LIMIT $2
            """,
            self._clock.now(), min(limit, 500),
        )
        return [dict(r) for r in rows]

    async def expire_stale(self, *, limit: int = 200) -> int:
        """Close lapsed requests so the queue reflects what is actually open.

        Expired requests are recorded as ``expired``, never as ``rejected``.
        Nobody rejected them, and an audit that claimed otherwise would
        misattribute a decision to a human who never made one.
        """
        rows = await self._db.fetch(
            """
            SELECT id, action_id, incident_id FROM approvals
             WHERE decision IS NULL AND expires_at <= $1
             ORDER BY expires_at
             LIMIT $2
            """,
            self._clock.now(), min(limit, 1000),
        )
        for r in rows:
            await self._audit.record(
                event_type=AuditEvent.APPROVAL_EXPIRED,
                actor="system:approval_reaper",
                actor_type="system",
                incident_id=r["incident_id"],
                resource_type="action",
                resource_id=r["action_id"],
                detail={"approval_id": r["id"]},
            )
        if rows:
            log.info("approval requests expired", count=len(rows))
        return len(rows)

    @staticmethod
    def _row(row: Any) -> ApprovalRequest:
        return ApprovalRequest(
            id=row["id"],
            action_id=row["action_id"],
            incident_id=row["incident_id"],
            requested_at=row["requested_at"],
            expires_at=row["expires_at"],
            decision=row["decision"],
            decided_by=row["decided_by"],
            decided_at=row["decided_at"],
            note=row["note"] or "",
        )


__all__ = ["ApprovalRequest", "ApprovalStore", "Decision"]
