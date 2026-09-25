"""The worker's long-horizon runtime.

Owns everything the step loop needs that outlives one incident: the RawTree
writer, the metrics forwarder, the heartbeat that opens incidents, and the
control channel the war room uses to crash this process on stage.

Kept out of ``worker.main`` so the job loop there stays a job loop. The worker
asks this module three things: drive an incident, resume one after a human
decision, and whether a given approval belongs to a horizon run at all.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
from typing import Any

from aegis.core.clock import SYSTEM_CLOCK
from aegis.core.config import Settings
from aegis.core.logging import get_logger

log = get_logger(__name__)

CONTROL_CHANNEL = "aegis:control"
# The exit status a SIGKILL produces. Docker's restart policy treats it as a
# crash, which is the point: the demo proves resume-after-crash, not
# resume-after-graceful-shutdown.
CRASH_EXIT_CODE = 137


class HorizonRuntime:
    """Long-lived horizon collaborators for one worker process."""

    def __init__(self, container: Any, settings: Settings) -> None:
        self._container = container
        self._settings = settings
        self._tasks: list[asyncio.Task[Any]] = []
        self._stop = asyncio.Event()
        self._bus: Any = None
        self._forwarder: Any = None
        self._heartbeat: Any = None

    # ------------------------------------------------------------------ #
    # lifecycle                                                           #
    # ------------------------------------------------------------------ #

    async def start(self) -> None:
        c = self._container
        self._bus = self._build_bus()

        if c.rawtree is not None:
            try:
                await c.rawtree.start()
            except Exception as exc:  # noqa: BLE001 - postgres still holds every event
                log.warning("rawtree writer did not start", error=str(exc))

        if c.rawtree_tools is not None:
            # Fetches the server's tool list once and filters it to the
            # read-only allowlist; until then the brain simply has no RawTree
            # tools, which is a gap rather than a failure.
            try:
                await asyncio.wait_for(c.rawtree_tools.refresh(), timeout=15.0)
            except Exception as exc:  # noqa: BLE001
                log.warning("rawtree agent tools unavailable", error=str(exc))

        try:
            from aegis.telemetry.rawtree import Heartbeat, MetricsForwarder

            if c.rawtree is not None:
                self._forwarder = MetricsForwarder(self._settings, c.rawtree)
                self._heartbeat = Heartbeat(
                    self._settings, c.rawtree, self._forwarder, self._on_anomaly
                )
                self._spawn(self._forwarder.run(self._stop), "forwarder")
                self._spawn(self._heartbeat.run(self._stop), "heartbeat")
        except Exception as exc:  # noqa: BLE001 - detection degrades, investigations do not
            log.warning("heartbeat not started", error=str(exc))

        if c.redis is not None and not self._settings.is_production:
            self._spawn(self._control_loop(), "control")

    async def aclose(self) -> None:
        self._stop.set()
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        rawtree = self._container.rawtree
        if rawtree is not None:
            with contextlib.suppress(Exception):
                await rawtree.aclose()

    def _spawn(self, coro: Any, name: str) -> None:
        task = asyncio.create_task(coro, name=f"horizon:{name}")
        task.add_done_callback(self._report_exit)
        self._tasks.append(task)

    def _report_exit(self, task: asyncio.Task[Any]) -> None:
        """Make a background loop's death loud.

        These loops are meant to run until shutdown. One that ends early -
        above all the heartbeat - means faults stop being detected, and without
        this the only symptom is silence.
        """
        if task.cancelled() or self._stop.is_set():
            return
        exc = task.exception()
        log.error(
            "horizon background task stopped unexpectedly",
            task=task.get_name(),
            error=f"{type(exc).__name__}: {exc}" if exc else "returned early",
        )

    # ------------------------------------------------------------------ #
    # incidents                                                           #
    # ------------------------------------------------------------------ #

    def _build_bus(self) -> Any:
        from aegis.agents.horizon.events import EventBus

        sinks: list[Any] = []
        if self._container.redis is not None:
            from aegis.api.war_room_events import RedisEventSink

            sinks.append(RedisEventSink(self._container.redis))
        return EventBus(self._store(), sinks, self._container.rawtree)

    def _store(self) -> Any:
        store = self._container.horizon_store
        if store is None:
            # Without Postgres checkpoints a crash would lose the run, so this
            # is reported loudly - but the loop can still complete in memory.
            from aegis.agents.horizon.memory_store import InMemoryHorizonStore

            log.error("horizon store unavailable; checkpoints are in-memory only")
            store = InMemoryHorizonStore()
            self._container.horizon_store = store
        return store

    def orchestrator(self) -> Any:
        from aegis.agents.horizon.orchestrator import HorizonOrchestrator

        return HorizonOrchestrator(self._deps())

    def _deps(self) -> Any:
        from aegis.worker.horizon_deps import build_horizon_deps

        return build_horizon_deps(self._container, self._settings, self._bus)

    async def run_incident(self, incident_id: str) -> Any:
        return await self.orchestrator().run(incident_id)

    async def resume(self, incident_id: str, action_id: str, approved: bool) -> Any:
        return await self.orchestrator().resume_after_approval(
            incident_id, action_id, approved
        )

    async def owns_action(self, incident_id: str, action_id: str) -> bool:
        """Whether this approval belongs to a horizon run parked on it."""
        try:
            state = await self._store().load_latest(incident_id)
        except Exception as exc:  # noqa: BLE001 - unknown means "not ours", fail closed
            log.warning("horizon checkpoint lookup failed", error=str(exc))
            return False
        return state is not None and state.pending_action_id == action_id

    # ------------------------------------------------------------------ #
    # heartbeat and control                                               #
    # ------------------------------------------------------------------ #

    async def _on_anomaly(self, anomaly: Any) -> None:
        from aegis.telemetry.rawtree import open_incident_from_anomaly

        incident_id = await open_incident_from_anomaly(self._container.db, anomaly)
        if incident_id is None or self._bus is None:
            return
        from aegis.domain.horizon import (
            HorizonEvent,
            HorizonEventType,
            HorizonPhase,
        )

        await self._bus.emit(
            HorizonEvent(
                ts=SYSTEM_CLOCK.now(),
                run_id="heartbeat",
                incident_id=incident_id,
                step=0,
                phase=HorizonPhase.DETECTING,
                event_type=HorizonEventType.HEARTBEAT_ANOMALY,
                source=anomaly.source,
                message=(
                    f"{anomaly.service} {anomaly.metric}={anomaly.value:.3g} "
                    f"(z={anomaly.z:.1f})"
                ),
                payload={
                    "service": anomaly.service,
                    "metric": anomaly.metric,
                    "value": anomaly.value,
                    "baseline_mean": anomaly.baseline_mean,
                    "z": anomaly.z,
                },
            )
        )

    async def _control_loop(self) -> None:
        """Honour the war room's "kill worker" button.

        A hard ``os._exit`` rather than a signal to ourselves: no drain, no
        checkpoint flush, no lease release - the same as ``kill -9``. Only
        outside production, and only for this one command.
        """
        redis = self._container.redis
        pubsub = redis.pubsub()
        await pubsub.subscribe(CONTROL_CHANNEL)
        try:
            while not self._stop.is_set():
                try:
                    message = await asyncio.wait_for(
                        pubsub.get_message(ignore_subscribe_messages=True), timeout=5.0
                    )
                except TimeoutError:
                    continue
                if not message:
                    continue
                try:
                    body = json.loads(message.get("data") or "{}")
                except (TypeError, json.JSONDecodeError):
                    log.warning("ignored malformed control message")
                    continue
                if body.get("command") == "crash":
                    log.warning("crash requested from the war room; exiting hard")
                    os._exit(CRASH_EXIT_CODE)
        finally:
            with contextlib.suppress(Exception):
                await pubsub.unsubscribe(CONTROL_CHANNEL)
                await pubsub.aclose()


__all__ = ["CONTROL_CHANNEL", "HorizonRuntime"]
