"""Job leases against real Postgres: renewed work is never stolen, lapsed work is.

The reaper's threshold dropped from fifteen minutes to the lease length so a
Spot-interrupted worker's incident resumes within a couple of minutes. That is
only safe if a live worker's renewal keeps its jobs out of the reaper's reach.
"""

from __future__ import annotations

import pytest

from aegis.persistence.db import Database
from aegis.persistence.jobs import LEASE_SECONDS, JobQueue

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

# A kind no worker claims, so a worker running against the same database
# cannot take the job out from under the test.
KIND = "lease_test"


async def _age_lock(db: Database, job_id: str, seconds: int) -> None:
    await db.execute(
        "UPDATE workflow_jobs SET locked_at = now() - make_interval(secs => $2) WHERE id = $1",
        job_id,
        seconds,
    )


async def test_a_renewed_job_survives_the_reaper(db: Database, clean_incident: str) -> None:
    queue = JobQueue(db)
    job_id = await queue.enqueue(incident_id=clean_incident, kind=KIND)
    claimed = await queue.claim("worker-a", kinds=[KIND])
    assert claimed is not None and claimed["id"] == job_id

    await _age_lock(db, job_id, LEASE_SECONDS + 60)
    assert await queue.renew("worker-a") == 1
    await queue.reap_stale()

    status = await db.fetchval("SELECT status FROM workflow_jobs WHERE id = $1", job_id)
    assert status == "running"
    await queue.complete(job_id)


async def test_a_lapsed_lease_is_requeued(db: Database, clean_incident: str) -> None:
    queue = JobQueue(db)
    job_id = await queue.enqueue(incident_id=clean_incident, kind=KIND)
    assert await queue.claim("worker-b", kinds=[KIND]) is not None

    await _age_lock(db, job_id, LEASE_SECONDS + 5)
    assert await queue.reap_stale() >= 1

    row = await db.fetchrow(
        "SELECT status, locked_by FROM workflow_jobs WHERE id = $1", job_id
    )
    assert row["status"] == "queued" and row["locked_by"] is None
    await db.execute("DELETE FROM workflow_jobs WHERE id = $1", job_id)


async def test_renewal_never_touches_another_workers_jobs(
    db: Database, clean_incident: str
) -> None:
    queue = JobQueue(db)
    job_id = await queue.enqueue(incident_id=clean_incident, kind=KIND)
    assert await queue.claim("worker-c", kinds=[KIND]) is not None

    assert await queue.renew("worker-d") == 0
    await db.execute("DELETE FROM workflow_jobs WHERE id = $1", job_id)
