"""INC-043 fault injection: deploy checkout 1.4.2, the build that leaks its pool.

This is an *operator* action, never an agent one. It goes through the same
runtime adapter the executors use (a Compose recreate onto another image tag),
and it is recorded in ``deployment_attempts`` exactly as a deploy pipeline would
record it, because the rollback the agent later proposes is checked against
that history - an unrecorded deploy would leave it nothing to roll back to.

The version that was running before is recorded too, as an observed baseline,
when no earlier row names it: Compose started it, not Aegis, and without that
row 1.4.1 would be an unknown version the rollback executor rightly refuses.

Idempotent: if checkout already runs 1.4.2 this reports so and changes nothing.

Usage (from the repository root, stack running):
    backend/.venv/Scripts/python.exe scripts/inject_043.py      # or: make inject-043
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend" / "src"))

SERVICE = "checkout"
GOOD_VERSION = "1.4.1"
BAD_VERSION = "1.4.2"
IMAGE_REPOSITORY = "aegis-2.0-workload"


def use_host_endpoints() -> None:
    """Point settings at the published ports when running outside a container.

    .env holds the in-network names (postgres:5432, the Unix socket) that the
    containers use; from the host the same services are on the published ports
    and, on Windows, the Docker named pipe.
    """
    if Path("/.dockerenv").exists():
        return
    os.environ["POSTGRES_HOST"] = os.getenv("AEGIS_HOST_PG_HOST", "localhost")
    os.environ["POSTGRES_PORT"] = os.getenv("POSTGRES_PUBLISH_PORT", "55433")
    os.environ["SANDBOX_DOCKER_HOST"] = os.getenv("DOCKER_HOST") or (
        "npipe:////./pipe/docker_engine" if os.name == "nt" else "unix:///var/run/docker.sock"
    )


def canonical_service_id(settings: Any, name: str = SERVICE) -> str:
    # The same identity the executors key deployment history on.
    from aegis.domain.models import ServiceRef

    return ServiceRef.build(
        settings.aegis_environment_name, settings.workload_namespace, name
    ).service_id


async def current_version(adapter: Any, service: str = SERVICE) -> str | None:
    state = await adapter.get_service(service)
    return state.version


async def ensure_baseline(db: Any, deployments: Any, settings: Any, version: str) -> None:
    """Record the running version as observed, if nothing has recorded it."""
    from aegis.persistence.patches import DeploymentState

    service_id = canonical_service_id(settings)
    known = await db.fetchval(
        """
        SELECT count(*) FROM deployment_attempts
         WHERE service_id = $1 AND to_version = $2
           AND state IN ('DEPLOYED','VERIFIED','ROLLED_BACK')
        """,
        service_id, version,
    )
    if known:
        return
    attempt = await deployments.start(
        environment=settings.aegis_environment_name,
        service_id=service_id,
        strategy="observed",
        detail={"actor": "operator:inject-043", "kind": "baseline_observed"},
    )
    await deployments.finish(
        attempt.id,
        state=DeploymentState.DEPLOYED,
        to_version=version,
        detail={"note": "running before INC-043; started by compose, not by Aegis"},
    )
    print(f"  recorded the running {version} as the observed baseline ({attempt.id})")


async def redeploy(
    *, to_version: str, actor: str, scenario: str, baseline: bool
) -> int:
    """Recreate checkout on ``to_version`` through the adapter, recorded."""
    use_host_endpoints()
    from aegis.core.config import get_settings
    from aegis.integrations.runtime import get_adapter
    from aegis.persistence.db import Database
    from aegis.persistence.patches import DeploymentRepository, DeploymentState

    settings = get_settings()
    adapter = get_adapter(settings)
    if not adapter.available:
        print(f"runtime adapter unavailable: {adapter.unavailable_reason}", file=sys.stderr)
        return 2

    image = f"{IMAGE_REPOSITORY}:{to_version}"
    try:
        adapter._docker().images.get(image)
    except Exception:  # noqa: BLE001 - any lookup failure means we cannot deploy it
        print(f"image {image} is not built. Run: make workload-images", file=sys.stderr)
        return 2

    before = await current_version(adapter)
    if before == to_version:
        print(f"{SERVICE} already runs {to_version}; nothing to do")
        return 0

    db = Database(settings)
    await db.connect()
    try:
        deployments = DeploymentRepository(db)
        if baseline and before:
            await ensure_baseline(db, deployments, settings, before)

        attempt = await deployments.start(
            environment=settings.aegis_environment_name,
            service_id=canonical_service_id(settings),
            strategy="recreate",
            detail={"actor": actor, "kind": "deploy", "scenario": scenario,
                    "requested_version": to_version},
        )
        print(f"deploying {SERVICE} {before} -> {to_version} ({attempt.id})")
        try:
            result = await adapter.rollback_deployment(
                SERVICE, to_version, idempotency_key=f"{scenario}:{attempt.id}"
            )
        except Exception as exc:  # noqa: BLE001 - recorded as FAILED, then reported
            await deployments.finish(
                attempt.id, state=DeploymentState.FAILED, error=str(exc)[:500],
                from_version=before,
            )
            print(f"deploy failed: {exc}", file=sys.stderr)
            return 1

        after = await current_version(adapter)
        await deployments.finish(
            attempt.id,
            state=(
                DeploymentState.DEPLOYED
                if result.succeeded and after == to_version
                else DeploymentState.FAILED
            ),
            from_version=before,
            to_version=after,
            detail={"performed": result.performed, "adapter_detail": result.detail},
            error=None if after == to_version else f"runtime reports {after}",
        )
        print(f"  {result.performed}: {result.detail}")
        print(f"  {SERVICE} now runs {after}")
        return 0 if after == to_version else 1
    finally:
        await db.close()
        await adapter.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.parse_args()
    return asyncio.run(
        redeploy(
            to_version=BAD_VERSION,
            actor="operator:inject-043",
            scenario="inject-043",
            baseline=True,
        )
    )


if __name__ == "__main__":
    raise SystemExit(main())
