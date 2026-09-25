"""Topology availability must be re-evaluated, never latched at boot.

Neo4j accepts bolt connections before it accepts writes, so a control-plane
process that starts alongside it loses that race routinely. The failure mode
being guarded here is not the race itself - it is a process that runs for
months still reporting topology as unavailable because Neo4j was slow to start
once, long after the graph became usable.
"""

from __future__ import annotations

import pytest

from aegis.container import Container
from aegis.core.config import Settings


class _Ingestor:
    """Fails for the first ``fail_times`` calls, then succeeds."""

    def __init__(self, fail_times: int) -> None:
        self.fail_times = fail_times
        self.calls = 0

    async def ensure_schema(self) -> int:
        self.calls += 1
        if self.calls <= self.fail_times:
            raise ConnectionError("neo4j is not accepting writes yet")
        return 32


def _container(ingestor: object | None) -> Container:
    c = Container(settings=Settings(postgres_password="x"))
    c.graph_ingest = ingestor
    return c


@pytest.mark.asyncio
async def test_transient_startup_failure_is_retried_not_latched() -> None:
    """A slow Neo4j start is survived within a single call."""
    ingestor = _Ingestor(fail_times=2)
    container = _container(ingestor)

    assert await container.ensure_graph_schema() is True
    assert ingestor.calls == 3
    assert container.capabilities["graph"].configured is True


@pytest.mark.asyncio
async def test_capability_recovers_on_a_later_call() -> None:
    """Exhausting the retries marks graph unavailable - and does not pin it there."""
    ingestor = _Ingestor(fail_times=99)
    container = _container(ingestor)

    assert await container.ensure_graph_schema() is False
    assert container.capabilities["graph"].configured is False
    assert container.capabilities["graph"].reason

    # Neo4j comes back. The next call must re-evaluate rather than trust the
    # boot-time verdict.
    ingestor.fail_times = 0
    assert await container.ensure_graph_schema() is True
    assert container.capabilities["graph"].configured is True
    assert container.capabilities["graph"].reason == ""


@pytest.mark.asyncio
async def test_no_graph_client_is_not_reported_as_a_failure() -> None:
    """An unconfigured graph is a different state from one that failed."""
    container = _container(None)

    assert await container.ensure_graph_schema() is False
    # Nothing was attempted, so nothing may be claimed about why it failed.
    assert "graph" not in container.capabilities
