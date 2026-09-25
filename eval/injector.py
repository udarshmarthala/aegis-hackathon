"""Fault injection. Deliberately outside the Aegis package.

ESD section 16: "Fault injection must be separated from Aegis itself. This
prevents the benchmark from accidentally giving the agent privileged knowledge
about the injected fault."

That separation is physical here. This module imports nothing from
``aegis.agents``, ``aegis.api`` or ``aegis.mcp``; it talks to the workload's own
admin surface over HTTP, the same way a chaos tool would. The harness passes it
``scenario.fault`` and passes the system under test ``scenario.to_input()`` -
two different objects, two different call sites, no shared state.

Endpoints are resolved from ``EVAL_WORKLOAD_ENDPOINTS`` (``name=url`` pairs,
comma separated) and otherwise from ``http://{target}:8080``, which is what the
compose network provides. An unreachable target raises ``SourceUnavailable`` and
a fault mode this injector cannot apply raises ``ExternalServiceError``; the
harness records both as harness failures and keeps them out of every
model-quality aggregate. A benchmark that could not inject its fault has
measured nothing, and must not be scored as if the model failed.
"""

from __future__ import annotations

import asyncio
import os
from typing import Any, Final

import httpx

from aegis.core.errors import ExternalServiceError, SourceUnavailable
from aegis.core.logging import get_logger
from aegis.evaluation.schema import FaultMode, Scenario, SecondaryFault

log = get_logger(__name__)

DEFAULT_PORT: Final = 8080
REQUEST_TIMEOUT_S: Final = 10.0
# The alert lag a scenario declares is bounded here as well: a scenario file is
# operator input, and a benchmark must not be able to block for half an hour.
MAX_SETTLE_S: Final = 300.0

# Fault modes the reference workload implements natively.
#
# This is every declared mode: ``workload/service.py`` implements the whole
# vocabulary, so a scenario can no longer be blocked because the image it runs
# against is less capable than the schema that describes it. The list is kept
# here rather than derived at import time because the preflight must answer
# "can this suite run" before anything is reachable - a dry run on a laptop
# with no stack still has to be truthful.
#
# Drift in the dangerous direction is caught at runtime, not trusted: the
# workload rejects an unknown mode with a 400, which ``_apply`` turns into a
# harness failure. An older deployed image therefore fails loudly instead of
# accepting the request and injecting nothing. ``verify_modes`` makes that
# check eagerly when a stack is available.
WORKLOAD_MODES: Final[frozenset[FaultMode]] = frozenset(FaultMode)


def _topology_endpoints() -> dict[str, dict[str, str]]:
    """``{topology: {service: url}}`` from the topology specs.

    Failing soft is deliberate: a missing or malformed topology file must not
    stop a run that does not need it. What must never happen is a *silent*
    fallback to an unreachable hostname, and that is covered by the preflight,
    which reports every target it cannot resolve before the run starts.
    """
    try:
        from topology import TopologyError, load_all

        return {name: topo.endpoint_map for name, topo in load_all().items()}
    except (ImportError, TopologyError, OSError, KeyError) as exc:
        log.warning("topology endpoints unavailable", error=f"{type(exc).__name__}: {exc}")
        return {}


def parse_endpoints(raw: str | None) -> dict[str, str]:
    """``"gateway=http://localhost:8080,checkout=http://localhost:8081"``."""
    endpoints: dict[str, str] = {}
    for part in (raw or "").split(","):
        name, _, url = part.partition("=")
        if name.strip() and url.strip():
            endpoints[name.strip()] = url.strip().rstrip("/")
    return endpoints


