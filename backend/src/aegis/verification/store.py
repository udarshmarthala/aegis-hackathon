"""Verification persistence.

Runs and their individual claims are stored in separate tables. That split is
what lets the incident view show "latency did not regress" as its own row with
its own before/after numbers, instead of a single opaque pass or fail that an
operator has to take on trust.

Nothing here updates a completed run. A verification is a measurement at a point
in time; re-running produces a new row so the history of what was believed, and
when, stays intact.
"""

from __future__ import annotations

from typing import Any

from aegis.core.ids import new_id
from aegis.core.logging import get_logger
from aegis.persistence.db import Database
from aegis.verification.engine import VerificationRun

log = get_logger(__name__)


class VerificationStore:
    __slots__ = ("_db",)

    def __init__(self, db: Database) -> None:
        self._db = db

    async def save(self, run: VerificationRun) -> str:
        """Persist a run and its claims atomically.

        One transaction: a run row without its claims would render as a verdict
        nobody can inspect, which is worse than no row at all.
        """
        async with self._db.transaction() as conn:
            await conn.execute(
                """
                INSERT INTO verification_runs
                    (id, incident_id, action_id, kind, passed, checks, notes,
                     verdict, baseline_window, observation_window,
                     started_at, completed_at)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12)
                """,
                run.id, run.incident_id, run.action_id, run.kind, run.passed,
                [r.as_json() for r in run.results], run.notes, run.verdict.value,
                {
                    "start": run.baseline.window_start,
                    "end": run.baseline.window_end,
                    "captured_at": run.baseline.captured_at.isoformat(),
                    "missing": run.baseline.missing,
                },
                {"claims": len(run.results)},
                run.started_at, run.completed_at,
            )
            for result in run.results:
                await conn.execute(
                    """
                    INSERT INTO verification_claims
                        (id, verification_id, incident_id, claim, test_kind,
                         test_spec, outcome, before_value, after_value, threshold,
                         evidence_ids, detail, observed_at)
                    VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13)
                    """,
                    "vc_" + new_id("ver").split("_", 1)[1],
                    run.id, run.incident_id, result.claim.statement,
                    result.claim.test.kind.value, result.claim.test.as_json(),
                    result.outcome.value, result.before_value, result.after_value,
                    result.threshold, list(result.evidence_ids), result.detail,
                    result.observed_at,
                )
        log.info(
            "verification persisted",
            verification_id=run.id,
            incident_id=run.incident_id,
            verdict=run.verdict.value,
        )
        return run.id

    async def for_incident(self, incident_id: str, *, limit: int = 50) -> list[dict[str, Any]]:
        rows = await self._db.fetch(
            """
            SELECT id, action_id, kind, passed, verdict, checks, notes,
                   baseline_window, started_at, completed_at
              FROM verification_runs
             WHERE incident_id = $1
             ORDER BY completed_at DESC
             LIMIT $2
            """,
            incident_id, min(limit, 200),
        )
        return [dict(r) for r in rows]

    async def for_action(self, action_id: str) -> list[dict[str, Any]]:
        rows = await self._db.fetch(
            """
            SELECT id, incident_id, kind, passed, verdict, checks, notes,
                   started_at, completed_at
              FROM verification_runs
             WHERE action_id = $1
             ORDER BY completed_at DESC
            """,
            action_id,
        )
        return [dict(r) for r in rows]

    async def claims_for(self, verification_id: str) -> list[dict[str, Any]]:
        rows = await self._db.fetch(
            """
            SELECT claim, test_kind, test_spec, outcome, before_value, after_value,
                   threshold, evidence_ids, detail, observed_at
              FROM verification_claims
             WHERE verification_id = $1
             ORDER BY observed_at ASC
            """,
            verification_id,
        )
        return [dict(r) for r in rows]

    async def latest_for_incident(self, incident_id: str) -> dict[str, Any] | None:
        rows = await self.for_incident(incident_id, limit=1)
        return rows[0] if rows else None


__all__ = ["VerificationStore"]
