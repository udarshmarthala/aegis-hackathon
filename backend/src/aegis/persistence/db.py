"""asyncpg connection pool and unit of work.

One pool per process, created at startup and closed on shutdown. Every
connection gets a statement timeout at checkout, so a pathological query can
never pin a connection indefinitely - the single most common way a service that
has run fine for months suddenly stops accepting requests.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import asyncpg

from aegis.core.config import Settings
from aegis.core.errors import ExternalServiceError
from aegis.core.logging import get_logger

log = get_logger(__name__)


async def _init_connection(conn: asyncpg.Connection) -> None:
    """Per-connection setup applied to every pooled connection."""
    # asyncpg returns JSONB as str by default; decode once here so repositories
    # never sprinkle json.loads across the codebase.
    await conn.set_type_codec(
        "jsonb", encoder=json.dumps, decoder=json.loads, schema="pg_catalog"
    )
    await conn.set_type_codec(
        "json", encoder=json.dumps, decoder=json.loads, schema="pg_catalog"
    )


class Database:
    """Owns the pool lifecycle. Injected, never imported as a global."""

    __slots__ = ("_pool", "_settings")

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._pool: asyncpg.Pool | None = None

    @property
    def pool(self) -> asyncpg.Pool:
        if self._pool is None:
            raise ExternalServiceError("database pool is not initialised", code="DB_NOT_READY")
        return self._pool

    @property
    def is_ready(self) -> bool:
        return self._pool is not None

    async def connect(self) -> None:
        if self._pool is not None:
            return
        s = self._settings
        self._pool = await asyncpg.create_pool(
            host=s.postgres_host,
            port=s.postgres_port,
            database=s.postgres_db,
            user=s.postgres_user,
            password=s.postgres_password.get_secret_value(),
            min_size=s.postgres_pool_min,
            max_size=s.postgres_pool_max,
            command_timeout=s.postgres_statement_timeout_ms / 1000,
            max_inactive_connection_lifetime=300.0,
            init=_init_connection,
            server_settings={
                "application_name": f"aegis-{s.otel_service_name}",
                "statement_timeout": str(s.postgres_statement_timeout_ms),
                # Guards against a transaction left open by a crashed caller
                # holding locks forever.
                "idle_in_transaction_session_timeout": "30000",
            },
        )
        log.info(
            "database pool ready",
            host=s.postgres_host,
            database=s.postgres_db,
            min_size=s.postgres_pool_min,
            max_size=s.postgres_pool_max,
        )

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None
            log.info("database pool closed")

    @asynccontextmanager
    async def acquire(self) -> AsyncIterator[asyncpg.Connection]:
        async with self.pool.acquire() as conn:
            yield conn

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[asyncpg.Connection]:
        """One transaction per request or per workflow node.

        Nothing is committed unless the whole block succeeds, which is what makes
        'create incident and enqueue its job' atomic - a job can never reference
        an incident that was rolled back.
        """
        async with self.pool.acquire() as conn, conn.transaction():
            yield conn

    async def healthy(self) -> bool:
        """Cheap liveness probe used by /health."""
        if self._pool is None:
            return False
        try:
            async with self.pool.acquire() as conn:
                return bool(await conn.fetchval("SELECT 1") == 1)
        except (asyncpg.PostgresError, OSError, TimeoutError) as exc:
            log.warning("database health check failed", error=str(exc))
            return False

    async def fetch(self, query: str, *args: Any) -> list[asyncpg.Record]:
        async with self.acquire() as conn:
            rows: list[asyncpg.Record] = await conn.fetch(query, *args)
            return rows

    async def fetchrow(self, query: str, *args: Any) -> asyncpg.Record | None:
        async with self.acquire() as conn:
            return await conn.fetchrow(query, *args)

    async def fetchval(self, query: str, *args: Any) -> Any:
        async with self.acquire() as conn:
            return await conn.fetchval(query, *args)

    async def execute(self, query: str, *args: Any) -> str:
        async with self.acquire() as conn:
            status: str = await conn.execute(query, *args)
            return status