class WorkloadFaultInjector:
    """Applies and clears faults through the workload's ``/admin/fault`` API."""

    __slots__ = ("_endpoints", "_topology_endpoints", "_client", "_settle", "_strict")

    def __init__(
        self,
        endpoints: dict[str, str] | None = None,
        *,
        settle: bool = True,
        strict: bool = True,
    ) -> None:
        self._endpoints = endpoints or parse_endpoints(os.getenv("EVAL_WORKLOAD_ENDPOINTS"))
        # Published host ports, per topology, from eval/topologies/*.yaml. Loaded
        # once because it is static configuration, and tolerated as empty so the
        # injector still works in a bare checkout with no topology files.
        self._topology_endpoints = _topology_endpoints()
        self._client: httpx.AsyncClient | None = None
        self._settle = settle
        # strict=True turns an unsupported fault mode into a harness failure, and
        # it is the default because the alternative is worse than a failed
        # scenario: injecting nothing leaves the workload healthy, and the
        # harness then scores a healthy system against an answer key that
        # describes a fault nobody applied. Every aggregate containing such a
        # scenario is a number about nothing. Callers who deliberately want the
        # partial subset pass strict=False and are told what they lost.
        self._strict = strict

    @staticmethod
    def supported_modes() -> frozenset[FaultMode]:
        """The fault modes this injector can actually apply.

        The single source of truth for "can this scenario run here". The
        preflight in ``eval/run.py`` asks this rather than keeping its own copy,
        so adding a mode to the workload cannot leave a stale list behind.
        """
        return WORKLOAD_MODES

    @classmethod
    def unsupported_modes(cls, scenario: Scenario) -> tuple[FaultMode, ...]:
        """Modes this scenario needs and this injector cannot apply, in order.

        Secondary faults count. A scenario whose primary fault is injectable but
        whose secondary fault is not is still only half-injected, and half a
        fault is not the scenario the answer key describes.
        """
        supported = cls.supported_modes()
        declared = (scenario.fault.mode, *(s.mode for s in scenario.fault.secondary))
        return tuple(dict.fromkeys(m for m in declared if m not in supported))

    @classmethod
    def can_inject(cls, scenario: Scenario) -> bool:
        return not cls.unsupported_modes(scenario)

    def url_for(self, target: str, workload: str = "") -> str:
        """Where to reach ``target``, preferring an explicit map.

        Resolution order is: an operator's ``EVAL_WORKLOAD_ENDPOINTS`` entry,
        then the published host port of the topology this scenario names, then
        the compose hostname. The middle step is what lets a harness on the host
        reach a service that publishes no port of its own - without it, every
        scenario targeting anything but an entrypoint could only be injected
        from inside the compose network.
        """
        explicit = self._endpoints.get(target)
        if explicit:
            return explicit
        mapped = self._topology_endpoints.get(workload, {}).get(target)
        if mapped:
            return mapped
        return f"http://{target}:{DEFAULT_PORT}"

    def knows(self, target: str, workload: str = "") -> bool:
        """Whether a host exists for this target at all.

        The preflight asks this so that a scenario whose topology is not running
        is reported before the run rather than as a timeout in the middle of it.
        """
        return bool(
            self._endpoints.get(target)
            or self._topology_endpoints.get(workload, {}).get(target)
        )

    async def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=REQUEST_TIMEOUT_S)
        return self._client

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # ---- ScenarioEnvironment ------------------------------------------------

    async def prepare(self, scenario: Scenario) -> None:
        """Inject the scenario's fault, then wait for it to become visible."""
        fault = scenario.fault
        if fault.mode is FaultMode.NONE:
            # False-positive scenarios inject nothing by construction. The wait
            # still happens so the alert window looks like every other run.
            log.info("no fault to inject", scenario=scenario.id)
        else:
            await self._apply(
                scenario.id,
                workload=scenario.workload,
                target=fault.target,
                mode=fault.mode,
                magnitude_ms=fault.magnitude_ms,
                error_rate=fault.error_rate,
                probability=fault.probability,
                parameters=dict(fault.parameters),
            )
            for extra in fault.secondary:
                await self._apply_secondary(scenario.id, extra, scenario.workload)

        if self._settle:
            # Telemetry has to exist before Aegis is asked about it; scraping is
            # what makes the fault observable at all.
            await asyncio.sleep(min(float(scenario.alert.fires_after_s), MAX_SETTLE_S))

    async def cleanup(self, scenario: Scenario) -> None:
        """Clear every fault this scenario injected. Best effort, always tried."""
        targets = [scenario.fault.target, *[s.target for s in scenario.fault.secondary]]
        for target in dict.fromkeys(t for t in targets if t and t != "none"):
            client = await self._http()
            try:
                await client.delete(f"{self.url_for(target, scenario.workload)}/admin/fault")
            except httpx.HTTPError as exc:
                # Leaving a fault behind poisons later scenarios, so it is a
                # warning with the target named rather than a silent pass.
                log.warning("fault not cleared", scenario=scenario.id, target=target,
                            error=str(exc))

    # ---- internals ----------------------------------------------------------

    async def _apply_secondary(
        self, scenario_id: str, fault: SecondaryFault, workload: str = ""
    ) -> None:
        if fault.start_offset_s:
            await asyncio.sleep(min(float(fault.start_offset_s), MAX_SETTLE_S))
        await self._apply(
            scenario_id,
            workload=workload,
            target=fault.target,
            mode=fault.mode,
            magnitude_ms=fault.magnitude_ms,
            error_rate=fault.error_rate,
            probability=1.0,
        )

    async def _apply(
        self,
        scenario_id: str,
        *,
        workload: str = "",
        target: str,
        mode: FaultMode,
        magnitude_ms: int,
        error_rate: float,
        probability: float,
        parameters: dict[str, Any] | None = None,
    ) -> None:
        if mode not in self.supported_modes():
            message = (
                f"fault mode {mode.value!r} needs a runtime-level injector; "
                "the workload admin API cannot apply it"
            )
            if self._strict:
                # ExternalServiceError - not SourceUnavailable - because the
                # harness maps it to ENVIRONMENT_FAILURE: the environment lacks
                # a mechanism, no evidence source is down, and either way the
                # scenario is excluded from model-quality aggregates.
                raise ExternalServiceError(
                    message,
                    context={
                        "scenario_id": scenario_id,
                        "target": target,
                        "mode": mode.value,
                        "reason": "uninjectable_fault_mode",
                    },
                )
            log.warning("fault mode not injectable here", scenario=scenario_id,
                        target=target, mode=mode.value)
            return

        payload: dict[str, Any] = {
            "mode": mode.value,
            "magnitude_ms": magnitude_ms,
            "error_rate": error_rate,
            "probability": probability,
            # Scenario parameters were declared in the schema and never sent,
            # so a scenario that distinguished itself only by a parameter -
            # a shorter timeout, a larger leak - was injected identically to
            # every other scenario of its mode.
            "parameters": parameters or {},
        }
        client = await self._http()
        url = f"{self.url_for(target, workload)}/admin/fault"
        try:
            response = await client.post(url, json=payload)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            # The scenario never happened. Raising here is what gets it counted
            # as a harness failure instead of a model failure.
            raise SourceUnavailable(
                f"could not inject {mode.value} into {target}: {exc}",
                context={"scenario_id": scenario_id, "target": target, "url": url},
            ) from exc
        log.info("fault injected", scenario=scenario_id, target=target, mode=mode.value,
                 magnitude_ms=magnitude_ms, error_rate=error_rate)


__all__ = ["MAX_SETTLE_S", "WORKLOAD_MODES", "WorkloadFaultInjector", "parse_endpoints"]
