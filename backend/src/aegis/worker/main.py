"""Investigation worker.

Claims jobs from the Postgres queue and runs the LangGraph investigation. The
properties that matter for a process expected to run for months:

* **Graceful shutdown.** SIGTERM stops new claims, lets in-flight jobs finish,
  then closes pools. A container restart does not abandon work midway.
* **Bounded concurrency.** At most ``--concurrency`` investigations run at once,
  so a burst of alerts cannot exhaust memory or the connection pool.
* **Self-healing queue.** Jobs held by a worker that died are reaped and
  requeued, so a hard kill cannot strand an incident forever.
* **No poison loops.** A job that keeps failing is parked after max_attempts and
  surfaced to an operator rather than retried indefinitely.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import os
import signal
from typing import Any

from aegis.agents.state import BudgetGuard
from aegis.agents.state import IncidentState as WorkflowState
from aegis.agents.workflow import run_investigation
from aegis.container import build_container
from aegis.core.clock import SYSTEM_CLOCK
from aegis.core.config import get_settings
from aegis.core.ids import correlation_id
from aegis.core.logging import (
    bind_correlation_id,
    bind_incident_id,
    configure_logging,
    get_logger,
)
from aegis.domain.enums import IncidentState
from aegis.persistence.db import Database
from aegis.persistence.incidents import IncidentRepository
from aegis.persistence.jobs import JobQueue
from aegis.persistence.migrate import run_migrations

log = get_logger(__name__)

POLL_INTERVAL_S = 2.0
REAP_INTERVAL_S = 120.0


async def _diagnosis_confidence(db: Database, incident_id: str) -> float:
    """The confidence of the most recent diagnosis, or zero.

    Zero rather than a default when no diagnosis exists: the confidence floor in
    the policy engine should then refuse the action, which is the correct
    outcome for an action whose justification has vanished.
    """
    value = await db.fetchval(
        """
        SELECT confidence FROM diagnoses
         WHERE incident_id = $1 AND abstained = FALSE
         ORDER BY created_at DESC LIMIT 1
        """,
        incident_id,
    )
    return float(value or 0.0)


def _proposal_from_stored(action: Any) -> Any:
    """Rebuild the exact proposal a human approved.

    Reconstructed from the stored row rather than re-derived from the model, so
    the action that runs is the action that was shown. The idempotency key is
    carried across unchanged, which means re-gating cannot create a second
    action row for work that was already proposed.
    """
    from aegis.domain.models import (
        ActionProposal,
        BlastRadius,
        ExpectedEffect,
        ResourceRef,
        RollbackPlan,
        VerificationPlan,
    )

    return ActionProposal(
        id=action.id,
        incident_id=action.incident_id,
        action_type=action.action_type,
        target=ResourceRef(
            resource_type=action.resource_type,
            resource_id=action.resource_id,
            environment=action.environment,
            service_id=action.service_id,
        ),
        reason=action.reason,
        supporting_evidence=action.supporting_evidence,
        expected_effect=ExpectedEffect(**action.expected_effect),
        blast_radius=BlastRadius(**action.blast_radius),
        rollback=RollbackPlan(**action.rollback_plan) if action.rollback_plan else None,
        verification=VerificationPlan(**action.verification_plan),
        arguments=action.arguments,
        idempotency_key=action.idempotency_key,
        proposed_at=action.created_at,
    )


class Worker:
    def __init__(self, worker_id: str, concurrency: int) -> None:
        self.worker_id = worker_id
        self.concurrency = concurrency
        self._stopping = asyncio.Event()
        self._inflight: set[asyncio.Task[Any]] = set()
        self._settings = get_settings()
        # The worker and the API build the same object graph from the same
        # composition root, so an action the API says needs approval is an
        # action the worker also refuses to run autonomously.
        self._container = build_container(self._settings)
        self._db = self._container.db

    async def start(self) -> None:
        s = self._settings
        configure_logging(s.log_level, s.log_format)
        log.info("worker starting", worker_id=self.worker_id, concurrency=self.concurrency)

        await self._container.connect()
        await run_migrations(self._db)

        unavailable = [
            name for name, cap in self._container.capabilities.items()
            if not cap.configured
        ]
        if unavailable:
            log.info("worker running with degraded capabilities",
                     unavailable=unavailable)

        reaper = asyncio.create_task(self._reap_loop())
        try:
            await self._poll_loop()
        finally:
            reaper.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await reaper
            await self._shutdown()

    def request_stop(self) -> None:
        log.info("shutdown requested; finishing in-flight work")
        self._stopping.set()

    async def _shutdown(self) -> None:
        if self._inflight:
            log.info("draining", jobs=len(self._inflight))
            await asyncio.gather(*self._inflight, return_exceptions=True)
        await self._container.aclose()
        log.info("worker stopped", worker_id=self.worker_id)

    async def _reap_loop(self) -> None:
        """Requeue jobs orphaned by a dead worker."""
        queue = JobQueue(self._db)
        while not self._stopping.is_set():
            try:
                await queue.reap_stale()
            except Exception as exc:  # noqa: BLE001
                log.warning("reaper failed", error=str(exc))
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stopping.wait(), timeout=REAP_INTERVAL_S)

    async def _poll_loop(self) -> None:
        queue = JobQueue(self._db)
        while not self._stopping.is_set():
            if len(self._inflight) >= self.concurrency:
                await asyncio.sleep(0.25)
                continue
            try:
                job = await queue.claim(
                    self.worker_id, kinds=["investigate", "execute_action"]
                )
            except Exception as exc:  # noqa: BLE001
                # A database blip must not kill the worker; back off and retry.
                log.warning("job claim failed", error=str(exc))
                await asyncio.sleep(POLL_INTERVAL_S)
                continue

            if job is None:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._stopping.wait(), timeout=POLL_INTERVAL_S)
                continue

            task = asyncio.create_task(self._run_job(job))
            self._inflight.add(task)
            task.add_done_callback(self._inflight.discard)

    async def _run_job(self, job: dict[str, Any]) -> None:
        if job.get("kind") == "execute_action":
            await self._run_execution(job)
            return
        await self._run_investigation_job(job)

    async def _run_execution(self, job: dict[str, Any]) -> None:
        """Execute an action a human approved.

        The approval authorises the action; it does not bypass the gates. The
        whole chain runs again here because the world may have moved since the
        operator looked at it - the lease may now be held, a kill switch may
        have been engaged, or the evidence behind the proposal may have been
        refuted.
        """
        from aegis.core.errors import AegisError
        from aegis.execution.validated import ValidatedAction

        queue = JobQueue(self._db)
        incidents = IncidentRepository(self._db)
        incident_id = job["incident_id"]
        payload = job.get("payload") or {}
        action_id = payload.get("action_id", "")
        cid = correlation_id()
        bind_correlation_id(cid)
        bind_incident_id(incident_id)

        try:
            container = self._container
            action = await container.actions.require(action_id)
            approval = await container.approvals.granted_for_action(action_id)
            if approval is None or not approval.is_usable(SYSTEM_CLOCK.now()):
                # An approval that lapsed between the click and the worker
                # picking this up is not an authorisation any more.
                log.warning(
                    "approved action is no longer authorised",
                    action_id=action_id,
                    approval_present=approval is not None,
                )
                await queue.complete(job["id"])
                await self._advance(
                    incidents, incident_id, IncidentState.ESCALATED,
                    "approval expired before the action could run",
                )
                return

            ports = container.ports
            if ports is None:
                # No runtime adapter is configured, so there is nothing to
                # execute through. Refuse before gating: validating first would
                # take a lease that could never be used, and blocking a
                # resource on an action that cannot run is worse than the
                # missing capability itself.
                log.error(
                    "approved action cannot execute: no runtime port configured",
                    action_id=action_id,
                    incident_id=incident_id,
                )
                await queue.complete(job["id"])
                await self._advance(
                    incidents, incident_id, IncidentState.ESCALATED,
                    "no runtime adapter is configured; the action was not attempted",
                )
                return

            proposal = _proposal_from_stored(action)
            outcome = await container.gate.validate(
                proposal,
                severity=(await incidents.get(incident_id)).severity,
                diagnosis_confidence=await _diagnosis_confidence(self._db, incident_id),
                has_abstained_diagnosis=False,
                correlation_id=cid,
                holder=f"worker:{self.worker_id}",
            )
            if not isinstance(outcome, ValidatedAction):
                log.warning(
                    "approved action was refused on re-gating",
                    action_id=action_id,
                    matched_rule=outcome.matched_rule,
                    reasons=outcome.reasons,
                )
                await queue.complete(job["id"])
                await self._advance(
                    incidents, incident_id, IncidentState.ESCALATED,
                    f"action refused on re-check: {outcome.matched_rule}",
                )
                return

            await self._advance(
                incidents, incident_id, IncidentState.REMEDIATING,
                "executing an approved action",
            )
            report = await container.execution.execute(outcome, ports)
            await queue.complete(job["id"])
            await self._advance(
                incidents,
                incident_id,
                IncidentState.MONITORING if report.succeeded else IncidentState.ESCALATED,
                report.escalation_reason or "remediation verified",
            )
            log.info(
                "approved action finished",
                action_id=action_id,
                succeeded=report.succeeded,
                state=report.final_state.value,
            )
        except AegisError as exc:
            log.error("approved action failed", action_id=action_id, error=str(exc))
            with contextlib.suppress(Exception):
                await queue.fail(job["id"], f"{exc.code}: {exc.message}")
        finally:
            bind_correlation_id(None)
            bind_incident_id(None)

    async def _run_investigation_job(self, job: dict[str, Any]) -> None:
        queue = JobQueue(self._db)
        incidents = IncidentRepository(self._db)
        incident_id = job["incident_id"]
        cid = correlation_id()
        bind_correlation_id(cid)
        bind_incident_id(incident_id)

        try:
            incident = await incidents.get(incident_id)
            if incident.state.is_terminal:
                log.info("incident already resolved; skipping", incident_id=incident_id)
                await queue.complete(job["id"])
                return

            log.info("investigation starting", incident_id=incident_id,
                     severity=incident.severity.value, attempt=job["attempts"])

            await self._advance(incidents, incident_id, IncidentState.TRIAGING,
                                "worker picked up investigation")

            s = self._settings
            # A fresh budget and a fresh dependency bundle per job: one
            # investigation can never consume another's allowance, and the
            # run-scoped action handoff cannot leak between incidents.
            deps = self._container.workflow_deps(
                BudgetGuard(
                    max_wall_seconds=s.agent_max_wall_seconds,
                    max_llm_calls=s.agent_max_llm_calls,
                    max_tool_calls=s.agent_max_tool_calls,
                    max_tokens=s.agent_max_tokens,
                )
            )

            await self._advance(incidents, incident_id, IncidentState.INVESTIGATING,
                                "evidence collection started")

            result = await run_investigation(
                deps,
                incident_id=incident_id,
                title=incident.title,
                severity=incident.severity.value,
                environment=incident.environment,
                workload=incident.workload,
                correlation_id=cid,
            )

            await self._settle(incidents, incident_id, result)
            await queue.complete(job["id"])
            log.info("investigation finished", incident_id=incident_id,
                     abstained=bool(result.get("abstained")),
                     confidence=result.get("confidence"))

        except Exception as exc:  # noqa: BLE001 - a bad job must not kill the worker
            log.exception("investigation failed", incident_id=incident_id, error=str(exc))
            with contextlib.suppress(Exception):
                await queue.fail(job["id"], f"{type(exc).__name__}: {exc}")
            with contextlib.suppress(Exception):
                await incidents.transition(
                    incident_id, IncidentState.ESCALATED, actor="worker",
                    reason=f"investigation failed: {type(exc).__name__}",
                )
        finally:
            bind_correlation_id(None)
            bind_incident_id(None)

    async def _advance(
        self,
        incidents: IncidentRepository,
        incident_id: str,
        target: IncidentState,
        reason: str,
    ) -> None:
        """Move state, tolerating an incident another actor already advanced.

        An illegal transition here means someone else moved it first, which is
        not a reason to fail the job.
        """
        from aegis.core.errors import DomainError

        try:
            await incidents.transition(incident_id, target, actor="worker", reason=reason)
        except DomainError as exc:
            log.debug("transition skipped", incident_id=incident_id,
                      target=target.value, reason=exc.message)

    async def _settle(
        self, incidents: IncidentRepository, incident_id: str, result: WorkflowState
    ) -> None:
        """Choose the state implied by the outcome.

        Note that an abstention escalates to a human rather than resolving. An
        investigation that could not conclude is unfinished business, not a
        closed incident.
        """
        await self._advance(incidents, incident_id, IncidentState.DIAGNOSING,
                            "hypotheses evaluated")
        if result.get("awaiting_approval"):
            target, reason = IncidentState.AWAITING_APPROVAL, "action requires human approval"
        elif result.get("abstained"):
            target, reason = IncidentState.ESCALATED, "insufficient evidence for a conclusion"
        else:
            target, reason = IncidentState.MONITORING, "diagnosis complete"
        await self._advance(incidents, incident_id, target, reason)


async def _main() -> None:
    parser = argparse.ArgumentParser(description="Aegis investigation worker")
    parser.add_argument(
        "--concurrency", type=int, default=int(os.getenv("WORKER_CONCURRENCY", "2"))
    )
    parser.add_argument(
        "--worker-id", default=os.getenv("HOSTNAME", f"worker-{os.getpid()}")
    )
    args = parser.parse_args()

    worker = Worker(args.worker_id, max(1, args.concurrency))
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        # Windows lacks add_signal_handler; the KeyboardInterrupt path covers it.
        with contextlib.suppress(NotImplementedError, AttributeError):
            loop.add_signal_handler(sig, worker.request_stop)
    await worker.start()


def main() -> None:
    # Ctrl-C during local development is a normal exit, not a failure.
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(_main())


if __name__ == "__main__":
    main()
