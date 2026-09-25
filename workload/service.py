"""Instrumented reference workload with a real fault surface.

One parametrised image runs as any node in a reference topology - an
application service, a datastore, a cache or a proxy. `SERVICE_NAME`,
`SERVICE_KIND` and `DOWNSTREAM` select behaviour, so a topology is
configuration rather than code, and the same image can stand up the reference
chain or a DeathStarBench-shaped graph.

Each instance exposes the metric names Aegis queries (`http_requests_total`,
`http_request_duration_seconds`) with a `service` label, plus the saturation
signals a responder needs to tell *why* a service is slow - pool occupancy,
queue depth, cache hit rate, CPU and memory. Latency alone cannot distinguish a
starved thread pool from a busy CPU, and asking a model to make that call from
RED metrics only is asking it to guess.

Fault injection is controlled at runtime through `/admin/fault`. Faults live in
the workload, never in Aegis, so a benchmark scenario cannot hand the agent
privileged knowledge of the injected fault (ESD section 16).

## What "real" means here

Every mode below changes the process's actual behaviour. `cpu_burn` burns CPU
on a worker thread and contends with real request handling; `memory_leak`
retains real bytes; `process_kill` really exits, and the container's restart
policy produces a real crash loop; `db_saturation` queues on a real bounded
semaphore and really times out. Nothing sleeps while pretending to be something
else, because a benchmark whose faults are theatre measures the theatre.

Three modes are honest approximations, and the approximation is stated rather
than hidden:

* `packet_loss` is applied at the application layer - the callee stops
  answering for a fraction of requests, so the caller sees the timeout that
  real loss produces. True L3 loss would need NET_ADMIN and a sidecar.
* `dns_failure` points resolution at a name that genuinely does not resolve, so
  the caller gets a real `getaddrinfo` failure, but the resolver itself is
  healthy.
* `disk_pressure` fills a bounded file under a cap rather than a real volume.

## Faults that ship in the image

`pool_leak` is gradual: every `leak_interval_s` it permanently takes
`leak_per_interval` slots of the real connection pool, so utilisation, queueing,
p99 and finally the error rate climb over a minute or two instead of stepping.
A leaked slot is released only by clearing the fault or by the process exiting.

`BAKED_FAULT` applies a mode at boot, but only when `WORKLOAD_VERSION` is listed
in `BAKED_FAULT_VERSIONS`. The fault then belongs to the image version rather
than to a runtime toggle, which is what makes the remedies behave as they would
in production: a restart re-applies it (and only buys time), a rollback to a
version without it removes it.

## Bounds

Every fault is bounded, because a benchmark that can exhaust the host is a
benchmark nobody runs twice. The leak has a hard ceiling, the disk filler has a
byte cap, CPU burn is capped per request, and every pool is finite by
construction.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import random
import socket
import sys
import tempfile
import time
from collections import OrderedDict
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Final

import httpx
from fastapi import FastAPI, Response
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)
from pydantic import BaseModel, Field

# --------------------------------------------------------------------------- #
# configuration                                                                #
# --------------------------------------------------------------------------- #

SERVICE: Final = os.getenv("SERVICE_NAME", "gateway")
# "service" | "datastore" | "cache" | "proxy". The kind only changes defaults -
# a datastore starts with a small connection pool and a slower floor, a cache
# with a fast floor and a large working set. It never changes the fault
# vocabulary, so a scenario targeting a datastore and one targeting a service
# are injected the same way.
KIND: Final = os.getenv("SERVICE_KIND", "service")
DOWNSTREAM: Final = tuple(
    d.strip() for d in os.getenv("DOWNSTREAM", "").split(",") if d.strip()
)
PORT: Final = int(os.getenv("PORT", "8080"))


def _build_info() -> dict[str, str]:
    """The identity written into the image at build time, if there is one.

    A file rather than only ENV because the runtime adapter recreates a
    container on another tag by copying the old container's environment, and
    that copy carries the *old* image's WORKLOAD_VERSION and BAKED_FAULT. A
    file inside the image cannot be overridden that way, so a 1.4.2 container
    is a 1.4.2 container however it was started.
    """
    path = Path(os.getenv("WORKLOAD_BUILD_INFO", "/app/build-info.json"))
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {str(k): str(v) for k, v in raw.items()} if isinstance(raw, dict) else {}


_BUILD_INFO: Final = _build_info()


def _identity(key: str, env: str, default: str = "") -> str:
    return _BUILD_INFO.get(key) or os.getenv(env, default)


# The image's own version wins; SERVICE_VERSION is the older per-container
# override for runs outside a built image.
VERSION_ENV: Final = _identity(
    "version", "WORKLOAD_VERSION", os.getenv("SERVICE_VERSION", "1.0.0")
)
DEPENDENCY_NAME: Final = _identity("dependency", "DEPENDENCY_NAME")
DEPENDENCY_VERSION: Final = _identity("dependency_version", "DEPENDENCY_VERSION")
BAKED_FAULT: Final = (
    _BUILD_INFO["baked_fault"] if "baked_fault" in _BUILD_INFO else os.getenv("BAKED_FAULT", "")
).strip()
BAKED_FAULT_VERSIONS: Final = frozenset(
    v.strip()
    for v in _identity("baked_fault_versions", "BAKED_FAULT_VERSIONS").split(",")
    if v.strip()
)

_KIND_DEFAULTS: Final[dict[str, dict[str, int]]] = {
    "service": {"pool": 32, "workers": 16, "cache": 256, "floor_ms": 3},
    "datastore": {"pool": 8, "workers": 8, "cache": 64, "floor_ms": 12},
    "cache": {"pool": 64, "workers": 32, "cache": 1024, "floor_ms": 1},
    "proxy": {"pool": 128, "workers": 64, "cache": 0, "floor_ms": 1},
}
_DEFAULTS: Final = _KIND_DEFAULTS.get(KIND, _KIND_DEFAULTS["service"])

POOL_SIZE: Final = int(os.getenv("POOL_SIZE", str(_DEFAULTS["pool"])))
WORKER_SLOTS: Final = int(os.getenv("WORKER_SLOTS", str(_DEFAULTS["workers"])))
CACHE_CAPACITY: Final = int(os.getenv("CACHE_CAPACITY", str(_DEFAULTS["cache"])))
FLOOR_MS: Final = int(os.getenv("FLOOR_MS", str(_DEFAULTS["floor_ms"])))

# Hard ceilings. These exist so a runaway scenario degrades the workload and
# nothing else; the benchmark must be safe to run on the machine that wrote it.
MAX_LEAK_BYTES: Final = 256 * 1024 * 1024
MAX_DISK_BYTES: Final = 128 * 1024 * 1024
MAX_CPU_BURN_MS: Final = 250
MAX_SLEEP_MS: Final = 30_000
POOL_WAIT_BUDGET_S: Final = 2.0
# pool_leak pacing. The interval floor stops a mistyped parameter turning a
# gradual leak into an instant exhaustion, which is a different incident.
LEAK_INTERVAL_DEFAULT_S: Final = 3.0
LEAK_INTERVAL_MIN_S: Final = 0.01
LEAK_PER_INTERVAL_DEFAULT: Final = 1
LEAK_PER_INTERVAL_MAX: Final = 8
# How long a request holds its connection while pool_leak is active. Requests
# must really use the pool, or leaked slots would starve nobody.
LEAK_QUERY_MS_DEFAULT: Final = 20
# A name reserved by RFC 6761 precisely so it never resolves. Using it means the
# DNS failure is a real resolver error rather than a raised exception pretending
# to be one.
UNRESOLVABLE_HOST: Final = "fault-injected.invalid"

MODES: Final[frozenset[str]] = frozenset(
    {
        "none",
        "latency",
        "error",
        "pool_exhaustion",
        "process_kill",
        "cpu_burn",
        "memory_leak",
        "disk_pressure",
        "packet_loss",
        "dns_failure",
        "dependency_outage",
        "cache_flush",
        "db_saturation",
        "config_change",
        "bad_deploy",
        "clock_skew",
        "thread_starvation",
        "pool_leak",
    }
)

# --------------------------------------------------------------------------- #
# metrics                                                                      #
# --------------------------------------------------------------------------- #

REQUESTS = Counter(
    "http_requests_total", "Total HTTP requests", ["service", "endpoint", "status"]
)
LATENCY = Histogram(
    "http_request_duration_seconds",
    "Request duration",
    ["service", "endpoint"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0),
)
POOL_EXHAUSTED = Counter(
    "connection_pool_exhausted_total", "Pool exhaustion events", ["service"]
)
POOL_IN_USE = Gauge("connection_pool_in_use", "Connections currently held", ["service"])
POOL_SIZE_G = Gauge("connection_pool_size", "Connection pool capacity", ["service"])
POOL_WAITERS = Gauge(
    "connection_pool_waiting", "Requests queued for a connection", ["service"]
)
QUEUE_DEPTH = Gauge(
    "worker_queue_depth", "Requests queued for a worker slot", ["service"]
)
CACHE_HITS = Counter("cache_hits_total", "Cache hits", ["service"])
CACHE_MISSES = Counter("cache_misses_total", "Cache misses", ["service"])
DEP_ERRORS = Counter(
    "dependency_errors_total",
    "Failed downstream calls",
    ["service", "dependency", "reason"],
)
LEAKED = Gauge("workload_leaked_bytes", "Bytes deliberately retained", ["service"])
DISK_USED = Gauge("workload_disk_used_bytes", "Bytes deliberately written", ["service"])
BUILD = Gauge(
    "workload_build_info",
    "Deployed build, 1 per version",
    ["service", "version", "dependency", "dependency_version"],
)
STARTED_AT = Gauge("workload_start_time_seconds", "Process start, unix seconds", ["service"])
FAULT_ACTIVE = Gauge(
    "workload_fault_active", "1 while a fault is injected", ["service", "mode"]
)

POOL_SIZE_G.labels(SERVICE).set(POOL_SIZE)
_STARTED_AT_VALUE: Final = time.time()
STARTED_AT.labels(SERVICE).set(_STARTED_AT_VALUE)

# --------------------------------------------------------------------------- #
# fault state                                                                  #
# --------------------------------------------------------------------------- #


class FaultState(BaseModel):
    """Runtime fault configuration.

    ``mode`` is validated against the closed vocabulary rather than accepted as
    free text: a typo'd mode that silently injects nothing is precisely the
    failure this whole module exists to prevent.
    """

    mode: str = Field(default="none")
    magnitude_ms: int = Field(default=0, ge=0)
    error_rate: float = Field(default=0.0, ge=0.0, le=1.0)
    probability: float = Field(default=1.0, ge=0.0, le=1.0)
    parameters: dict[str, Any] = Field(default_factory=dict)

    def normalised(self) -> FaultState:
        if self.mode not in MODES:
            raise ValueError(f"unknown fault mode {self.mode!r}; known: {sorted(MODES)}")
        return self.model_copy(
            update={"magnitude_ms": min(self.magnitude_ms, MAX_SLEEP_MS)}
        )


class Runtime:
    """The mutable state a fault actually damages.

    Held in one object so that clearing a fault is a single, total reset rather
    than an attempt to remember which globals were touched - the way a partial
    cleanup leaves one scenario contaminating the next.
    """

    __slots__ = (
        "fault",
        "pool",
        "workers",
        "cache",
        "leak",
        "disk_path",
        "disk_bytes",
        "version",
        "clock_offset_s",
        "downstream_timeout_s",
        "leaked_slots",
        "leak_task",
    )

    def __init__(self) -> None:
        self.fault = FaultState()
        self.pool = asyncio.Semaphore(POOL_SIZE)
        self.workers = asyncio.Semaphore(WORKER_SLOTS)
        self.cache: OrderedDict[str, float] = OrderedDict()
        self.leak: list[bytearray] = []
        self.disk_path: Path | None = None
        self.disk_bytes = 0
        self.version = VERSION_ENV
        self.clock_offset_s = 0.0
        self.downstream_timeout_s = 5.0
        self.leaked_slots = 0
        self.leak_task: asyncio.Task[None] | None = None

    def release_leak(self) -> None:
        """Stop the leaker and hand every leaked slot back to the pool.

        The task is cancelled before the slots are released so it cannot take
        one back between the two steps.
        """
        if self.leak_task is not None:
            self.leak_task.cancel()
            self.leak_task = None
        for _ in range(self.leaked_slots):
            self.pool.release()
            POOL_IN_USE.labels(SERVICE).dec()
        self.leaked_slots = 0

    def reset(self) -> None:
        """Return to a healthy baseline and release everything a fault held."""
        self.fault = FaultState()
        self.release_leak()
        self.leak.clear()
        LEAKED.labels(SERVICE).set(0)
        if self.disk_path is not None:
            with contextlib.suppress(OSError):
                self.disk_path.unlink()
            self.disk_path = None
        self.disk_bytes = 0
        DISK_USED.labels(SERVICE).set(0)
        self.version = VERSION_ENV
        self.clock_offset_s = 0.0
        self.downstream_timeout_s = 5.0
        # The cache is deliberately NOT cleared: a fault that ends should leave
        # the service warm, the way a recovered dependency does. Clearing it
        # here would make every recovery look like a cache_flush.
        _publish_fault("none")
        _publish_build(self.version)


def _publish_fault(mode: str) -> None:
    FAULT_ACTIVE.clear()
    FAULT_ACTIVE.labels(SERVICE, mode).set(0.0 if mode == "none" else 1.0)


def _publish_build(version: str) -> None:
    BUILD.clear()
    BUILD.labels(SERVICE, version, DEPENDENCY_NAME, DEPENDENCY_VERSION).set(1)


rt = Runtime()
_publish_build(rt.version)
_publish_fault("none")


def _fires() -> bool:
    """Whether this particular request is affected."""
    return random.random() <= rt.fault.probability  # noqa: S311


def _param(name: str, default: Any) -> Any:
    return rt.fault.parameters.get(name, default)


# --------------------------------------------------------------------------- #
# the faults                                                                   #
# --------------------------------------------------------------------------- #


def _burn_cpu(milliseconds: int) -> None:
    """Consume CPU for real, on a worker thread.

    A sleep would produce the same latency graph and none of the contention. An
    incident where CPU is the cause behaves differently from one where it is
    not - other requests on the same box slow down too - and a benchmark that
    cannot tell those apart is not testing diagnosis.
    """
    deadline = time.perf_counter() + min(milliseconds, MAX_CPU_BURN_MS) / 1000
    total = 0.0
    while time.perf_counter() < deadline:
        for _ in range(2000):
            total += 1.000001 * 1.000001
    _ = total


def _leak(byte_count: int) -> None:
    """Retain real memory, up to a hard ceiling.

    Bounded because a workload that OOMs the host takes the whole benchmark
    with it, and because an unbounded collection is a defect here for the same
    reason it is anywhere else.
    """
    current = sum(len(b) for b in rt.leak)
    if current >= MAX_LEAK_BYTES:
        return
    chunk = min(byte_count, MAX_LEAK_BYTES - current)
    if chunk > 0:
        rt.leak.append(bytearray(chunk))
        LEAKED.labels(SERVICE).set(current + chunk)


def _fill_disk(byte_count: int) -> None:
    """Write real bytes to a bounded file."""
    if rt.disk_bytes >= MAX_DISK_BYTES:
        return
    if rt.disk_path is None:
        handle, name = tempfile.mkstemp(prefix=f"aegis-{SERVICE}-", suffix=".fill")
        os.close(handle)
        rt.disk_path = Path(name)
    chunk = min(byte_count, MAX_DISK_BYTES - rt.disk_bytes)
    try:
        with rt.disk_path.open("ab") as fh:
            fh.write(b"\0" * chunk)
    except OSError:
        # A full or read-only filesystem is the condition being simulated, so
        # reaching it is a success, not an error to propagate.
        return
    rt.disk_bytes += chunk
    DISK_USED.labels(SERVICE).set(rt.disk_bytes)


async def leak_pool_step(count: int) -> int:
    """Permanently take up to ``count`` connection-pool slots. Returns how many.

    Bounded by the pool itself: once every slot is leaked there is nothing left
    to take. A slot busy with a request is waited for (up to the normal pool
    budget) as a leaking driver would, rather than skipped, so the leak keeps
    pace under load instead of stalling while the pool is busy.
    """
    taken = 0
    for _ in range(max(0, count)):
        if rt.leaked_slots >= POOL_SIZE:
            break
        try:
            await asyncio.wait_for(rt.pool.acquire(), timeout=POOL_WAIT_BUDGET_S)
        except TimeoutError:
            break
        # No await between the acquire and the bookkeeping, so a cancellation
        # cannot leave a slot held that release_leak does not know about.
        rt.leaked_slots += 1
        POOL_IN_USE.labels(SERVICE).inc()
        taken += 1
    return taken


async def _leak_pool_loop(interval_s: float, per_interval: int) -> None:
    while rt.leaked_slots < POOL_SIZE:
        await asyncio.sleep(interval_s)
        await leak_pool_step(per_interval)


def _start_pool_leak() -> None:
    interval = max(
        LEAK_INTERVAL_MIN_S, float(_param("leak_interval_s", LEAK_INTERVAL_DEFAULT_S))
    )
    per = min(
        LEAK_PER_INTERVAL_MAX,
        max(1, int(_param("leak_per_interval", LEAK_PER_INTERVAL_DEFAULT))),
    )
    rt.leak_task = asyncio.create_task(_leak_pool_loop(interval, per))


def baked_fault_applies(version: str, fault: str, versions: frozenset[str]) -> bool:
    """Whether the image's baked fault is live for this version.

    Keyed on the version so one Dockerfile builds both the healthy and the
    faulty tag: the faulty build lists itself, the healthy one does not.
    """
    return bool(fault) and fault != "none" and version in versions


async def _hold_pool(name: str, sem: asyncio.Semaphore, hold_ms: int) -> str | None:
    """Occupy a finite pool slot, queueing and timing out for real.

    This is the mechanism behind pool_exhaustion, db_saturation and
    thread_starvation. They differ in which pool they starve and what they
    report, not in how the starvation is faked, because it is not faked: slots
    are genuinely held and genuinely contended.
    """
    gauge = QUEUE_DEPTH if name == "worker" else POOL_WAITERS
    gauge.labels(SERVICE).inc()
    try:
        await asyncio.wait_for(sem.acquire(), timeout=POOL_WAIT_BUDGET_S)
    except TimeoutError:
        POOL_EXHAUSTED.labels(SERVICE).inc()
        return {"connection": "pool_exhausted", "db": "db_timeout"}.get(
            name, "worker_timeout"
        )
    finally:
        gauge.labels(SERVICE).dec()

    if name != "worker":
        POOL_IN_USE.labels(SERVICE).inc()
    try:
        await asyncio.sleep(min(hold_ms, MAX_SLEEP_MS) / 1000)
    finally:
        sem.release()
        if name != "worker":
            POOL_IN_USE.labels(SERVICE).dec()
    return None


def _cache_lookup(key: str) -> bool:
    """True on a hit. A cold cache costs the caller real time."""
    if CACHE_CAPACITY <= 0:
        return True
    if key in rt.cache:
        rt.cache.move_to_end(key)
        CACHE_HITS.labels(SERVICE).inc()
        return True
    CACHE_MISSES.labels(SERVICE).inc()
    rt.cache[key] = time.time()
    while len(rt.cache) > CACHE_CAPACITY:
        rt.cache.popitem(last=False)
    return False


async def apply_fault() -> str | None:
    """Run the active fault against this request.

    Returns an error label when the request must fail, ``None`` when it should
    proceed. Every branch either changes real process state or really delays
    the caller; none of them merely records that a fault happened.
    """
    mode = rt.fault.mode
    magnitude = rt.fault.magnitude_ms

    # The floor is what a healthy instance of this kind costs, so a "fast"
    # datastore is still slower than a cache and a topology has believable
    # shape before anything is injected.
    if FLOOR_MS:
        await asyncio.sleep(FLOOR_MS / 1000)

    if mode == "none" or not _fires():
        return None

    if mode == "latency":
        await asyncio.sleep(min(magnitude, MAX_SLEEP_MS) / 1000)
        return None

    if mode == "error":
        return "injected_error" if random.random() < rt.fault.error_rate else None  # noqa: S311

    if mode == "pool_exhaustion":
        return await _hold_pool("connection", rt.pool, magnitude or 500)

    if mode == "pool_leak":
        # Every request needs a connection. The leak is the background task;
        # what the caller experiences is a pool that keeps getting smaller.
        return await _hold_pool(
            "connection", rt.pool, int(_param("query_ms", LEAK_QUERY_MS_DEFAULT))
        )

    if mode == "db_saturation":
        return await _hold_pool("db", rt.pool, magnitude or 800)

    if mode == "thread_starvation":
        return await _hold_pool("worker", rt.workers, magnitude or 600)

    if mode == "cpu_burn":
        # to_thread keeps the event loop responsive enough to answer /metrics
        # while the box is genuinely busy - otherwise the scrape fails and the
        # incident looks like an outage rather than saturation.
        await asyncio.to_thread(_burn_cpu, magnitude or 40)
        return None

    if mode == "memory_leak":
        _leak(int(_param("bytes_per_request", 512 * 1024)))
        # A leak shows as latency long before it shows as a kill: the allocator
        # and the collector both get slower as the heap grows.
        await asyncio.sleep(min(len(rt.leak), 200) / 4000)
        return None

    if mode == "disk_pressure":
        await asyncio.to_thread(
            _fill_disk, int(_param("bytes_per_request", 1024 * 1024))
        )
        return "disk_full" if rt.disk_bytes >= MAX_DISK_BYTES else None

    if mode == "cache_flush":
        if not _cache_lookup(str(_param("key", "hot"))):
            await asyncio.sleep(min(magnitude or 150, MAX_SLEEP_MS) / 1000)
        return None

    if mode == "process_kill":
        # Really exit. `restart: unless-stopped` brings the container back, so
        # the scenario produces a genuine crash loop with a real gap in the
        # series and a real start-time reset - none of which a simulated
        # "restart" would generate.
        await asyncio.sleep(min(magnitude, 5_000) / 1000)
        sys.stdout.flush()
        os._exit(137)

    if mode == "bad_deploy":
        await asyncio.sleep(min(magnitude or 120, MAX_SLEEP_MS) / 1000)
        return "bad_deploy_error" if random.random() < rt.fault.error_rate else None  # noqa: S311

    # dependency_outage, packet_loss, dns_failure, config_change and clock_skew
    # act on the downstream call or on standing configuration rather than on
    # this request directly.
    return None


# --------------------------------------------------------------------------- #
# downstream                                                                   #
# --------------------------------------------------------------------------- #


async def _call_downstream(client: httpx.AsyncClient, target: str) -> tuple[str, Any]:
    """Call one dependency, honouring the faults that live on the call path."""
    mode = rt.fault.mode
    host = target

    if mode == "dependency_outage" and _fires():
        DEP_ERRORS.labels(SERVICE, target, "outage").inc()
        return target, "DependencyOutage"

    if mode == "dns_failure" and _fires():
        # A name that genuinely does not resolve, so the caller experiences a
        # real resolver failure with a real error type and real timing.
        host = UNRESOLVABLE_HOST

    if mode == "packet_loss" and _fires():
        # The callee stops answering. From this side that is indistinguishable
        # from loss: the request is sent, nothing comes back, the client times
        # out. See the module docstring for what this does not reproduce.
        await asyncio.sleep(rt.downstream_timeout_s + 0.5)
        DEP_ERRORS.labels(SERVICE, target, "timeout").inc()
        return target, "PacketLossTimeout"

    try:
        resp = await client.get(
            f"http://{host}:{PORT}/work", timeout=rt.downstream_timeout_s
        )
    except (httpx.HTTPError, socket.gaierror) as exc:
        reason = "dns" if host == UNRESOLVABLE_HOST else type(exc).__name__.lower()
        DEP_ERRORS.labels(SERVICE, target, reason).inc()
        return target, type(exc).__name__
    if resp.status_code >= 500:
        DEP_ERRORS.labels(SERVICE, target, str(resp.status_code)).inc()
    return target, resp.status_code


# --------------------------------------------------------------------------- #
# app                                                                          #
# --------------------------------------------------------------------------- #


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    app.state.client = httpx.AsyncClient(timeout=rt.downstream_timeout_s)
    if baked_fault_applies(VERSION_ENV, BAKED_FAULT, BAKED_FAULT_VERSIONS):
        # Applied on every boot, so a restart resets the damage and then the
        # same image starts doing it again.
        apply_fault_state(FaultState(mode=BAKED_FAULT).normalised())
    yield
    rt.release_leak()
    await app.state.client.aclose()


app = FastAPI(title=f"workload-{SERVICE}", lifespan=lifespan)


@app.get("/work")
async def work() -> Any:
    """The traced business endpoint. Calls every configured downstream."""
    started = time.perf_counter()
    endpoint = "/work"
    status = "200"
    downstream: dict[str, Any] = {}

    try:
        failure = await apply_fault()
        if failure:
            status = "500"
            return Response(
                content=f'{{"error":"{failure}","service":"{SERVICE}"}}',
                status_code=500,
                media_type="application/json",
            )

        for target in DOWNSTREAM:
            name, outcome = await _call_downstream(app.state.client, target)
            downstream[name] = outcome
            if not isinstance(outcome, int):
                status = "503"
            elif outcome >= 500:
                status = "500"

        body = {
            "service": SERVICE,
            "version": rt.version,
            "downstream": downstream,
            "status": status,
            # Offset by clock_skew when injected, so a scenario about
            # disagreeing clocks has something that actually disagrees.
            "observed_at": time.time() + rt.clock_offset_s,
        }
        # A failed dependency has to fail *this* request too. Recording 503 on
        # the metric while answering 200 to the caller means the failure stops
        # here: the caller sees success, never degrades, and every
        # cascading-failure scenario becomes unreachable because the cascade
        # cannot physically propagate past the first hop.
        if status == "200":
            return body
        return Response(
            content=json.dumps(body),
            status_code=int(status),
            media_type="application/json",
        )
    finally:
        REQUESTS.labels(SERVICE, endpoint, status).inc()
        LATENCY.labels(SERVICE, endpoint).observe(time.perf_counter() - started)


@app.get("/health")
async def health() -> dict[str, Any]:
    return {
        "status": "ok",
        "service": SERVICE,
        "kind": KIND,
        "version": rt.version,
        "fault": rt.fault.mode,
        "downstream": list(DOWNSTREAM),
        "dependency": DEPENDENCY_NAME,
        "dependency_version": DEPENDENCY_VERSION,
    }


@app.get("/metrics")
async def metrics() -> Response:
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.post("/admin/fault")
async def set_fault(state: FaultState) -> Any:
    """Inject or clear a fault. Deliberately outside Aegis.

    Modes that change standing configuration rather than per-request behaviour
    are applied here, once, so that the request path stays a single pass with
    no configuration branches in it.
    """
    try:
        normalised = state.normalised()
    except ValueError as exc:
        # A rejected mode must be a 400 the injector can see, not a silent
        # no-op: injecting nothing and reporting success is how a benchmark
        # ends up scoring a healthy system against a fault nobody applied.
        return Response(
            content=f'{{"error":"{exc}","service":"{SERVICE}"}}',
            status_code=400,
            media_type="application/json",
        )

    apply_fault_state(normalised)
    return {"applied": rt.fault.model_dump(), "service": SERVICE, "version": rt.version}


def apply_fault_state(normalised: FaultState) -> None:
    """Replace the active fault. Shared by the admin endpoint and by boot."""
    rt.reset()
    rt.fault = normalised
    _publish_fault(normalised.mode)

    if normalised.mode == "pool_leak":
        _start_pool_leak()
    elif normalised.mode == "cache_flush":
        # The point of the scenario: everything that was warm is now cold, and
        # latency stays high until the working set is rebuilt.
        rt.cache.clear()
    elif normalised.mode == "config_change":
        # A timeout shorter than the downstream's real response time. Nothing
        # errors at the source; the caller simply starts timing out, which is
        # what makes this scenario hard and worth having.
        rt.downstream_timeout_s = max(0.01, float(_param("downstream_timeout_s", 0.05)))
    elif normalised.mode == "bad_deploy":
        rt.version = str(_param("version", "2.0.0"))
        _publish_build(rt.version)
    elif normalised.mode == "clock_skew":
        rt.clock_offset_s = float(_param("offset_s", 300.0))


@app.delete("/admin/fault")
async def clear_fault() -> dict[str, Any]:
    rt.reset()
    return {"cleared": True, "service": SERVICE, "version": rt.version}


@app.get("/admin/state")
async def admin_state() -> dict[str, Any]:
    """What the fault actually did, for the harness to assert against.

    A benchmark should be able to check that injection had an effect rather
    than trusting that a 200 on ``/admin/fault`` meant something happened.
    """
    return {
        "service": SERVICE,
        "kind": KIND,
        "version": rt.version,
        "fault": rt.fault.model_dump(),
        "leaked_bytes": sum(len(b) for b in rt.leak),
        "leaked_pool_slots": rt.leaked_slots,
        "disk_bytes": rt.disk_bytes,
        "cache_entries": len(rt.cache),
        "pool_size": POOL_SIZE,
        "worker_slots": WORKER_SLOTS,
        "downstream_timeout_s": rt.downstream_timeout_s,
        "clock_offset_s": rt.clock_offset_s,
        "started_at": _STARTED_AT_VALUE,
    }


@app.get("/admin/modes")
async def admin_modes() -> dict[str, Any]:
    """The fault vocabulary this build implements.

    The injector reads this to decide what it can apply, so a mode can never be
    listed as supported in one place and missing in another.
    """
    return {"service": SERVICE, "modes": sorted(MODES)}
