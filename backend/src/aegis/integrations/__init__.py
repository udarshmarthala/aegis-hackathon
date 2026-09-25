"""External integrations and the environment adapter boundary.

Everything Aegis touches outside its own datastores lives here: GitHub for change
evidence, Slack for notification, LangSmith for tracing, and the runtime adapters
that observe and act on the workload itself.

Two conventions hold across the whole package.

**Read and write are structurally separate.** Every client exposes
``READ_METHODS`` and ``WRITE_METHODS`` as disjoint frozensets. The MCP tool layer
may expose the read names directly; a write name must pass the gate chain first.

**A missing integration is a state, not a crash.** ``health`` reports what is
configured and what is reachable, and every client raises a typed error naming
the reason rather than returning a fabricated result. "Not configured",
"unreachable" and "found nothing" are three different answers and stay that way
all the way to the UI.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx

from aegis.core.config import Settings
from aegis.core.errors import is_unset
from aegis.core.logging import get_logger
from aegis.integrations.github import GitHubClient, GitHubRateLimited
from aegis.integrations.langsmith import LangSmithIntegration, RunHandle
from aegis.integrations.runtime import (
    ComposeAdapter,
    EcsAdapter,
    InstanceInfo,
    KubernetesAdapter,
    LogChunk,
    RuntimeAdapter,
    WriteResult,
    get_adapter,
)
from aegis.integrations.slack import SlackClient, SlackMessageRef

log = get_logger(__name__)

# A health probe must never be the slow thing on a page load, and it must never
# hold a request open while a dead dependency times out on its own schedule.
HEALTH_PROBE_TIMEOUT_S = 3.0


def _entry(configured: bool, healthy: bool | None, reason: str) -> dict[str, Any]:
    """One row of the integration-health surface.

    ``healthy is None`` means "not probed", which is distinct from ``False``
    meaning "probed and failed". Collapsing them would show an unconfigured
    integration as a broken one and send someone debugging a non-problem.
    """
    return {"configured": configured, "healthy": healthy, "reason": reason}


async def _probe_url(url: str, path: str) -> tuple[bool, str]:
    """Bounded GET used for the telemetry sources' own readiness endpoints."""
    try:
        async with httpx.AsyncClient(base_url=url, timeout=HEALTH_PROBE_TIMEOUT_S) as client:
            resp = await client.get(path)
    except Exception as exc:  # noqa: BLE001 - a probe reports, it never raises
        return False, type(exc).__name__
    return (True, "") if resp.is_success else (False, f"http {resp.status_code}")


async def _github(settings: Settings) -> dict[str, Any]:
    client = GitHubClient(settings)
    if not client.configured:
        return _entry(False, None, "github_token is not set")
    try:
        remaining = await client.rate_limit()
    except GitHubRateLimited as exc:
        # Reachable and authenticated - just out of quota. That is degraded, not
        # down, and an operator needs to see the difference.
        return _entry(True, False, f"rate limited until {exc.context.get('reset_at', '')}")
    except Exception as exc:  # noqa: BLE001
        return _entry(True, False, type(exc).__name__)
    finally:
        await client.close()
    return _entry(True, True, f"{remaining.get('remaining', 0)} calls remaining")


async def _slack(settings: Settings) -> dict[str, Any]:
    client = SlackClient(settings)
    if not client.configured:
        return _entry(False, None, client.unconfigured_reason)
    # Deliberately unprobed: the only way to test a Slack credential is to post,
    # and a health check that pages a channel is worse than no health check.
    return _entry(True, None, "configured; not probed because any probe would post a message")


async def _langsmith(settings: Settings) -> dict[str, Any]:
    integration = LangSmithIntegration(settings)
    healthy, reason = await asyncio.to_thread(integration.health)
    return _entry(integration.configured, healthy, reason)


async def _runtime(settings: Settings, adapter: RuntimeAdapter | None) -> dict[str, Any]:
    owned = adapter is None
    try:
        adapter = adapter or get_adapter(settings)
    except Exception as exc:  # noqa: BLE001
        return _entry(False, None, str(exc))
    try:
        if not adapter.available:
            return _entry(False, None, adapter.unavailable_reason)
        healthy = await adapter.ping()
        return _entry(True, healthy, "" if healthy else "adapter did not answer its probe")
    except Exception as exc:  # noqa: BLE001
        return _entry(True, False, type(exc).__name__)
    finally:
        if owned:
            await adapter.close()


async def health(
    settings: Settings, *, adapter: RuntimeAdapter | None = None
) -> dict[str, dict[str, Any]]:
    """Per-integration status for the API's integration-health surface.

    Never raises and never blocks on a dead dependency: every probe is bounded
    and any failure is reported as a row rather than propagated. An operator
    looking at this page during an incident is already having a bad day.
    """
    names = ("github", "slack", "langsmith", "runtime", "tempo", "loki")
    results = await asyncio.gather(
        _github(settings),
        _slack(settings),
        _langsmith(settings),
        _runtime(settings, adapter),
        _telemetry(settings.tempo_url, "/ready", "tempo_url"),
        _telemetry(settings.loki_url, "/ready", "loki_url"),
        return_exceptions=True,
    )

    out: dict[str, dict[str, Any]] = {}
    for name, result in zip(names, results, strict=True):
        if isinstance(result, BaseException):
            log.warning("integration health probe failed", integration=name, error=str(result))
            out[name] = _entry(False, None, f"probe failed: {type(result).__name__}")
        else:
            out[name] = result
    return out


async def _telemetry(url: str, path: str, setting_name: str) -> dict[str, Any]:
    if is_unset(url):
        return _entry(False, None, f"not deployed ({setting_name.upper()} is empty)")
    healthy, reason = await _probe_url(url, path)
    return _entry(True, healthy, reason)


__all__ = [
    "ComposeAdapter",
    "EcsAdapter",
    "GitHubClient",
    "GitHubRateLimited",
    "InstanceInfo",
    "KubernetesAdapter",
    "LangSmithIntegration",
    "LogChunk",
    "RunHandle",
    "RuntimeAdapter",
    "SlackClient",
    "SlackMessageRef",
    "WriteResult",
    "get_adapter",
    "health",
]
