"""Neo4j client - a deliberately soft dependency.

Every method converts a driver failure into ``SourceUnavailable``. The caller
records an evidence gap and continues with reduced confidence. Losing topology
degrades blast-radius analysis; it does not stop an investigation (ESD 34).

All Cypher is parameterised. Labels and relationship types are drawn from module
constants, never from caller input, so no path exists to inject a clause.
"""

from __future__ import annotations

from typing import Any

from aegis.core.config import Settings
from aegis.core.errors import SourceNotConfigured, SourceUnavailable, is_unset
from aegis.core.logging import get_logger
from aegis.core.resilience import Bulkhead, guarded_call

log = get_logger(__name__)


class Neo4jClient:
    __slots__ = ("_driver", "_settings", "_bulkhead")

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._driver: Any = None
        self._bulkhead = Bulkhead("neo4j", limit=8)

    @property
    def configured(self) -> bool:
        """False when ``NEO4J_URI`` is empty - Neo4j is not deployed."""
        return not is_unset(self._settings.neo4j_uri)

    def _require_configured(self) -> None:
        # Checked before ``guarded_call``: an absent deployment is not an
        # outage, so it is neither retried nor counted against the breaker, and
        # no driver is built for a URI that names nothing.
        if not self.configured:
            raise SourceNotConfigured.for_setting("neo4j", "NEO4J_URI")

    def _ensure_driver(self) -> Any:
        self._require_configured()
        if self._driver is None:
            from neo4j import AsyncGraphDatabase, NotificationDisabledClassification

            self._driver = AsyncGraphDatabase.driver(
                self._settings.neo4j_uri,
                auth=(
                    self._settings.neo4j_user,
                    self._settings.neo4j_password.get_secret_value(),
                ),
                max_connection_pool_size=16,
                connection_acquisition_timeout=5.0,
                max_transaction_retry_time=5.0,
                # The topology graph is built incrementally, so a traversal
                # naturally runs ahead of ingestion: querying DEPLOYED_AS
                # before any deployment has been ingested is an empty result,
                # not a fault. Neo4j reports each one as an UNRECOGNISED
                # notification, and the driver logs it as a warning - tens of
                # lines per investigation, none actionable. Left on, that noise
                # buries the warnings that do matter over a long-running
                # process. Only this classification is silenced; deprecations,
                # performance and security notifications still surface.
                notifications_disabled_classifications=[
                    NotificationDisabledClassification.UNRECOGNIZED,
                ],
            )
        return self._driver

    async def close(self) -> None:
        if self._driver is not None:
            try:
                await self._driver.close()
            finally:
                self._driver = None

    async def healthy(self) -> bool:
        try:
            await self.run("RETURN 1 AS ok", {})
        except Exception:  # noqa: BLE001
            return False
        return True

    async def run(self, cypher: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        """Execute read-only Cypher behind timeout, retry, breaker and bulkhead."""
        self._require_configured()

        async def _execute() -> list[dict[str, Any]]:
            driver = self._ensure_driver()
            async with driver.session(database=self._settings.neo4j_database) as session:
                result = await session.run(cypher, params)
                return [dict(record) async for record in result]

        try:
            return await guarded_call(
                _execute,
                dependency="neo4j",
                timeout_s=self._settings.source_timeout_s,
                attempts=2,
                bulkhead=self._bulkhead,
            )
        except Exception as exc:
            raise SourceUnavailable(
                f"neo4j query failed: {type(exc).__name__}",
                context={"dependency": "neo4j"},
            ) from exc

    async def write(self, cypher: str, params: dict[str, Any]) -> None:
        """Idempotent graph writes (always MERGE) for topology ingestion."""
        self._require_configured()

        async def _execute() -> None:
            driver = self._ensure_driver()
            async with driver.session(database=self._settings.neo4j_database) as session:
                await session.run(cypher, params)

        try:
            await guarded_call(
                _execute,
                dependency="neo4j",
                timeout_s=self._settings.source_timeout_s,
                attempts=2,
                bulkhead=self._bulkhead,
            )
        except Exception as exc:
            raise SourceUnavailable(
                f"neo4j write failed: {type(exc).__name__}",
                context={"dependency": "neo4j"},
            ) from exc
