"""Seed the operational knowledge graph from the reference workload.

Topology ingestion is idempotent by construction: every write is a MERGE on the
canonical service id, so re-running this (or a pod restarting) updates the
existing node instead of creating a duplicate (ESD section 7).

In a real environment this is driven by runtime discovery and OTel service
relationships. For the local simulator the reference topology is declared here.
"""

from __future__ import annotations

import asyncio
import os
import sys

from neo4j import AsyncGraphDatabase

# These MUST match what ``graph.TopologyIngestor`` derives from Settings -
# ``aegis_environment_name`` and ``workload_namespace``. They did not: this
# script used WORKLOAD_NAME="demo" while ingestion used
# WORKLOAD_NAMESPACE="aegis-workload", so seeding and telemetry discovery each
# produced a separate Service node for the same service. Only the seeded set
# carried the CALLS edges, so a traversal that happened to select the other one
# reported no downstream services on a graph that plainly contained them.
ENVIRONMENT = os.getenv("AEGIS_ENVIRONMENT_NAME", "local-docker")
WORKLOAD = os.getenv("WORKLOAD_NAMESPACE", os.getenv("WORKLOAD_NAME", "aegis-workload"))

# (caller, callee) edges of the reference application.
EDGES = [("gateway", "checkout"), ("checkout", "payment")]
OWNERS = {"gateway": "Platform", "checkout": "Commerce", "payment": "Payments"}

MERGE_SERVICE = """
MERGE (s:Service {service_id: $service_id})
SET s.name = $name,
    s.environment = $environment,
    s.workload = $workload,
    s.owner_team = $owner,
    s.updated_at = datetime()
"""

MERGE_CALLS = """
MATCH (a:Service {service_id: $src_id})
MATCH (b:Service {service_id: $dst_id})
MERGE (a)-[r:CALLS]->(b)
SET r.updated_at = datetime()
"""


def canonical(name: str) -> str:
    return f"{ENVIRONMENT}:{WORKLOAD}:{name}"


async def main() -> int:
    uri = os.getenv("NEO4J_URI", "bolt://localhost:7687")
    user = os.getenv("NEO4J_USER", "neo4j")
    password = os.getenv("NEO4J_PASSWORD", "")
    if not password:
        print("NEO4J_PASSWORD is not set", file=sys.stderr)
        return 2

    driver = AsyncGraphDatabase.driver(uri, auth=(user, password))
    try:
        async with driver.session() as session:
            # A uniqueness constraint is what actually guarantees identity
            # stability; the MERGE pattern alone would not survive a race.
            await session.run(
                "CREATE CONSTRAINT service_id_unique IF NOT EXISTS "
                "FOR (s:Service) REQUIRE s.service_id IS UNIQUE"
            )
            services = {n for edge in EDGES for n in edge}
            for name in sorted(services):
                await session.run(
                    MERGE_SERVICE,
                    service_id=canonical(name),
                    name=name,
                    environment=ENVIRONMENT,
                    workload=WORKLOAD,
                    owner=OWNERS.get(name, "unassigned"),
                )
            for src, dst in EDGES:
                await session.run(MERGE_CALLS, src_id=canonical(src), dst_id=canonical(dst))

            count = await (await session.run(
                "MATCH (s:Service) WHERE s.environment = $env RETURN count(s) AS n",
                env=ENVIRONMENT,
            )).single()
            edges = await (await session.run(
                "MATCH (:Service)-[r:CALLS]->(:Service) RETURN count(r) AS n"
            )).single()
            print(f"topology seeded: {count['n']} services, {edges['n']} CALLS edges "
                  f"(environment={ENVIRONMENT})")
    finally:
        await driver.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
