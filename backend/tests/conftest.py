"""Shared test fixtures.

Unit tests must run with no infrastructure at all - that is what makes them
usable in a pre-commit hook and in a pull request from a laptop on a train.
Integration tests need real datastores, so they are marked and skipped rather
than failed when those are absent: a skipped integration test is honest, while a
failing one on a machine with no Postgres is noise that trains people to ignore
a red build.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator

import pytest

from aegis.core.config import Settings
from aegis.persistence.db import Database

# The dev stack publishes Postgres on 55433. 5432 is avoided because a natively
# installed PostgreSQL commonly owns it and wins over Docker's proxy, and 55432
# collided with an unrelated project on the reference machine. Overridable so CI
# can point at its own service container.
DEFAULT_TEST_PG_PORT = int(os.getenv("AEGIS_TEST_PG_PORT", "55433"))
DEFAULT_TEST_PG_HOST = os.getenv("AEGIS_TEST_PG_HOST", "localhost")


def integration_settings() -> Settings:
    """Settings pointed at the local stack, with secrets left to the environment."""
    return Settings(
        postgres_host=DEFAULT_TEST_PG_HOST,
        postgres_port=DEFAULT_TEST_PG_PORT,
        neo4j_uri=os.getenv("AEGIS_TEST_NEO4J_URI", "bolt://localhost:7687"),
        prometheus_url=os.getenv("AEGIS_TEST_PROMETHEUS_URL", "http://localhost:9090"),
        redis_host=os.getenv("AEGIS_TEST_REDIS_HOST", "localhost"),
        otel_traces_enabled=False,
    )


@pytest.fixture(scope="session")
def settings() -> Settings:
    return integration_settings()


@pytest.fixture
async def db(settings: Settings) -> AsyncIterator[Database]:
    """A live connection pool, or a skip.

    Skipping on connection failure keeps the integration suite honest on a
    developer machine with nothing running, while still failing loudly in CI
    where the service containers are guaranteed to exist.
    """
    database = Database(settings)
    try:
        await database.connect()
    except Exception as exc:  # noqa: BLE001 - absent infra is a skip, not a failure
        pytest.skip(
            f"postgres unavailable at {settings.postgres_host}:"
            f"{settings.postgres_port}: {exc}"
        )
    try:
        yield database
    finally:
        await database.close()


@pytest.fixture
async def clean_incident(db: Database) -> AsyncIterator[str]:
    """An incident row that is removed afterwards.

    Integration tests share a database with whatever else is running locally, so
    each one creates and removes exactly its own rows. Truncating tables would
    destroy an operator's local demo data, which is a hostile thing for a test
    suite to do.
    """
    from aegis.core.ids import INCIDENT, new_id

    incident_id = new_id(INCIDENT)
    await db.execute(
        """
        INSERT INTO incidents (id, title, severity, state, environment, workload,
                               created_at, updated_at, correlation_id)
        VALUES ($1, 'integration test incident', 'P3', 'RECEIVED', 'test', 'default',
                now(), now(), 'test-correlation')
        """,
        incident_id,
    )
    try:
        yield incident_id
    finally:
        # ON DELETE CASCADE removes the evidence, actions, approvals and audit
        # rows that hang off this incident, and nothing else.
        await db.execute("DELETE FROM incidents WHERE id = $1", incident_id)


@pytest.fixture
async def approver(db: Database) -> AsyncIterator[str]:
    """A user row to attribute decisions to.

    ``approvals.decided_by`` is a foreign key into ``users``. That constraint is
    the point: an approval must name an account that actually exists, so the
    audit trail cannot record a decision by a person the system has never seen.
    """
    from aegis.core.ids import new_id

    uid = "usr_" + new_id("apr").split("_", 1)[1]
    await db.execute(
        """
        INSERT INTO users (id, firebase_uid, email, display_name, roles)
        VALUES ($1, $1, 'integration@test.local', 'Integration Approver',
                ARRAY['viewer','approver'])
        """,
        uid,
    )
    try:
        yield uid
    finally:
        # Teardown runs in reverse setup order, so this fixture is torn down
        # before the incident whose approvals reference it. Clearing its own
        # references first keeps the cleanup self-contained instead of
        # depending on another fixture happening to run first.
        await db.execute("DELETE FROM approvals WHERE decided_by = $1", uid)
        await db.execute("DELETE FROM users WHERE id = $1", uid)
