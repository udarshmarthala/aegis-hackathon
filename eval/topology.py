"""Reference topologies: the graphs the scenario corpus is written against.

A scenario's answer key names services, a causal dependency and a blast radius.
It never inspects business logic. So what a benchmark environment has to
reproduce is the *shape* of a system - which services exist, which calls which,
and what kind of node each one is - not the application that happens to have
that shape.

That is what this module builds. Each topology is one YAML file; every node runs
the same instrumented image as the reference workload, so the full 17-mode fault
vocabulary applies to all of them uniformly. Standing up real DeathStarBench
would add forty containers of hotel-booking and social-graph logic that no
evaluator reads, and would still need a fault surface it does not expose.

Two properties are enforced rather than trusted:

* **The graph is acyclic.** Two services that call each other recurse until
  something times out, and every scenario in that topology would then fail for
  a reason unrelated to its fault - the worst kind of benchmark result, because
  it looks like a model failure.
* **Every edge has a node.** A `calls:` entry naming a service that does not
  exist would resolve to an unreachable hostname at runtime, and the scenario
  would be scored against a fault that reached nothing.

The compose file is generated rather than hand-written because the alternative
is twenty-seven hand-maintained service blocks whose ports, aliases and
downstream lists must agree with the injector's endpoint map. They would not
agree for long, and the failure would be silent.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

import yaml

TOPOLOGY_DIR: Final = Path(__file__).parent / "topologies"
GENERATED: Final = (
    Path(__file__).parent.parent / "infra" / "docker" / "topologies.generated.yml"
)
# Prometheus file_sd. One file per topology so that a topology can be added or
# removed without rewriting a shared list, and so a stale file is obvious.
TARGETS_DIR: Final = Path(__file__).parent.parent / "infra" / "prometheus" / "targets"

KINDS: Final = frozenset({"service", "datastore", "cache", "proxy"})

# Host ports are allocated from one block per topology so that two topologies
# can be defined without colliding, and so a reader can tell from the port which
# environment answered. Nothing else in the repo publishes above 18000.
PORT_BLOCKS: Final[dict[str, int]] = {
    "reference": 18000,
    "hotelreservation": 18100,
    "socialnetwork": 18200,
}

WORKLOAD_IMAGE: Final = "aegis-2.0-workload:latest"


class TopologyError(ValueError):
    """A topology that cannot be generated. Raised, never worked around."""


@dataclass(frozen=True, slots=True)
class Node:
    name: str
    kind: str
    calls: tuple[str, ...]
    port: int


@dataclass(frozen=True, slots=True)
class Topology:
    name: str
    entrypoint: str
    nodes: tuple[Node, ...]

    @property
    def endpoint_map(self) -> dict[str, str]:
        """``EVAL_WORKLOAD_ENDPOINTS`` for a harness running on the host.

        The injector reaches every node directly, not just the entrypoint: a
        scenario faults `mongodb-profile`, and a fault it cannot deliver is a
        scenario that measured nothing.
        """
        return {n.name: f"http://localhost:{n.port}" for n in self.nodes}

    def endpoint_env(self) -> str:
        return ",".join(f"{k}={v}" for k, v in sorted(self.endpoint_map.items()))


def _assert_acyclic(name: str, edges: dict[str, tuple[str, ...]]) -> None:
    """Depth-first cycle detection, naming the cycle it found.

    An error that says "there is a cycle" sends someone hunting; one that says
    "a -> b -> a" is fixed in a minute.
    """
    white, grey, black = 0, 1, 2
    colour = dict.fromkeys(edges, white)

    def visit(node: str, path: list[str]) -> None:
        colour[node] = grey
        for nxt in edges.get(node, ()):
            if colour.get(nxt) == grey:
                cycle = " -> ".join([*path[path.index(nxt):], nxt])
                raise TopologyError(f"topology {name!r} has a cycle: {cycle}")
            if colour.get(nxt) == white:
                visit(nxt, [*path, nxt])
        colour[node] = black

    for node in edges:
        if colour[node] == white:
            visit(node, [node])


def load(path: Path) -> Topology:
    """Parse and validate one topology file."""
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    name = str(raw["name"])
    base = PORT_BLOCKS.get(name)
    if base is None:
        raise TopologyError(
            f"topology {name!r} has no port block; add one to PORT_BLOCKS so its "
            "published ports cannot collide with another topology's"
        )

    services: dict[str, dict[str, Any]] = raw["services"]
    edges = {
        svc: tuple(str(c) for c in (body.get("calls") or []))
        for svc, body in services.items()
    }

    for svc, calls in edges.items():
        for callee in calls:
            if callee not in services:
                raise TopologyError(
                    f"topology {name!r}: {svc} calls {callee!r}, which is not a "
                    "service in this topology - it would resolve to nothing and "
                    "the fault would reach nothing"
                )
    _assert_acyclic(name, edges)

    entrypoint = str(raw["entrypoint"])
    if entrypoint not in services:
        raise TopologyError(
            f"topology {name!r}: entrypoint {entrypoint!r} is not a service"
        )

    nodes: list[Node] = []
    for offset, svc in enumerate(sorted(services)):
        kind = str(services[svc].get("kind", "service"))
        if kind not in KINDS:
            raise TopologyError(
                f"topology {name!r}: {svc} has kind {kind!r}; known: {sorted(KINDS)}"
            )
        nodes.append(Node(name=svc, kind=kind, calls=edges[svc], port=base + offset))
    return Topology(name=name, entrypoint=entrypoint, nodes=tuple(nodes))


def load_all() -> dict[str, Topology]:
    return {t.name: t for t in (load(p) for p in sorted(TOPOLOGY_DIR.glob("*.yaml")))}


def _compose_service(topo: Topology, node: Node) -> dict[str, Any]:
    return {
        "image": WORKLOAD_IMAGE,
        "build": {"context": "../../workload"},
        "restart": "unless-stopped",
        "profiles": [topo.name],
        "container_name": f"aegis-{topo.name}-{node.name}",
        # The container's hostname is what a peer dials, so it stays the bare
        # service name even though the compose key is prefixed to avoid
        # colliding with the `workload` profile's own gateway/checkout/payment.
        "hostname": node.name,
        "networks": {"default": {"aliases": [node.name]}},
        "environment": {
            "SERVICE_NAME": node.name,
            "SERVICE_KIND": node.kind,
            "DOWNSTREAM": ",".join(node.calls),
        },
        # Every node publishes. The injector runs on the host and must reach
        # any service a scenario targets, not only the entrypoint.
        "ports": [f"{node.port}:8080"],
        # Prometheus discovers scrape targets from these labels, so adding a
        # service to a topology cannot leave a stale scrape config behind.
        "labels": {
            "aegis.service": node.name,
            "aegis.topology": topo.name,
            "aegis.kind": node.kind,
        },
    }


_LOADGEN_SOURCE: Final = (
    "import time,urllib.request\n"
    "while True:\n"
    "    try: urllib.request.urlopen(URL, timeout=5).read()\n"
    "    except Exception: pass\n"
    "    time.sleep(0.4)\n"
)


# The reference three are declared directly in docker-compose.yml under the
# `workload` profile and are what `make up` starts. Generating them again here
# would create a second set of containers claiming the same network aliases:
# with both profiles up, `gateway` would resolve to whichever container compose
# happened to register last, and a fault injected into one would be observed on
# the other. The topology file still contributes its endpoint map and its
# Prometheus targets - only the container definitions are skipped.
COMPOSE_DECLARED: Final = frozenset({"reference"})


def render_compose(topologies: dict[str, Topology]) -> str:
    services: dict[str, Any] = {}
    for topo in topologies.values():
        if topo.name in COMPOSE_DECLARED:
            continue
        for node in topo.nodes:
            services[f"{topo.name}-{node.name}"] = _compose_service(topo, node)

        services[f"{topo.name}-loadgen"] = {
            "image": WORKLOAD_IMAGE,
            "build": {"context": "../../workload"},
            "restart": "unless-stopped",
            "profiles": [topo.name],
            "container_name": f"aegis-{topo.name}-loadgen",
            "environment": {"SERVICE_NAME": f"{topo.name}-loadgen", "DOWNSTREAM": ""},
            # Continuous traffic, because a metric with no samples during the
            # incident window is indistinguishable from a flat one, and Aegis
            # would record an evidence gap for a service that was merely idle.
            "command": [
                "python",
                "-c",
                f"URL='http://{topo.entrypoint}:8080/work'\n{_LOADGEN_SOURCE}",
            ],
            "labels": {"aegis.topology": topo.name, "aegis.role": "loadgen"},
        }

    header = (
        "# GENERATED by eval/topology.py from eval/topologies/*.yaml. Do not edit.\n"
        "#\n"
        "# Regenerate with:  python eval/topology.py --write\n"
        "# CI fails when this file is stale, because a topology that differs from\n"
        "# its own description is an environment nobody can reproduce.\n"
        "#\n"
        "# One profile per topology. They are mutually exclusive by convention,\n"
        "# not by mechanism: running two at once works, costs a lot of memory and\n"
        "# measures nothing extra.\n"
        "name: aegis-2-0\n\n"
    )
    return header + yaml.safe_dump({"services": services}, sort_keys=True, width=100)


def render_targets(topo: Topology) -> str:
    """Prometheus file_sd for one topology.

    Every node is scraped on its published host port rather than through the
    compose network, because the scrape has to work whether Prometheus is a peer
    container or a process on the host. ``host.docker.internal`` resolves to the
    host from inside Docker Desktop and is what the collector already uses.
    """
    groups = [
        {
            "targets": [f"host.docker.internal:{node.port}"],
            "labels": {
                "service": node.name,
                "topology": topo.name,
                "kind": node.kind,
            },
        }
        for node in topo.nodes
    ]
    header = (
        f"# GENERATED by eval/topology.py from eval/topologies/{topo.name}.yaml.\n"
        "# Do not edit. Regenerate with: python eval/topology.py --write\n"
    )
    return header + yaml.safe_dump(groups, sort_keys=False, width=100)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="reference topology generator")
    parser.add_argument("--write", action="store_true", help="write the compose file")
    parser.add_argument(
        "--check",
        action="store_true",
        help="exit 1 if the generated file is missing or stale (for CI)",
    )
    parser.add_argument(
        "--endpoints",
        metavar="TOPOLOGY",
        help="print EVAL_WORKLOAD_ENDPOINTS for one topology",
    )
    args = parser.parse_args(argv)

    try:
        topologies = load_all()
    except (TopologyError, KeyError, OSError) as exc:
        print(f"topology error: {exc}", file=sys.stderr)
        return 2

    if args.endpoints:
        topo = topologies.get(args.endpoints)
        if topo is None:
            print(
                f"unknown topology {args.endpoints!r}; known: {sorted(topologies)}",
                file=sys.stderr,
            )
            return 2
        print(topo.endpoint_env())
        return 0

    # Every generated artefact, keyed by the path it belongs at. Checking and
    # writing walk the same map so a new artefact cannot be written by --write
    # and forgotten by --check, which would let it drift undetected.
    artefacts: dict[Path, str] = {GENERATED: render_compose(topologies)}
    for topo in topologies.values():
        artefacts[TARGETS_DIR / f"{topo.name}.yml"] = render_targets(topo)

    if args.check:
        stale = [
            path
            for path, want in artefacts.items()
            if (path.read_text(encoding="utf-8") if path.exists() else "") != want
        ]
        if stale:
            for path in stale:
                print(f"{path} is out of date", file=sys.stderr)
            print("run: python eval/topology.py --write", file=sys.stderr)
            return 1
        print(f"{len(artefacts)} generated file(s) current")
        return 0

    if args.write:
        for path, content in artefacts.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        print(f"wrote {len(artefacts)} file(s)")

    for name, topo in sorted(topologies.items()):
        ports = [n.port for n in topo.nodes]
        print(
            f"{name:18} {len(topo.nodes):>2} services  "
            f"entrypoint={topo.entrypoint:22} ports {min(ports)}-{max(ports)}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
