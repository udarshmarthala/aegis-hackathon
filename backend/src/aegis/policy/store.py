"""Kill-switch persistence.

Loading is fail-closed: if the table cannot be read, the returned state reports
every switch engaged. Aegis would rather refuse a safe action than perform an
unsafe one because a query timed out.
"""

from __future__ import annotations

from aegis.core.logging import get_logger
from aegis.domain.enums import ActionType
from aegis.persistence.db import Database
from aegis.policy.killswitch import KillSwitchState

log = get_logger(__name__)


class PolicyStore:
    __slots__ = ("_db",)

    def __init__(self, db: Database) -> None:
        self._db = db

    async def load_kill_switches(self) -> KillSwitchState:
        """Snapshot every engaged switch in one query."""
        try:
            rows = await self._db.fetch(
                "SELECT scope, target, reason FROM kill_switches WHERE engaged = TRUE"
            )
        except Exception as exc:  # noqa: BLE001 - fail closed, never propagate
            log.error("kill switch load failed; failing closed", error=str(exc))
            return KillSwitchState.fail_closed(f"policy store unreadable: {type(exc).__name__}")

        global_engaged = False
        reason = ""
        environments: set[str] = set()
        action_types: set[ActionType] = set()
        services: set[str] = set()

        for row in rows:
            scope, target = row["scope"], row["target"]
            if scope == "global":
                global_engaged = True
                reason = row["reason"]
            elif scope == "environment":
                environments.add(target)
            elif scope == "service":
                services.add(target)
            elif scope == "action_type":
                try:
                    action_types.add(ActionType(target))
                except ValueError:
                    # An unknown action type in the table is a misconfiguration.
                    # Log it rather than silently ignoring a safety control.
                    log.error("kill switch names unknown action type", target=target)

        return KillSwitchState(
            global_engaged=global_engaged,
            environments=frozenset(environments),
            action_types=frozenset(action_types),
            services=frozenset(services),
            reason=reason,
        )

    async def engage(
        self, scope: str, target: str, *, reason: str, actor: str
    ) -> None:
        await self._db.execute(
            """
            INSERT INTO kill_switches (scope, target, engaged, reason, engaged_by)
            VALUES ($1, $2, TRUE, $3, $4)
            ON CONFLICT (scope, target) DO UPDATE
                SET engaged = TRUE, reason = EXCLUDED.reason,
                    engaged_by = EXCLUDED.engaged_by, engaged_at = now()
            """,
            scope, target, reason, actor,
        )
        log.warning("kill switch engaged", scope=scope, target=target, actor=actor)

    async def release(self, scope: str, target: str, *, actor: str) -> None:
        await self._db.execute(
            "UPDATE kill_switches SET engaged = FALSE, engaged_by = $3 "
            "WHERE scope = $1 AND target = $2",
            scope, target, actor,
        )
        log.warning("kill switch released", scope=scope, target=target, actor=actor)

    async def list_all(self) -> list[dict[str, object]]:
        rows = await self._db.fetch(
            "SELECT scope, target, engaged, reason, engaged_by, engaged_at "
            "FROM kill_switches ORDER BY scope, target"
        )
        return [dict(r) for r in rows]

    async def autonomous_actions_last_hour(self, environment: str) -> int:
        """Rate-limit input. Counts only actions that actually executed."""
        value = await self._db.fetchval(
            """
            SELECT count(*) FROM remediation_actions
            WHERE environment = $1
              AND executed_at IS NOT NULL
              AND executed_at > now() - interval '1 hour'
            """,
            environment,
        )
        return int(value or 0)
