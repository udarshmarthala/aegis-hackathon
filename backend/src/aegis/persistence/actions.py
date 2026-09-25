"""Remediation action and policy-decision persistence.

Two design points carry real safety weight:

* **The idempotency key is a UNIQUE column, not an application check.** A worker
  that crashes between acting and recording will retry the same proposal.
  ``propose`` returns the existing row on conflict, so the retry observes the
  original action instead of mutating production a second time (ESD 19).

* **Policy decisions are stored in their own table, keyed by action.** Risk tier
  and effect stay independently auditable, and an action can accumulate several
  decisions over its life (proposed, re-evaluated after approval) without any of
  them being overwritten.

State changes go through ``transition``, which is conditional on the expected
current state. A concurrent writer therefore loses rather than silently
clobbering, which is what keeps the action state machine honest.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from aegis.core.errors import DomainError, NotFoundError
from aegis.core.ids import new_id
from aegis.core.logging import get_logger
from aegis.domain.enums import ActionState, ActionType, PolicyEffect, RiskTier
from aegis.domain.models import ActionProposal, PolicyDecision
from aegis.persistence.db import Database

log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class StoredAction:
    """A remediation action as persisted, independent of the proposal object."""

    id: str
    incident_id: str
    action_type: ActionType
    state: ActionState
    resource_type: str
    resource_id: str
    service_id: str | None
    environment: str
    reason: str
    supporting_evidence: list[str]
    expected_effect: dict[str, Any]
    blast_radius: dict[str, Any]
    rollback_plan: dict[str, Any] | None
    verification_plan: dict[str, Any]
    arguments: dict[str, Any]
    idempotency_key: str
    proposed_by: str
    executed_at: datetime | None
    completed_at: datetime | None
    result: dict[str, Any] | None
    error: str | None
    created_at: datetime
    updated_at: datetime

    @property
    def is_terminal(self) -> bool:
        return self.state.is_terminal


def _dump(model: Any) -> dict[str, Any]:
    """Serialise a pydantic model to plain JSON-safe types for JSONB."""
    if model is None:
        return {}
    data: dict[str, Any] = json.loads(model.model_dump_json())
    return data


class ActionRepository:
    __slots__ = ("_db",)

    def __init__(self, db: Database) -> None:
        self._db = db

    async def propose(self, proposal: ActionProposal) -> tuple[StoredAction, bool]:
        """Persist a proposal. Returns (action, created).

        ``created`` is False when the idempotency key already existed. Callers
        must treat that as "this work was already proposed" and must not execute
        a second time.
        """
        row = await self._db.fetchrow(
            """
            INSERT INTO remediation_actions
                (id, incident_id, action_type, state, resource_type, resource_id,
                 service_id, environment, reason, supporting_evidence,
                 expected_effect, blast_radius, rollback_plan, verification_plan,
                 arguments, idempotency_key, proposed_by)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17)
            ON CONFLICT (idempotency_key) DO NOTHING
            RETURNING *
            """,
            proposal.id, proposal.incident_id, proposal.action_type.value,
            ActionState.PROPOSED.value, proposal.target.resource_type,
            proposal.target.resource_id, proposal.target.service_id,
            proposal.target.environment, proposal.reason,
            list(proposal.supporting_evidence),
            _dump(proposal.expected_effect), _dump(proposal.blast_radius),
            _dump(proposal.rollback) or None, _dump(proposal.verification),
            dict(proposal.arguments), proposal.idempotency_key,
            proposal.proposed_by.value,
        )
        if row is not None:
            return self._row(row), True

        existing = await self.by_idempotency_key(proposal.idempotency_key)
        if existing is None:  # pragma: no cover - only if the row vanished mid-flight
            raise DomainError(
                "action insert conflicted but no existing row was found",
                context={"idempotency_key": proposal.idempotency_key},
            )
        log.info(
            "action already proposed; returning the original",
            action_id=existing.id,
            idempotency_key=proposal.idempotency_key,
            state=existing.state.value,
        )
        return existing, False

    async def get(self, action_id: str) -> StoredAction | None:
        row = await self._db.fetchrow(
            "SELECT * FROM remediation_actions WHERE id = $1", action_id
        )
        return self._row(row) if row else None

    async def require(self, action_id: str) -> StoredAction:
        action = await self.get(action_id)
        if action is None:
            raise NotFoundError("action not found", context={"action_id": action_id})
        return action

    async def by_idempotency_key(self, key: str) -> StoredAction | None:
        row = await self._db.fetchrow(
            "SELECT * FROM remediation_actions WHERE idempotency_key = $1", key
        )
        return self._row(row) if row else None

    async def for_incident(self, incident_id: str, *, limit: int = 100) -> list[StoredAction]:
        rows = await self._db.fetch(
            """
            SELECT * FROM remediation_actions
             WHERE incident_id = $1 ORDER BY created_at DESC LIMIT $2
            """,
            incident_id, min(limit, 500),
        )
        return [self._row(r) for r in rows]

    async def recent(
        self, *, state: ActionState | None = None, limit: int = 50
    ) -> list[StoredAction]:
        """Most recent actions across all incidents, optionally by state.

        The state filter is bound as a parameter rather than interpolated, and
        it is an enum on the way in, so an arbitrary string can never reach the
        query.
        """
        rows = await self._db.fetch(
            """
            SELECT * FROM remediation_actions
             WHERE ($2::text IS NULL OR state = $2)
             ORDER BY created_at DESC
             LIMIT $1
            """,
            min(limit, 200), state.value if state else None,
        )
        return [self._row(r) for r in rows]

    async def transition(
        self,
        action_id: str,
        *,
        to: ActionState,
        expected: ActionState | None = None,
        error: str | None = None,
        result: dict[str, Any] | None = None,
        mark_executed: bool = False,
        mark_completed: bool = False,
    ) -> StoredAction:
        """Move an action to a new state, optionally guarded on the current one.

        The guard is what makes concurrent writers safe: two workers both trying
        to move PROPOSED -> EXECUTING produce one success and one
        ``DomainError``, rather than two executions.
        """
        row = await self._db.fetchrow(
            """
            UPDATE remediation_actions
               SET state = $2,
                   error = COALESCE($4, error),
                   result = COALESCE($5, result),
                   executed_at = CASE WHEN $6 THEN now() ELSE executed_at END,
                   completed_at = CASE WHEN $7 THEN now() ELSE completed_at END,
                   updated_at = now()
             WHERE id = $1 AND ($3::text IS NULL OR state = $3)
            RETURNING *
            """,
            action_id, to.value, expected.value if expected else None,
            error, result, mark_executed, mark_completed,
        )
        if row is None:
            current = await self.get(action_id)
            if current is None:
                raise NotFoundError("action not found", context={"action_id": action_id})
            want = expected.value if expected else "?"
            raise DomainError(
                f"action is {current.state.value}, expected {want}",
                context={
                    "action_id": action_id,
                    "current_state": current.state.value,
                    "expected_state": expected.value if expected else None,
                },
            )
        log.info("action state changed", action_id=action_id, state=to.value)
        return self._row(row)

    async def record_decision(
        self,
        *,
        action_id: str,
        incident_id: str,
        decision: PolicyDecision,
        context_snapshot: dict[str, Any],
    ) -> str:
        """Persist a policy decision with the exact context it was made from.

        The snapshot is what makes the decision replayable during an audit: a
        reviewer can rebuild the PolicyContext months later and confirm the same
        rules produce the same effect.
        """
        decision_id = "pd_" + new_id("act").split("_", 1)[1]
        await self._db.execute(
            """
            INSERT INTO policy_decisions
                (id, action_id, incident_id, effect, risk_tier, matched_rule,
                 reasons, gates, policy_version, context_snapshot,
                 decided_at, expires_at)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12)
            """,
            decision_id, action_id, incident_id, decision.effect.value,
            int(decision.risk_tier), decision.matched_rule, list(decision.reasons),
            [json.loads(g.model_dump_json()) for g in decision.gates],
            decision.policy_version, context_snapshot,
            decision.decided_at, decision.expires_at,
        )
        return decision_id

    async def decisions_for(self, action_id: str) -> list[dict[str, Any]]:
        rows = await self._db.fetch(
            """
            SELECT id, effect, risk_tier, matched_rule, reasons, gates,
                   policy_version, context_snapshot, decided_at, expires_at
              FROM policy_decisions WHERE action_id = $1 ORDER BY decided_at DESC
            """,
            action_id,
        )
        return [dict(r) for r in rows]

    async def latest_decision(self, action_id: str) -> PolicyDecision | None:
        rows = await self.decisions_for(action_id)
        if not rows:
            return None
        r = rows[0]
        return PolicyDecision(
            effect=PolicyEffect(r["effect"]),
            risk_tier=RiskTier(int(r["risk_tier"])),
            matched_rule=r["matched_rule"],
            reasons=list(r["reasons"]),
            gates=r["gates"] or [],
            policy_version=r["policy_version"],
            decided_at=r["decided_at"],
            expires_at=r["expires_at"],
        )

    @staticmethod
    def _row(row: Any) -> StoredAction:
        return StoredAction(
            id=row["id"],
            incident_id=row["incident_id"],
            action_type=ActionType(row["action_type"]),
            state=ActionState(row["state"]),
            resource_type=row["resource_type"],
            resource_id=row["resource_id"],
            service_id=row["service_id"],
            environment=row["environment"],
            reason=row["reason"],
            supporting_evidence=list(row["supporting_evidence"] or []),
            expected_effect=row["expected_effect"] or {},
            blast_radius=row["blast_radius"] or {},
            rollback_plan=row["rollback_plan"],
            verification_plan=row["verification_plan"] or {},
            arguments=row["arguments"] or {},
            idempotency_key=row["idempotency_key"],
            proposed_by=row["proposed_by"],
            executed_at=row["executed_at"],
            completed_at=row["completed_at"],
            result=row["result"],
            error=row["error"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )


__all__ = ["ActionRepository", "StoredAction"]
