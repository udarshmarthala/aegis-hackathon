"""The workload's ``pool_leak`` fault and the image-baked fault switch.

INC-043 depends on three properties of the reference workload, and each is a
test here because each, if lost, silently changes what the remedies prove:

* the leak is **gradual** - a pool that exhausts in one step is a different
  incident, and the heartbeat would never see it coming;
* it is **bounded by the pool** and **released on clear**, so a reset really
  returns the service to health;
* the baked fault applies **only to the version that lists it**, so the healthy
  and faulty tags built from one Dockerfile really differ, and a restart of the
  faulty one re-applies it.

The workload is not a backend package, so it is loaded from its file with a
small pool. Zero network: requests go through FastAPI's in-process client.
"""

from __future__ import annotations

import asyncio
import importlib.util
import os
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from fastapi.testclient import TestClient
from prometheus_client import REGISTRY

WORKLOAD = Path(__file__).resolve().parents[3] / "workload" / "service.py"
MODULE_NAME = "aegis_test_workload_service"
POOL = 4


def _load() -> ModuleType:
    # Imported once per session: the module registers Prometheus collectors on
    # the default registry, and a second import would collide with the first.
    if MODULE_NAME in sys.modules:
        return sys.modules[MODULE_NAME]
    os.environ.update(
        {"SERVICE_NAME": "checkout", "POOL_SIZE": str(POOL), "FLOOR_MS": "0"}
    )
    spec = importlib.util.spec_from_file_location(MODULE_NAME, WORKLOAD)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[MODULE_NAME] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def wl() -> Any:
    module = _load()
    module.rt.reset()
    yield module
    module.rt.reset()


def _gauge(name: str) -> float:
    value = REGISTRY.get_sample_value(name, {"service": "checkout"})
    return float(value or 0.0)


async def test_a_leak_step_takes_real_slots_and_is_bounded_by_the_pool(wl: Any) -> None:
    assert await wl.leak_pool_step(3) == 3
    assert wl.rt.leaked_slots == 3
    assert _gauge("connection_pool_in_use") == 3

    # Only one slot is left; asking for more cannot exceed the pool.
    assert await wl.leak_pool_step(5) == 1
    assert wl.rt.leaked_slots == POOL
    assert await wl.leak_pool_step(1) == 0
    assert wl.rt.pool.locked()


async def test_the_leak_is_gradual_not_an_instant_exhaustion(wl: Any) -> None:
    wl.apply_fault_state(
        wl.FaultState(
            mode="pool_leak",
            parameters={"leak_interval_s": 0.05, "leak_per_interval": 1},
        ).normalised()
    )
    # Nothing leaks at the moment of injection: the first slot goes after one
    # interval, so utilisation has a slope for the heartbeat to see.
    assert wl.rt.leaked_slots == 0

    observed: list[int] = []
    for _ in range(200):
        await asyncio.sleep(0.01)
        observed.append(wl.rt.leaked_slots)
        if wl.rt.leaked_slots >= POOL:
            break

    assert observed[-1] == POOL
    assert max(observed) == POOL  # never more than the pool
    distinct = sorted(set(observed))
    # Every intermediate level was visited, one slot per interval.
    assert distinct == list(range(distinct[0], POOL + 1))
    assert len(distinct) >= POOL - 1


async def test_clearing_the_fault_returns_every_leaked_slot(wl: Any) -> None:
    wl.apply_fault_state(
        wl.FaultState(
            mode="pool_leak", parameters={"leak_interval_s": 0.01, "leak_per_interval": 2}
        ).normalised()
    )
    for _ in range(200):
        await asyncio.sleep(0.01)
        if wl.rt.leaked_slots >= POOL:
            break
    assert wl.rt.leaked_slots == POOL
    task = wl.rt.leak_task

    wl.rt.reset()

    assert wl.rt.leaked_slots == 0
    assert wl.rt.leak_task is None
    await asyncio.sleep(0)
    # Finished (the pool filled) or cancelled - either way it leaks no more.
    assert task is not None and task.done()
    assert _gauge("connection_pool_in_use") == 0
    # The whole pool is usable again, immediately.
    for _ in range(POOL):
        await asyncio.wait_for(wl.rt.pool.acquire(), timeout=0.1)
    for _ in range(POOL):
        wl.rt.pool.release()


async def test_an_exhausted_pool_fails_requests_for_real(
    wl: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(wl, "POOL_WAIT_BUDGET_S", 0.05)
    wl.rt.fault = wl.FaultState(mode="pool_leak")
    await wl.leak_pool_step(POOL)

    assert await wl.apply_fault() == "pool_exhausted"


@pytest.mark.parametrize(
    ("version", "fault", "versions", "expected"),
    [
        ("1.4.2", "pool_leak", frozenset({"1.4.2"}), True),
        ("1.4.1", "pool_leak", frozenset({"1.4.2"}), False),
        ("1.4.1", "", frozenset({"1.4.1"}), False),
        ("1.4.2", "none", frozenset({"1.4.2"}), False),
    ],
)
def test_a_baked_fault_applies_only_to_the_version_that_lists_it(
    wl: Any, version: str, fault: str, versions: frozenset[str], expected: bool
) -> None:
    assert wl.baked_fault_applies(version, fault, versions) is expected


def test_the_faulty_version_boots_leaking_and_the_healthy_one_does_not(
    wl: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(wl, "BAKED_FAULT", "pool_leak")
    monkeypatch.setattr(wl, "BAKED_FAULT_VERSIONS", frozenset({"1.4.2"}))

    monkeypatch.setattr(wl, "VERSION_ENV", "1.4.2")
    with TestClient(wl.app) as client:
        assert client.get("/health").json()["fault"] == "pool_leak"
    # Shutdown hands the slots back, as process exit would.
    assert wl.rt.leaked_slots == 0

    # Booting again re-applies it: a restart only buys time.
    with TestClient(wl.app) as client:
        assert client.get("/health").json()["fault"] == "pool_leak"

    wl.rt.reset()
    monkeypatch.setattr(wl, "VERSION_ENV", "1.4.1")
    with TestClient(wl.app) as client:
        assert client.get("/health").json()["fault"] == "none"


def test_pool_leak_is_in_the_closed_vocabulary_and_build_info_names_the_dependency(
    wl: Any,
) -> None:
    with TestClient(wl.app) as client:
        assert "pool_leak" in client.get("/admin/modes").json()["modes"]
        text = client.get("/metrics").text
        assert 'workload_build_info{dependency="' in text
        assert "dependency_version=" in text
        rejected = client.post("/admin/fault", json={"mode": "pool_leek"})
        assert rejected.status_code == 400
