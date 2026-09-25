"""Resource leases - the concurrency arbiter for write actions.

Postgres decides, not the application. ``resource_leases`` carries a partial
unique index on ``(resource_type, resource_id) WHERE released_at IS NULL``, so
two workers racing to act on the same instance produce one winner and one
``LeaseConflict`` at the database level. Any scheme that checked for a lease and
then inserted one would have a window between the two statements; this has none.

Leases expire. A worker that dies mid-action must not hold a resource forever,
so every lease carries ``expires_at`` and acquisition treats an expired lease as
absent - it is reaped in the same transaction that takes the new one.

Expiry is deliberately NOT a licence to act. It only frees the lock. Whether the
original action left the resource in a partial state is a separate question the
verification engine answers.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from aegis.core.clock import SYSTEM_CLOCK, Clock
from aegis.core.errors import LeaseConflict
from aegis.core.ids import LEASE, new_id
from aegis.core.logging import get_logger
from aegis.domain.models import ResourceRef
from aegis.persistence.audit import AuditEvent, AuditLog
from aegis.persistence.db import Database

log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class Lease:
    """Proof that this worker, and only this worker, may act on a resource."""

    id: str
    resource_type: str
    resource_id: str
    holder: str
    incident_id: str | None
    acquired_at: datetime
    expires_at: datetime

    def is_expired(self, now: datetime) -> bool:
        return now >= self.expires_at

    @property
    def key(self) -> tuple[str, str]:
        return (self.resource_type, self.resource_id)


class LeaseManager:
    """Acquire, renew, release and reap resource leases."""

    __slots__ = ("_audit", "_clock", "_db", "_default_ttl")

    def __init__(
        self,
        db: Database,
        audit: AuditLog,
        *,
        default_ttl_seconds: int,
        clock: Clock = SYSTEM_CLOCK,
    ) -> None:
        self._db = db
        self._audit = audit
        self._clock = clock
        self._default_ttl = default_ttl_seconds

    async def acquire(
        self,
        target: ResourceRef,
        *,
        holder: str,
        incident_id: str | None = None,
        ttl_seconds: int | None = None,
        correlation_id: str | None = None,
    ) -> Lease:
        """Take the lease or raise ``LeaseConflict``.

        Reaping and insertion happen in one transaction so that the window
        between "this lease looks expired" and "I have taken it" does not exist.
        The audit row is written inside the same transaction: an audit entry for
        a lease that was rolled back would be a false record.
        """
        ttl = ttl_seconds if ttl_seconds is not None else self._default_ttl
        now = self._clock.now()
        expires_at = now + timedelta(seconds=ttl)
        lease_id = new_id(LEASE)

        async with self._db.transaction() as conn:
            # Reap anything already past its deadline for this exact resource.
            reaped = await conn.fetchval(
                """
                UPDATE resource_leases
                   SET released_at = now()
                 WHERE resource_type = $1 AND resource_id = $2
                   AND released_at IS NULL AND expires_at <= $3
                RETURNING id
                """,
                target.resource_type, target.resource_id, now,
            )
            if reaped:
                log.warning(
                    "reaped expired lease",
                    lease_id=reaped,
                    resource_type=target.resource_type,
                    resource_id=target.resource_id,
                )
                await self._audit.record_in(
                    conn,
                    event_type=AuditEvent.LEASE_EXPIRED,
                    actor="system:lease_reaper",
                    actor_type="system",
                    incident_id=incident_id,
                    resource_type=target.resource_type,
                    resource_id=target.resource_id,
                    detail={"lease_id": reaped},
                    correlation_id=correlation_id,
                )

            row = await conn.fetchrow(
                """
                INSERT INTO resource_leases
                    (id, resource_type, resource_id, holder, incident_id,
                     acquired_at, expires_at)
                VALUES ($1,$2,$3,$4,$5,$6,$7)
                ON CONFLICT DO NOTHING
                RETURNING id, acquired_at, expires_at
                """,
                lease_id, target.resource_type, target.resource_id,
                holder, incident_id, now, expires_at,
            )

            if row is None:
                # The partial unique index rejected the insert: someone else
                # holds a live lease. Read who, so the message is actionable.
                current = await conn.fetchrow(
                    """
                    SELECT holder, incident_id, expires_at FROM resource_leases
                     WHERE resource_type = $1 AND resource_id = $2
                       AND released_at IS NULL
                    """,
                    target.resource_type, target.resource_id,
                )
                held_by = current["holder"] if current else "unknown"
                await self._audit.record_in(
                    conn,
                    event_type=AuditEvent.LEASE_DENIED,
                    actor=holder,
                    actor_type="agent",
                    incident_id=incident_id,
                    resource_type=target.resource_type,
                    resource_id=target.resource_id,
                    detail={"held_by": held_by},
                    correlation_id=correlation_id,
                )
                raise LeaseConflict(
                    f"{target.resource_type}:{target.resource_id} is leased by {held_by}",
                    context={
                        "resource_type": target.resource_type,
                        "resource_id": target.resource_id,
                        "held_by": held_by,
                        "held_until": current["expires_at"].isoformat() if current else None,
                    },
                )

            await self._audit.record_in(
                conn,
                event_type=AuditEvent.LEASE_ACQUIRED,
                actor=holder,
                actor_type="agent",
                incident_id=incident_id,
                resource_type=target.resource_type,
                resource_id=target.resource_id,
                detail={"lease_id": row["id"], "ttl_seconds": ttl},
                correlation_id=correlation_id,
            )

        log.info(
            "lease acquired",
            lease_id=row["id"],
            resource_type=target.resource_type,
            resource_id=target.resource_id,
            holder=holder,
            ttl_s=ttl,
        )
        return Lease(
            id=row["id"],
            resource_type=target.resource_type,
            resource_id=target.resource_id,
            holder=holder,
            incident_id=incident_id,
            acquired_at=row["acquired_at"],
            expires_at=row["expires_at"],
        )

    async def renew(self, lease: Lease, *, ttl_seconds: int | None = None) -> Lease:
        """Extend a lease this holder still owns.

        Renewal is conditional on the lease being unreleased AND unexpired. A
        lapsed lease cannot be revived: another worker may already have taken
        the resource, and silently extending would give two holders at once.
        """
        ttl = ttl_seconds if ttl_seconds is not None else self._default_ttl
        now = self._clock.now()
        new_expiry = now + timedelta(seconds=ttl)
        row = await self._db.fetchrow(
            """
            UPDATE resource_leases
               SET expires_at = $2
             WHERE id = $1 AND released_at IS NULL AND expires_at > $3
            RETURNING expires_at
            """,
            lease.id, new_expiry, now,
        )
        if row is None:
            raise LeaseConflict(
                f"lease {lease.id} is no longer held and cannot be renewed",
                context={"lease_id": lease.id, "resource_id": lease.resource_id},
            )
        return Lease(
            id=lease.id,
            resource_type=lease.resource_type,
            resource_id=lease.resource_id,
            holder=lease.holder,
            incident_id=lease.incident_id,
            acquired_at=lease.acquired_at,
            expires_at=row["expires_at"],
        )

    async def release(self, lease: Lease, *, correlation_id: str | None = None) -> None:
        """Release a lease.

        Idempotent and non-raising. Release runs in a ``finally`` on the
        execution path, where raising would mask the original error.
        """
        try:
            await self._db.execute(
                """
                UPDATE resource_leases SET released_at = now()
                 WHERE id = $1 AND released_at IS NULL
                """,
                lease.id,
            )
        except Exception as exc:  # noqa: BLE001 - runs in a finally block
            log.error(
                "lease release failed; it will be reaped at expiry",
                lease_id=lease.id,
                resource_id=lease.resource_id,
                error=str(exc),
            )
            return

        await self._audit.record(
            event_type=AuditEvent.LEASE_RELEASED,
            actor=lease.holder,
            actor_type="agent",
            incident_id=lease.incident_id,
            resource_type=lease.resource_type,
            resource_id=lease.resource_id,
            detail={"lease_id": lease.id},
            correlation_id=correlation_id,
        )
        log.info("lease released", lease_id=lease.id, resource_id=lease.resource_id)

    async def is_held(self, target: ResourceRef) -> bool:
        """Whether a live lease exists. Feeds the policy concurrency gate.

        Expired-but-unreleased rows do not count as held: treating a dead
        worker's lease as a permanent block would make one crash disable
        remediation for that resource until a human intervened.
        """
        held = await self._db.fetchval(
            """
            SELECT EXISTS (
                SELECT 1 FROM resource_leases
                 WHERE resource_type = $1 AND resource_id = $2
                   AND released_at IS NULL AND expires_at > $3
            )
            """,
            target.resource_type, target.resource_id, self._clock.now(),
        )
        return bool(held)

    async def reap_expired(self, *, limit: int = 200) -> int:
        """Background sweep. Returns how many leases were reaped.

        Bounded per call so a large backlog is drained over several passes
        rather than in one statement holding locks on hundreds of rows.
        """
        rows = await self._db.fetch(
            """
            UPDATE resource_leases SET released_at = now()
             WHERE id IN (
                 SELECT id FROM resource_leases
                  WHERE released_at IS NULL AND expires_at <= $1
                  ORDER BY expires_at
                  LIMIT $2
                  FOR UPDATE SKIP LOCKED
             )
            RETURNING id, resource_type, resource_id, incident_id
            """,
            self._clock.now(), min(limit, 1000),
        )
        for r in rows:
            await self._audit.record(
                event_type=AuditEvent.LEASE_EXPIRED,
                actor="system:lease_reaper",
                actor_type="system",
                incident_id=r["incident_id"],
                resource_type=r["resource_type"],
                resource_id=r["resource_id"],
                detail={"lease_id": r["id"]},
            )
        if rows:
            log.info("reaped expired leases", count=len(rows))
        return len(rows)

    async def active(self, *, limit: int = 200) -> list[Lease]:
        """Live leases, for the operator concurrency view."""
        rows = await self._db.fetch(
            """
            SELECT id, resource_type, resource_id, holder, incident_id,
                   acquired_at, expires_at
              FROM resource_leases
             WHERE released_at IS NULL AND expires_at > $1
             ORDER BY acquired_at DESC
             LIMIT $2
            """,
            self._clock.now(), min(limit, 500),
        )
        return [
            Lease(
                id=r["id"],
                resource_type=r["resource_type"],
                resource_id=r["resource_id"],
                holder=r["holder"],
                incident_id=r["incident_id"],
                acquired_at=r["acquired_at"],
                expires_at=r["expires_at"],
            )
            for r in rows
        ]


__all__ = ["Lease", "LeaseManager"]
