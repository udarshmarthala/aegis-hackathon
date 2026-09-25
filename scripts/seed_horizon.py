"""Seed the history the long-horizon agent recalls: INC-042 and its predecessors.

Three things are written, all idempotent (deterministic ids; a re-run inserts
nothing new and never bumps a memory's occurrence count):

* **Remediation history** - eight resolved incidents from the past three weeks,
  each with the actions taken and the verification each one got. Restarts aimed
  at pool exhaustion never verified; rollbacks did. This is what the
  ``action_success_rate`` query aggregates, both from RawTree and from its
  Postgres fallback (``remediation_actions`` joined to ``verification_runs``).
* **The INC-042 memory**, through ``IncidentMemoryStore.write`` and its strict
  gate - not around it. The gate wants a non-abstaining diagnosis that cites
  evidence, a passed verification and a named human approver; each of those is
  a real row seeded here (evidence items, a verification run, an approval by
  the local developer account), so the memory is exactly as grounded as one the
  workflow would write.
* **The INC-042 memory card** in ``horizon_memory_cards`` and one
  ``verification_result`` event per historical action in ``horizon_events``,
  mirrored to RawTree when ``RAWTREE_WRITE_KEY`` is set.

It also ensures the ``dev-user`` account exists: ``approvals.decided_by`` is a
foreign key into ``users``, and the dev-mode principal is ``dev-user``, so
without the row no approval can be recorded on a local stack at all.

Usage:  backend/.venv/Scripts/python.exe scripts/seed_horizon.py   # or: make seed-horizon
"""

from __future__ import annotations

import asyncio
import hashlib
import sys
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from inject_043 import canonical_service_id, use_host_endpoints

DEV_USER = "dev-user"
POOL_SYMPTOM = "pool_exhaustion"
MEMORY_CARD_ID = "mc_inc042"
SEED_RUN_PREFIX = "seed-history:"
LESSON = "restart only buys time when the leak ships in the image; roll back"


@dataclass(frozen=True)
class Attempt:
    action_type: str
    verified: bool
    recovery_s: float  # seconds from execution to the verification verdict


@dataclass(frozen=True)
class PastIncident:
    display_id: str
    days_ago: float
    service: str
    symptom: str
    title: str
    attempts: tuple[Attempt, ...]


RESTART_FAILED = Attempt("restart_instance", verified=False, recovery_s=0.0)

HISTORY: tuple[PastIncident, ...] = (
    PastIncident("INC-031", 21, "checkout", POOL_SYMPTOM,
                 "checkout connection pool exhaustion after deploy",
                 (RESTART_FAILED, Attempt("rollback_deployment", True, 260))),
    PastIncident("INC-034", 17, "payment", POOL_SYMPTOM,
                 "payment connection pool exhaustion after deploy",
                 (RESTART_FAILED, RESTART_FAILED, Attempt("rollback_deployment", True, 300))),
    PastIncident("INC-036", 13, "gateway", "latency_spike",
                 "gateway p99 latency spike on a wedged instance",
                 (Attempt("restart_instance", True, 45),)),
    PastIncident("INC-038", 10, "checkout", POOL_SYMPTOM,
                 "checkout connection pool exhaustion",
                 (RESTART_FAILED, Attempt("rollback_deployment", True, 210))),
    PastIncident("INC-039", 8, "checkout", "cache_degradation",
                 "checkout cache hit rate collapse",
                 (Attempt("restart_instance", True, 60),)),
    PastIncident("INC-040", 6, "payment", POOL_SYMPTOM,
                 "payment connection pool exhaustion after deploy",
                 (RESTART_FAILED, Attempt("rollback_deployment", True, 280))),
    PastIncident("INC-041", 4, "gateway", "error_rate_spike",
                 "gateway 5xx spike after an instance hung",
                 (Attempt("restart_instance", True, 50),)),
    PastIncident("INC-042", 2, "checkout", POOL_SYMPTOM,
                 "checkout connection pool exhaustion after deploy",
                 (RESTART_FAILED, Attempt("rollback_deployment", True, 240))),
)


def stable_id(prefix: str, label: str, at: datetime) -> str:
    """A valid, time-sortable ULID id that is the same on every run."""
    from ulid import ULID

    ms = int(at.timestamp() * 1000)
    tail = hashlib.sha256(label.encode()).digest()[:10]
    return f"{prefix}_{ULID.from_bytes(ms.to_bytes(6, 'big') + tail)}"


