"""The production wiring for the step loop, built without any infrastructure.

Unit tests for the orchestrator use hand-built deps; this guards the one place
that assembles the real ones, so a renamed container attribute or constructor
cannot silently leave the worker without a gate or a brain.
"""

from __future__ import annotations

from aegis.agents.horizon.orchestrator import HorizonDeps
from aegis.container import build_container
from aegis.core.config import Settings
from aegis.worker.horizon_deps import build_horizon_deps


def _settings() -> Settings:
    # Explicit values so the developer's .env cannot change what is tested.
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        aegis_mode="scripted",
        rawtree_write_key="",
        rawtree_read_key="",
        nimble_api_key="",
        bfl_api_key="",
    )


def test_container_builds_every_horizon_collaborator_without_io():
    container = build_container(_settings())

    assert container.horizon_store is not None
    assert container.brain is not None
    # Unconfigured sponsors still exist - they answer with their fallback.
    assert container.rawtree is not None
    assert container.known_issues is not None
    assert container.incident_map is not None
    for name in ("brain", "rawtree", "nimble", "flux"):
        assert name in container.capabilities


def test_deps_carry_the_real_write_path():
    container = build_container(_settings())
    deps = build_horizon_deps(container, container.settings, bus=object())

    assert isinstance(deps, HorizonDeps)
    # The write path must be the container's own gate and execution service,
    # never a stand-in: a proposal only reaches the runtime through them.
    assert deps.gate is container.gate
    assert deps.execution is container.execution
    assert deps.actions is container.actions
    assert deps.health_probe is not None


def test_unconfigured_sponsors_are_reported_not_hidden():
    container = build_container(_settings())
    caps = container.capability_report()

    assert caps["rawtree"]["configured"] is False
    assert caps["nimble"]["configured"] is False
    assert caps["flux"]["configured"] is False
    assert caps["rawtree"]["reason"]
