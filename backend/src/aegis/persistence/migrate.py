"""Migration runner.

Ordered SQL files applied inside a Postgres advisory lock, so several API and
worker replicas booting at once cannot race each other into a half-applied
schema. Each file is applied exactly once and recorded with its checksum; a
changed file that was already applied is a hard error rather than a silent
divergence between environments.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import asyncpg

from aegis.core.errors import ConfigError
from aegis.core.logging import get_logger
from aegis.persistence.db import Database

log = get_logger(__name__)

# Arbitrary but fixed: every Aegis process contends on this one lock id.
_ADVISORY_LOCK_ID = 0x4145_4749  # "AEGI"

_MIGRATIONS_TABLE = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    filename    TEXT PRIMARY KEY,
    checksum    TEXT NOT NULL,
    applied_at  TIMESTAMPTZ NOT NULL DEFAULT now()
)
"""


def _migrations_dir() -> Path:
    """Locate migrations/ relative to the installed package."""
    # src/aegis/persistence/migrate.py -> backend/migrations
    candidate = Path(__file__).resolve().parents[3] / "migrations"
    if candidate.is_dir():
        return candidate
    # Container layout: /app/migrations
    fallback = Path("/app/migrations")
    if fallback.is_dir():
        return fallback
    raise ConfigError(f"migrations directory not found (looked in {candidate} and {fallback})")


def _checksum(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


async def run_migrations(db: Database) -> list[str]:
    """Apply pending migrations. Returns the filenames applied this run."""
    directory = _migrations_dir()
    files = sorted(p for p in directory.glob("*.sql") if p.is_file())
    if not files:
        log.warning("no migration files found", directory=str(directory))
        return []

    applied: list[str] = []

    async with db.acquire() as conn:
        # Blocks until any other booting replica finishes; released on unlock.
        await conn.execute("SELECT pg_advisory_lock($1)", _ADVISORY_LOCK_ID)
        try:
            await conn.execute(_MIGRATIONS_TABLE)
            known: dict[str, str] = {
                r["filename"]: r["checksum"]
                for r in await conn.fetch("SELECT filename, checksum FROM schema_migrations")
            }

            for path in files:
                sql = path.read_text(encoding="utf-8")
                digest = _checksum(sql)
                name = path.name

                if name in known:
                    if known[name] != digest:
                        raise ConfigError(
                            f"migration {name} changed after it was applied; "
                            "add a new migration instead of editing history",
                            context={"expected": known[name], "found": digest},
                        )
                    continue

                log.info("applying migration", migration=name)
                # Each file is its own transaction: a failure leaves previously
                # applied files intact and this one fully rolled back.
                async with conn.transaction():
                    await conn.execute(sql)
                    await conn.execute(
                        "INSERT INTO schema_migrations (filename, checksum) VALUES ($1, $2)",
                        name,
                        digest,
                    )
                applied.append(name)
        except asyncpg.PostgresError as exc:
            raise ConfigError(
                f"migration failed: {exc}", context={"error": str(exc)}
            ) from exc
        finally:
            await conn.execute("SELECT pg_advisory_unlock($1)", _ADVISORY_LOCK_ID)

    if applied:
        log.info("migrations applied", count=len(applied), files=applied)
    else:
        log.info("schema up to date", checked=len(files))
    return applied
