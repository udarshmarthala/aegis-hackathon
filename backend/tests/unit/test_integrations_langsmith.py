"""LangSmith: observability that can never become a control-plane dependency.

The invariant under test is blunt - no tracing call may raise, whatever the
LangSmith client does. If one could, an outage at a third party would stop an
incident workflow.
"""

from __future__ import annotations

import pytest
from pydantic import SecretStr

from aegis.core.config import Settings
from aegis.integrations.langsmith import LangSmithIntegration


class ExplodingClient:
    """Every method fails, the way a broken or throttled client would."""

    def create_run(self, **_kwargs):
        raise RuntimeError("langsmith is down")

    def update_run(self, *_args, **_kwargs):
        raise RuntimeError("langsmith is down")

    def list_projects(self, **_kwargs):
        raise RuntimeError("langsmith is down")

    def list_datasets(self, **_kwargs):
        raise RuntimeError("langsmith is down")

    def create_dataset(self, **_kwargs):
        raise RuntimeError("langsmith is down")

    def create_examples(self, **_kwargs):
        raise RuntimeError("langsmith is down")

    def create_feedback(self, *_args, **_kwargs):
        raise RuntimeError("langsmith is down")


class RecordingClient:
    def __init__(self) -> None:
        self.runs: list[dict] = []
        self.updates: list[tuple] = []

    def create_run(self, **kwargs):
        self.runs.append(kwargs)

    def update_run(self, run_id, **kwargs):
        self.updates.append((run_id, kwargs))


def settings(*, tracing: bool = True, key: str = "lsv2_pt_testkey") -> Settings:
    return Settings(
        _env_file=None, langsmith_tracing=tracing, langsmith_api_key=SecretStr(key)
    )


def integration(client=None, **over) -> LangSmithIntegration:
    obj = LangSmithIntegration(settings(**over))
    if client is not None:
        obj._client = client
        obj._attempted = True
    return obj


def test_unconfigured_when_tracing_off_or_key_missing():
    assert integration(tracing=False).configured is False
    assert integration(key="").configured is False
    assert integration().configured is True


def test_enable_does_nothing_when_unconfigured(monkeypatch):
    """Enabling by accident would ship prompt content to a third party."""
    monkeypatch.delenv("LANGCHAIN_TRACING_V2", raising=False)
    assert integration(tracing=False).enable() is False
    import os

    assert "LANGCHAIN_TRACING_V2" not in os.environ


def test_tracing_without_a_client_is_a_silent_no_op():
    with integration(tracing=False).trace_run("triage") as handle:
        handle.set_outputs(verdict="degraded")
        handle.add_metadata(incident_id="inc_1")
    assert handle.active is False


def test_a_failing_langsmith_never_reaches_the_caller():
    obj = integration(ExplodingClient())
    with obj.trace_run("triage", inputs={"a": 1}, tags=["t"]) as handle:
        handle.set_outputs(ok=True)
    assert handle.active is False


def test_the_traced_body_still_raises():
    """Tracing failures are absorbed; the work being traced is not."""
    obj = integration(ExplodingClient())
    with pytest.raises(ZeroDivisionError), obj.trace_run("diagnose"):
        1 / 0  # noqa: B018


def test_a_body_failure_is_recorded_before_it_propagates():
    client = RecordingClient()
    obj = integration(client)
    with pytest.raises(ValueError), obj.trace_run("diagnose"):
        raise ValueError("boom")
    assert client.updates[0][1]["error"].startswith("ValueError: boom")


def test_successful_runs_are_closed_with_outputs():
    client = RecordingClient()
    obj = integration(client)
    with obj.trace_run("triage") as handle:
        handle.set_outputs(verdict="critical")
    assert client.runs[0]["name"] == "triage"
    assert client.updates[0][1]["outputs"] == {"verdict": "critical"}
    assert client.updates[0][1]["error"] is None


async def test_decorator_wraps_sync_and_async_callables():
    obj = integration(ExplodingClient())

    @obj.traced("sync-step")
    def add(a: int, b: int) -> int:
        return a + b

    @obj.traced("async-step")
    async def fetch() -> str:
        return "value"

    assert add(2, 3) == 5
    assert await fetch() == "value"


def test_dataset_and_experiment_helpers_degrade_to_falsy():
    obj = integration(ExplodingClient())
    assert obj.ensure_dataset("smoke") is None
    assert obj.add_examples("smoke", [({"a": 1}, {"b": 2})]) == 0
    assert obj.record_experiment("run-1", dataset_name="smoke", metrics={"pass": 1.0}) is None
    assert obj.record_feedback("run-1", "accuracy", 1.0) is False


def test_health_distinguishes_unconfigured_from_broken():
    assert integration(tracing=False).health()[0] is None
    healthy, reason = integration(ExplodingClient()).health()
    assert healthy is False
    assert "down" in reason