async def ensure_dev_user(db: Any) -> None:
    await db.execute(
        """
        INSERT INTO users (id, firebase_uid, email, display_name, roles)
        VALUES ($1, $1, 'dev@localhost', 'Local Developer',
                ARRAY['viewer','responder','approver','admin'])
        ON CONFLICT (id) DO NOTHING
        """,
        DEV_USER,
    )


async def seed_incident(db: Any, settings: Any, past: PastIncident, now: datetime) -> dict[str, Any]:
    """One resolved incident with evidence, actions, approvals and verifications."""
    opened = now - timedelta(days=past.days_ago)
    incident_id = stable_id("inc", past.display_id, opened)
    service_id = canonical_service_id(settings, past.service)
    total_s = 90 + sum(a.recovery_s or 120 for a in past.attempts)
    resolved = opened + timedelta(seconds=total_s)

    await db.execute(
        """
        INSERT INTO incidents (id, title, severity, state, environment, workload,
                               affected_services, confidence, summary, correlation_id,
                               metadata, created_at, updated_at, resolved_at)
        VALUES ($1,$2,'P2','RESOLVED',$3,$4,$5,0.86,$6,$7,$8,$9,$10,$10)
        ON CONFLICT (id) DO NOTHING
        """,
        incident_id, f"{past.display_id} {past.title}", settings.aegis_environment_name,
        settings.workload_namespace, [past.service],
        f"Resolved. {past.symptom.replace('_', ' ')} on {past.service}.",
        f"seed-{past.display_id.lower()}",
        {"display_id": past.display_id, "seeded_history": True, "symptom": past.symptom},
        opened, resolved,
    )

    evidence_ids = []
    for kind, source, source_type, etype, summary in (
        ("pool", "prometheus", "metrics", "metric_series",
         f"connection_pool_in_use/size on {past.service} climbed to 1.0 over ~90s"
         if past.symptom == POOL_SYMPTOM else f"{past.symptom.replace('_', ' ')} on {past.service}"),
        ("deploy", "deployment_attempts", "deployment", "deployment_event",
         f"{past.service} redeployed shortly before the symptom began"),
    ):
        ev_id = stable_id("ev", f"{past.display_id}:{kind}", opened)
        await db.execute(
            """
            INSERT INTO evidence_items (id, incident_id, source, source_type, evidence_type,
                                        status, trust_class, resource_id, summary,
                                        structured_value, provenance_uri, content_hash,
                                        observed_at, retrieved_at)
            VALUES ($1,$2,$3,$4,$5,'VALIDATED',$6,$7,$8,$9,$10,$11,$12,$12)
            ON CONFLICT (id) DO NOTHING
            """,
            ev_id, incident_id, source, source_type, etype,
            "TIER_A" if source_type == "metrics" else "TIER_B", service_id, summary,
            {"seeded_history": True}, f"seed://{past.display_id}/{kind}",
            hashlib.sha256(ev_id.encode()).hexdigest(), opened,
        )
        evidence_ids.append(ev_id)

    actions: list[dict[str, Any]] = []
    at = opened + timedelta(seconds=90)
    for n, attempt in enumerate(past.attempts, start=1):
        action_id = stable_id("act", f"{past.display_id}:{n}", at)
        rollback = attempt.action_type == "rollback_deployment"
        await db.execute(
            """
            INSERT INTO remediation_actions (id, incident_id, action_type, state, resource_type,
                resource_id, service_id, environment, reason, supporting_evidence,
                idempotency_key, proposed_by, executed_at, completed_at, arguments,
                created_at, updated_at, error)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,'remediation_planner',$12,$13,$14,
                    $12,$13,$15)
            ON CONFLICT (id) DO NOTHING
            """,
            action_id, incident_id, attempt.action_type,
            "SUCCESS" if attempt.verified else "FAILED",
            "deployment" if rollback else "instance",
            past.service if rollback else f"{past.service}-1", service_id,
            settings.aegis_environment_name,
            f"{attempt.action_type} for {past.symptom.replace('_', ' ')}", evidence_ids,
            f"seed:{past.display_id}:{n}", at,
            at + timedelta(seconds=attempt.recovery_s or 120),
            {"to_version": "previous"} if rollback else {},
            None if attempt.verified else "verification FAILED: symptom returned",
        )
        if rollback:
            await db.execute(
                """
                INSERT INTO approvals (id, action_id, incident_id, requested_at, expires_at,
                                       decision, decided_by, decided_at, note)
                VALUES ($1,$2,$3,$4,$5,'approved',$6,$7,'rollback approved')
                ON CONFLICT (id) DO NOTHING
                """,
                stable_id("apr", f"{past.display_id}:{n}", at), action_id, incident_id,
                at - timedelta(seconds=60), at + timedelta(minutes=14), DEV_USER,
                at - timedelta(seconds=20),
            )
        verification_id = stable_id("ver", f"{past.display_id}:{n}", at)
        checks = [
            {"name": "pool_utilisation < 0.8 sustained", "passed": attempt.verified},
            {"name": "p99_ms < 250 sustained", "passed": attempt.verified},
            {"name": "error_rate < 0.02 sustained", "passed": attempt.verified},
        ]
        done = at + timedelta(seconds=attempt.recovery_s or 120)
        await db.execute(
            """
            INSERT INTO verification_runs (id, incident_id, action_id, kind, passed, verdict,
                                           checks, notes, started_at, completed_at)
            VALUES ($1,$2,$3,'action',$4,$5,$6,$7,$8,$9)
            ON CONFLICT (id) DO NOTHING
            """,
            verification_id, incident_id, action_id, attempt.verified,
            "VERIFIED" if attempt.verified else "FAILED", checks,
            "sustained window passed" if attempt.verified
            else "pool refilled within the window; the leak returned after restart",
            at, done,
        )
        actions.append({"id": action_id, "attempt": attempt, "at": at, "done": done,
                        "verification_id": verification_id, "checks": checks})
        at = done + timedelta(seconds=30)

    return {"incident_id": incident_id, "evidence_ids": evidence_ids, "actions": actions,
            "opened": opened, "resolved": resolved, "service_id": service_id}


