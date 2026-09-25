"""Persistence behaviour against a real PostgreSQL.

The unit suite proves the logic; these prove the assumptions it rests on. Every
test here would pass against a mock and still be wrong in production, because
what is being checked is the database's own behaviour: does the partial unique
index actually reject a second live lease, does ``ON CONFLICT`` actually return
the original row, does a conditional UPDATE actually lose a race.

Those are the guarantees the safety model is built on. If Postgres does not
behave as the schema claims, no amount of application logic saves us.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from aegis.core.config import Settings
from aegis.core.errors import DomainError, LeaseConflict
from aegis.core.ids import ACTION, new_id
from aegis.domain.enums import ActionState, ActionType, AgentRole, MetricDirection
from aegis.domain.models import (
    ActionProposal,
    BlastRadius,
    ExpectedEffect,
    ResourceRef,
    RollbackPlan,
    VerificationPlan,
)
from aegis.execution.approvals import ApprovalStore
from aegis.execution.leases import LeaseManager
from aegis.persistence.actions import ActionRepository
from aegis.persistence.audit import AuditEvent, AuditLog
from aegis.persistence.db import Database

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


def _proposal(incident_id: str, *, key: str, resource: str = "payment-1") -> ActionProposal:
    return ActionProposal(
        id=new_id(ACTION),
        incident_id=incident_id,
        action_type=ActionType.RESTART_INSTANCE,
        target=ResourceRef(
            resource_type="instance",
            resource_id=resource,
            environment="test",
            service_id="test:default:payment",
        ),
        reason="integration test proposal",
        supporting_evidence=["ev_integration_a"],
        expected_effect=ExpectedEffect(
            metric="error_rate", direction=MetricDirection.DECREASE, threshold=0.01
        ),
        blast_radius=BlastRadius(directly_affected=["payment"]),
        rollback=RollbackPlan(
            strategy="inverse_action", description="restart again", automatic=True
        ),
        verification=VerificationPlan(
            target_metric="error_rate", direction=MetricDirection.DECREASE, threshold=0.01
        ),
        idempotency_key=key,
        proposed_by=AgentRole.REMEDIATION_PLANNER,
        proposed_at=datetime.now(UTC),
    )


# --------------------------------------------------------------------------- #
# leases: the database is the concurrency arbiter                              #
# --------------------------------------------------------------------------- #


async def test_two_workers_cannot_hold_one_resource(db: Database, clean_incident: str) -> None:
    """The partial unique index must produce one winner, not two holders."""
    audit = AuditLog(db)
    leases = LeaseManager(db, audit, default_ttl_seconds=60)
    target = ResourceRef(
        resource_type="instance", resource_id=f"lease-{clean_incident[-8:]}",
        environment="test", service_id="test:default:payment",
    )

    first = await leases.acquire(target, holder="worker-a", incident_id=clean_incident)
    try:
        with pytest.raises(LeaseConflict) as exc:
            await leases.acquire(target, holder="worker-b", incident_id=clean_incident)
        assert exc.value.context["held_by"] == "worker-a"
    finally:
        await leases.release(first)

    # Once released the resource is immediately available again.
    second = await leases.acquire(target, holder="worker-b", incident_id=clean_incident)
    await leases.release(second)


async def test_concurrent_acquisition_produces_exactly_one_winner(
    db: Database, clean_incident: str
) -> None:
    """Racing coroutines, not sequential calls - this is the real failure mode."""
    audit = AuditLog(db)
    leases = LeaseManager(db, audit, default_ttl_seconds=60)
    target = ResourceRef(
        resource_type="instance", resource_id=f"race-{clean_incident[-8:]}",
        environment="test", service_id="test:default:payment",
    )

    results = await asyncio.gather(
        *(leases.acquire(target, holder=f"worker-{i}", incident_id=clean_incident)
          for i in range(5)),
        return_exceptions=True,
    )
    winners = [r for r in results if not isinstance(r, BaseException)]
    conflicts = [r for r in results if isinstance(r, LeaseConflict)]

    assert len(winners) == 1, "exactly one worker may hold the lease"
    assert len(conflicts) == 4
    await leases.release(winners[0])


async def test_an_expired_lease_is_reaped_and_reacquired(
    db: Database, clean_incident: str
) -> None:
    """A dead worker must not block a resource forever."""
    audit = AuditLog(db)
    leases = LeaseManager(db, audit, default_ttl_seconds=60)
    target = ResourceRef(
        resource_type="instance", resource_id=f"expire-{clean_incident[-8:]}",
        environment="test", service_id="test:default:payment",
    )

    stale = await leases.acquire(
        target, holder="dead-worker", incident_id=clean_incident, ttl_seconds=1
    )
    await db.execute(
        "UPDATE resource_leases SET expires_at = now() - interval '1 minute' WHERE id = $1",
        stale.id,
    )

    assert await leases.is_held(target) is False, "an expired lease is not held"
    fresh = await leases.acquire(target, holder="live-worker", incident_id=clean_incident)
    assert fresh.id != stale.id
    await leases.release(fresh)


# --------------------------------------------------------------------------- #
# actions: idempotency is a column, not an application check                   #
# --------------------------------------------------------------------------- #


async def test_the_same_idempotency_key_returns_the_original_action(
    db: Database, clean_incident: str
) -> None:
    """A retried proposal must observe the first action, never create a second."""
    actions = ActionRepository(db)
    key = f"idem-{clean_incident[-10:]}"

    first, created_first = await actions.propose(_proposal(clean_incident, key=key))
    second, created_second = await actions.propose(_proposal(clean_incident, key=key))

    assert created_first is True
    assert created_second is False, "a duplicate key must not create a second action"
    assert first.id == second.id

    rows = await db.fetchval(
        "SELECT count(*) FROM remediation_actions WHERE idempotency_key = $1", key
    )
    assert int(rows) == 1


async def test_a_guarded_transition_loses_the_race_rather_than_clobbering(
    db: Database, clean_incident: str
) -> None:
    """Two workers moving one action forward produce one success and one error."""
    actions = ActionRepository(db)
    stored, _ = await actions.propose(
        _proposal(clean_incident, key=f"guard-{clean_incident[-10:]}")
    )

    await actions.transition(
        stored.id, to=ActionState.APPROVED, expected=ActionState.PROPOSED
    )
    with pytest.raises(DomainError) as exc:
        await actions.transition(
            stored.id, to=ActionState.APPROVED, expected=ActionState.PROPOSED
        )
    assert exc.value.context["current_state"] == ActionState.APPROVED.value


# --------------------------------------------------------------------------- #
# approvals: expiry is enforced by the query, not by a later check              #
# --------------------------------------------------------------------------- #


async def test_at_most_one_approval_is_open_per_action(
    db: Database, clean_incident: str
) -> None:
    actions = ActionRepository(db)
    audit = AuditLog(db)
    approvals = ApprovalStore(db, audit, ttl_seconds=900)
    stored, _ = await actions.propose(
        _proposal(clean_incident, key=f"apr-{clean_incident[-10:]}")
    )

    first = await approvals.request(action_id=stored.id, incident_id=clean_incident)
    second = await approvals.request(action_id=stored.id, incident_id=clean_incident)
    assert first.id == second.id, "a repeated request must return the open one"


async def test_an_expired_request_cannot_be_decided(
    db: Database, clean_incident: str, approver: str
) -> None:
    """A decision arriving after expiry is refused, not silently revived."""
    actions = ActionRepository(db)
    audit = AuditLog(db)
    approvals = ApprovalStore(db, audit, ttl_seconds=900)
    stored, _ = await actions.propose(
        _proposal(clean_incident, key=f"exp-{clean_incident[-10:]}")
    )
    request = await approvals.request(action_id=stored.id, incident_id=clean_incident)

    await db.execute(
        "UPDATE approvals SET expires_at = now() - interval '1 minute' WHERE id = $1",
        request.id,
    )
    with pytest.raises(DomainError, match="expired"):
        await approvals.decide(request.id, decision="approved", decided_by=approver)

    reloaded = await approvals.get(request.id)
    assert reloaded is not None
    assert reloaded.decision is None, "an expired request is never recorded as decided"
    assert reloaded.is_usable(datetime.now(UTC)) is False


async def test_a_granted_approval_stops_being_usable_once_it_lapses(
    db: Database, clean_incident: str, approver: str
) -> None:
    actions = ActionRepository(db)
    audit = AuditLog(db)
    approvals = ApprovalStore(db, audit, ttl_seconds=900)
    stored, _ = await actions.propose(
        _proposal(clean_incident, key=f"lapse-{clean_incident[-10:]}")
    )
    request = await approvals.request(action_id=stored.id, incident_id=clean_incident)
    granted = await approvals.decide(
        request.id, decision="approved", decided_by=approver, note="looks right"
    )

    assert granted.is_usable(datetime.now(UTC)) is True
    assert granted.is_usable(granted.expires_at + timedelta(seconds=1)) is False


# --------------------------------------------------------------------------- #
# audit: append-only, and never fatal                                          #
# --------------------------------------------------------------------------- #


async def test_audit_rows_are_written_and_readable_in_order(
    db: Database, clean_incident: str
) -> None:
    audit = AuditLog(db)
    for event in (AuditEvent.INCIDENT_CREATED, AuditEvent.ACTION_PROPOSED):
        await audit.record(
            event_type=event,
            actor="integration-test",
            actor_type="system",
            incident_id=clean_incident,
            correlation_id="corr-integration",
        )

    records = await audit.for_incident(clean_incident)
    assert [r.event_type for r in records] == [
        AuditEvent.INCIDENT_CREATED,
        AuditEvent.ACTION_PROPOSED,
    ]
    assert audit.write_failures == 0


async def test_the_audit_trail_outlives_the_incident(
    db: Database, settings: Settings
) -> None:
    """``audit_log.incident_id`` carries no foreign key, and that is deliberate.

    An audit row must survive the incident it describes. If deleting an incident
    cascaded away its audit trail, the record of who authorised what would be
    erasable by deleting the thing it was about - which is the one property an
    audit log may not have.
    """
    audit = AuditLog(db)
    orphan_id = "inc_never_existed_0000000000"
    await audit.record(
        event_type=AuditEvent.ACTION_PROPOSED,
        actor="integration-test",
        actor_type="system",
        incident_id=orphan_id,
    )
    assert audit.write_failures == 0, "an unparented audit row is legal by design"

    written = await db.fetchval(
        "SELECT count(*) FROM audit_log WHERE incident_id = $1", orphan_id
    )
    assert int(written) == 1
    await db.execute("DELETE FROM audit_log WHERE incident_id = $1", orphan_id)


async def test_an_audit_failure_is_counted_and_never_propagates(
    settings: Settings,
) -> None:
    """Losing an audit row must not abort the operation being audited.

    A closed pool is the honest way to provoke a real write failure: the call
    must return normally and increment the counter, because failing a rollback
    because its audit row could not be written would turn a recoverable incident
    into an unrecoverable one.
    """
    closed = Database(settings)
    audit = AuditLog(closed)

    await audit.record(
        event_type=AuditEvent.ACTION_PROPOSED,
        actor="integration-test",
        actor_type="system",
    )
    assert audit.write_failures == 1, "the failure must be counted, not hidden"
