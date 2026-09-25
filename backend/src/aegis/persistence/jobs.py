"""Durable workflow queue on Postgres.

Using the database we already depend on, rather than adding a broker, keeps
"create the incident and schedule its investigation" inside one transaction. A
job can therefore never reference an incident that was rolled back, and a worker
crash loses nothing because the job row outlives the process.

Pickup uses ``FOR UPDATE SKIP LOCKED``, which gives competing workers
exactly-once delivery without any coordination between them.
"""

from __future__ import annotations

from typing import Any, Final

import asyncpg

from aegis.core.ids import correlation_id
from aegis.core.logging import get_logger
from aegis.persistence.db import Database

log = get_logger(__name__)


# How long a job's lock survives without renewal. Workers renew every
# ``RENEW_INTERVAL_S``; four missed renewals mean the worker is gone - on
# Fargate Spot a replacement task has a new hostname, so only this reaper can
# hand its work on.
LEASE_SECONDS: Final = 120
RENEW_INTERVAL_S: Final = 30.0


class JobQueue:
    __slots__ = ("_db",)

    def __init__(self, db: Database) -> None:
        self._db = db

    async def enqueue(
        self,
        *,
        incident_id: str,
        kind: str = "investigate",
        payload: dict[str, Any] | None = None,
        conn: asyncpg.Connection | None = None,
    ) -> str | None:
        """Schedule work. Idempotent per (incident, kind).

        The partial unique index means a duplicate enqueue while a job is still
        queued or running is a silent no-op, returning None. Alert storms
        therefore cannot spawn parallel investigations of one incident.
        """
        job_id = f"job_{correlation_id()}"
        query = """
            INSERT INTO workflow_jobs (id, incident_id, kind, payload)
            VALUES ($1, $2, $3, $4)
            ON CONFLICT DO NOTHING
            RETURNING id
        """
        args = (job_id, incident_id, kind, payload or {})
        row = await (conn.fetchrow(query, *args) if conn else self._db.fetchrow(query, *args))
        if row is None:
            log.info("job already pending", incident_id=incident_id, kind=kind)
            return None
        return str(row["id"])

    async def claim(
        self, worker_id: str, *, kinds: list[str] | None = None
    ) -> dict[str, Any] | None:
        """Atomically claim the next runnable job, or return None.

        Everything happens in one statement so two workers cannot claim the same
        row even under contention.
        """
        kind_filter = "AND kind = ANY($2)" if kinds else ""
        args: list[Any] = [worker_id]
        if kinds:
            args.append(kinds)

        row = await self._db.fetchrow(
            f"""
            WITH next AS (
                SELECT id FROM workflow_jobs
                WHERE status = 'queued' AND run_after <= now() {kind_filter}
                ORDER BY run_after
                FOR UPDATE SKIP LOCKED
                LIMIT 1
            )
            UPDATE workflow_jobs j
               SET status = 'running',
                   locked_by = $1,
                   locked_at = now(),
                   attempts = j.attempts + 1,
                   updated_at = now()
              FROM next
             WHERE j.id = next.id
         RETURNING j.id, j.incident_id, j.kind, j.payload, j.attempts, j.max_attempts
            """,
            *args,
        )
        return dict(row) if row else None

    async def complete(self, job_id: str) -> None:
        await self._db.execute(
            "UPDATE workflow_jobs SET status='done', updated_at=now() WHERE id=$1", job_id
        )

    async def fail(self, job_id: str, error: str, *, retry_in_s: int = 30) -> None:
        """Retry with backoff until max_attempts, then park as failed.

        Parking rather than retrying forever is deliberate: a permanently broken
        job should surface to an operator, not spin consuming budget.
        """
        await self._db.execute(
            """
            UPDATE workflow_jobs
               SET status = CASE WHEN attempts >= max_attempts THEN 'failed' ELSE 'queued' END,
                   run_after = now() + make_interval(secs => $3),
                   last_error = $2,
                   locked_by = NULL,
                   locked_at = NULL,
                   updated_at = now()
             WHERE id = $1
            """,
            job_id, error[:2000], retry_in_s,
        )

    async def reap_stale(self, *, older_than_s: int = LEASE_SECONDS) -> int:
        """Requeue jobs whose worker died holding them.

        Without this a hard-killed worker would leave its job 'running' forever.
        """
        result = await self._db.execute(
            """
            UPDATE workflow_jobs
               SET status='queued', locked_by=NULL, locked_at=NULL, updated_at=now()
             WHERE status='running'
               AND locked_at < now() - make_interval(secs => $1)
            """,
            older_than_s,
        )
        count = int(result.split()[-1]) if result.startswith("UPDATE") else 0
        if count:
            log.warning("reaped stale jobs", count=count)
        return count

    async def renew(self, worker_id: str) -> int:
        """Refresh the lock on every job this worker is running.

        A lease, not a claim-forever: a live worker renews well inside
        ``LEASE_SECONDS``, so the reaper can treat any lock older than that as
        abandoned without ever stealing work from a slow but healthy worker.
        """
        result = await self._db.execute(
            """
            UPDATE workflow_jobs SET locked_at = now(), updated_at = now()
             WHERE status = 'running' AND locked_by = $1
            """,
            worker_id,
        )
        return int(result.split()[-1]) if result.startswith("UPDATE") else 0

    async def reclaim_own(self, worker_id: str) -> int:
        """Requeue jobs a previous incarnation of *this* worker was holding.

        Run once at startup, before the first claim. A container restarted after
        a hard kill keeps its hostname, and therefore its worker id, so anything
        still 'running' under that id belongs to a process that no longer
        exists. Waiting for ``reap_stale`` would strand the incident for fifteen
        minutes; the checkpoint makes resuming it immediately safe.

        Never touches another worker's jobs: a live peer is indistinguishable
        from a dead one here, which is exactly why the reaper waits.
        """
        result = await self._db.execute(
            """
            UPDATE workflow_jobs
               SET status='queued', locked_by=NULL, locked_at=NULL,
                   run_after=now(), updated_at=now()
             WHERE status='running' AND locked_by = $1
            """,
            worker_id,
        )
        count = int(result.split()[-1]) if result.startswith("UPDATE") else 0
        if count:
            log.warning("reclaimed jobs from a previous run of this worker",
                        worker_id=worker_id, count=count)
        return count

    async def stats(self) -> dict[str, int]:
        rows = await self._db.fetch(
            "SELECT status, count(*) AS n FROM workflow_jobs GROUP BY status"
        )
        return {r["status"]: int(r["n"]) for r in rows}