async def seed_memory(db: Any, settings: Any, past: PastIncident, seeded: dict[str, Any]) -> None:
    """INC-042 through the real memory gate."""
    from aegis.domain.models import Diagnosis, VerificationCheck, VerificationResult
    from aegis.memory.store import IncidentMemoryStore

    existing = await db.fetchval(
        "SELECT count(*) FROM incident_memories WHERE incident_id = $1", seeded["incident_id"]
    )
    if existing:
        print(f"  {past.display_id} memory already present")
        return

    fixed = next(a for a in seeded["actions"] if a["attempt"].verified)
    statement = (
        "A connection leak shipped in the checkout deploy exhausted the connection pool; "
        "each request that lost a connection stranded a slot until none were left."
    )
    await db.execute(
        """
        INSERT INTO diagnoses (id, incident_id, abstained, statement, root_cause_category,
                               confidence, supporting_evidence, affected_services, created_at)
        VALUES ($1,$2,FALSE,$3,'resource_exhaustion',0.86,$4,$5,$6)
        ON CONFLICT (id) DO NOTHING
        """,
        stable_id("hyp", f"{past.display_id}:diagnosis", seeded["opened"]),
        seeded["incident_id"], statement, seeded["evidence_ids"], [past.service],
        seeded["resolved"],
    )
    diagnosis = Diagnosis(
        incident_id=seeded["incident_id"],
        abstained=False,
        statement=statement,
        root_cause_category="resource_exhaustion",
        confidence=0.86,
        supporting_evidence=seeded["evidence_ids"],
        affected_services=[past.service],
        contributing_factors=["dependency upgrade in the deploy", "no pool-leak alerting"],
    )
    verification = VerificationResult(
        id=fixed["verification_id"],
        incident_id=seeded["incident_id"],
        action_id=fixed["id"],
        passed=True,
        checks=[VerificationCheck(name=c["name"], passed=c["passed"]) for c in fixed["checks"]],
        started_at=fixed["at"],
        completed_at=fixed["done"],
        notes="sustained window passed after rollback",
    )
    memory = await IncidentMemoryStore(db).write(
        diagnosis=diagnosis,
        verification=verification,
        title=f"{past.display_id} checkout connection pool exhaustion after deploy",
        symptoms="checkout connection pool exhaustion after deploy: pool utilisation climbs "
                 "to 100%, p99 and error rate follow",
        successful_fix="rollback_deployment of checkout to the previous version (verified, "
                       f"recovery ~{int(fixed['attempt'].recovery_s)} s)",
        approved_by=DEV_USER,
        failed_attempts=[
            "restart_instance on checkout: pool reset, then leaked again; verification failed"
        ],
        prevention=LESSON,
        timeline=[
            {"at": a["at"].isoformat(), "action": a["attempt"].action_type,
             "verified": a["attempt"].verified}
            for a in seeded["actions"]
        ],
        evidence_pattern={"symptom": POOL_SYMPTOM, "service": past.service},
    )
    print(f"  {past.display_id} memory written ({memory.id})")


async def seed_horizon_tables(db: Any, history: list[tuple[PastIncident, dict[str, Any]]]) -> list[Any]:
    """The memory card and the historical verification events. Returns new events."""
    from aegis.domain.horizon import (
        HorizonEvent,
        HorizonEventType,
        HorizonPhase,
        MemoryCard,
        Source,
    )
    from aegis.persistence.horizon import PostgresHorizonStore

    if not await db.fetchval("SELECT to_regclass('horizon_events') IS NOT NULL"):
        print("  horizon tables absent (migration 010 not applied yet); skipped")
        return []
    store = PostgresHorizonStore(db)

    past, seeded = next((p, s) for p, s in history if p.display_id == "INC-042")
    fixed = next(a for a in seeded["actions"] if a["attempt"].verified)
    card = MemoryCard(
        id=MEMORY_CARD_ID,
        incident_id=past.display_id,
        symptoms="checkout connection pool exhaustion after deploy",
        root_cause="connection leak shipped in the deployed image",
        failed_actions=["restart_instance"],
        successful_action="rollback_deployment",
        recovery_s=fixed["attempt"].recovery_s,
        lesson=LESSON,
        image_status="unavailable",
        image_reason="seeded history; no incident map rendered",
    )
    await store.save_memory_card(card)
    print(f"  memory card {card.id} saved")

    already = await db.fetchval(
        "SELECT count(*) FROM horizon_events WHERE run_id LIKE $1", f"{SEED_RUN_PREFIX}%"
    )
    if already:
        # Nothing new to mirror either: RawTree tables are append-only, so a
        # second mirror would double every count action_success_rate reports.
        print(f"  {already} historical events already present")
        return []

    new: list[Any] = [card]
    for past_incident, data in history:
        for step, action in enumerate(data["actions"], start=1):
            attempt: Attempt = action["attempt"]
            event = HorizonEvent(
                ts=action["done"],
                run_id=f"{SEED_RUN_PREFIX}{past_incident.display_id}",
                incident_id=data["incident_id"],
                step=step,
                phase=HorizonPhase.VERIFYING,
                event_type=HorizonEventType.VERIFICATION_RESULT,
                tool=attempt.action_type,
                status="ok" if attempt.verified else "error",
                source=Source.POSTGRES,
                message=(
                    f"{attempt.action_type} on {past_incident.service}: "
                    f"{'VERIFIED' if attempt.verified else 'FAILED'}"
                ),
                payload={
                    "action_type": attempt.action_type,
                    "symptom": past_incident.symptom,
                    "verified": attempt.verified,
                    "verdict": "VERIFIED" if attempt.verified else "FAILED",
                    "recovery_s": attempt.recovery_s,
                    "display_id": past_incident.display_id,
                    "action_id": action["id"],
                },
            )
            await store.append_event(event)
            new.append(event)
    print(f"  {len(new) - 1} historical verification events appended")
    return new


async def mirror_to_rawtree(settings: Any, items: list[Any]) -> None:
    if not items:
        return
    if not settings.rawtree_write_key.get_secret_value().strip():
        print("  RawTree: RAWTREE_WRITE_KEY not set; history stays in Postgres only")
        return
    try:
        from aegis.integrations.rawtree import RawTreeClient
    except ImportError as exc:
        print(f"  RawTree: client not importable ({exc}); skipped")
        return
    from aegis.domain.horizon import MemoryCard

    client = RawTreeClient(settings)
    await client.start()
    try:
        for item in items:
            if isinstance(item, MemoryCard):
                client.enqueue_memory_card(item)
            else:
                client.enqueue_event(item)
    finally:
        await client.aclose()
    print(f"  RawTree: {len(items)} rows mirrored ({client.stats()})")


async def main() -> int:
    use_host_endpoints()
    from aegis.core.config import get_settings
    from aegis.persistence.db import Database

    settings = get_settings()
    db = Database(settings)
    await db.connect()
    try:
        # Anchored to midnight UTC so a re-run on the same day computes the same
        # ids; a run on a later day finds the INC-042 memory by incident, not by
        # timestamp, and adds no second copy of history either.
        now = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
        seeded_before = await db.fetchval(
            "SELECT min(created_at) FROM incidents "
            "WHERE metadata->>'seeded_history' = 'true'"
        )
        if seeded_before is not None:
            now = seeded_before + timedelta(days=HISTORY[0].days_ago)

        await ensure_dev_user(db)
        history = []
        for past in HISTORY:
            history.append((past, await seed_incident(db, settings, past, now)))
        print(f"  {len(history)} historical incidents present")

        past42, seeded42 = history[-1]
        await seed_memory(db, settings, past42, seeded42)
        new_items = await seed_horizon_tables(db, history)
        await mirror_to_rawtree(settings, new_items)
    finally:
        await db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
