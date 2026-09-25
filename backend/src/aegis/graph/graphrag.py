"""GraphRAG - structural retrieval with deterministic ranking.

The graph answers *which* nodes are structurally related. Something still has to
decide which of them are worth spending an investigation's attention on. That
decision is made here, in Python, from three measurable signals (ESD 24):

* **structural distance** - hops from the nearest seed service
* **temporal relevance** - a deployment landing near the incident start
* **causality** - sitting on a shortest path between two seeds

No model participates in the ranking. The same graph and the same incident
produce the same ordering on every run, which is what makes a replayed
investigation comparable to the original one (CLAUDE.md 3.1).

The budget is a hard cap, not a hint. When more nodes qualify than fit, the
lowest-scoring ones are dropped and the truncation is recorded in provenance -
a silently shortened context is indistinguishable from a small blast radius.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from aegis.core.clock import SYSTEM_CLOCK, Clock
from aegis.core.logging import get_logger
from aegis.domain.enums import EvidenceType, SourceType
from aegis.domain.models import BlastRadius, EvidenceItem
from aegis.evidence.store import EvidenceStore
from aegis.graph.traversal import (
    CausalPath,
    DeploymentSummary,
    GraphTraversal,
    QueryRecorder,
    TeamRef,
    provenance_uri,
    rendered_cypher,
)

log = get_logger(__name__)

# Seeds are capped because every seed costs a fixed handful of bounded queries.
# Four covers the services an alert storm realistically implicates; beyond that
# the marginal seed adds latency to an incident rather than insight.
MAX_SEEDS = 4

# Ordered seed pairs explored for causal paths. Pairs grow quadratically with
# seeds, so this is capped independently.
MAX_CAUSAL_PAIRS = 6

# Candidates whose change lineage is fetched, nearest first. One query each, so
# the whole expansion stays under ~40 bounded reads regardless of graph size.
MAX_DEPLOY_LOOKUPS = 8

MAX_DEPLOYMENTS_PER_SERVICE = 5

# Scoring weights. Distance dominates, because structural proximity is the one
# signal that is always observed rather than inferred.
W_DISTANCE = 1.0
W_CAUSAL = 0.35
W_TEMPORAL = 0.40

# A deployment this close to the incident start is worth looking at. One from
# last week is topology, not a lead.
TEMPORAL_WINDOW_S = 3600.0

# Scores are rounded before sorting so that floating-point noise can never
# reorder two otherwise equal nodes between runs.
_SCORE_PRECISION = 6


@dataclass(frozen=True, slots=True)
class ScoredNode:
    service_id: str
    name: str
    label: str
    hops: int
    score: float
    reasons: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "service_id": self.service_id,
            "name": self.name,
            "label": self.label,
            "hops": self.hops,
            "score": self.score,
            "reasons": list(self.reasons),
        }


@dataclass(frozen=True, slots=True)
class CommitSummary:
    sha: str
    repo: str | None = None
    author: str | None = None
    authored_at: datetime | None = None
    deployment_id: str | None = None
    service_id: str | None = None


@dataclass(frozen=True, slots=True)
class GraphContext:
    """Everything the graph contributed to one incident, plus how it was obtained."""

    incident_id: str
    seeds: tuple[str, ...]
    services: tuple[ScoredNode, ...]
    edges: tuple[Mapping[str, Any], ...]
    deployments: tuple[DeploymentSummary, ...]
    commits: tuple[CommitSummary, ...]
    owners: Mapping[str, TeamRef]
    causal_paths: tuple[tuple[str, ...], ...]
    blast_radius: BlastRadius
    provenance: Mapping[str, Any] = field(default_factory=dict)

    @property
    def truncated(self) -> bool:
        return bool(self.provenance.get("truncated"))

    def service_ids(self) -> tuple[str, ...]:
        return tuple(s.service_id for s in self.services)


class GraphRAG:
    """Combines bounded graph traversal with deterministic scoring."""

    __slots__ = ("_traversal", "_clock")

    def __init__(self, traversal: GraphTraversal, *, clock: Clock = SYSTEM_CLOCK) -> None:
        self._traversal = traversal
        self._clock = clock

    async def expand(
        self,
        seed_services: list[str],
        *,
        incident_id: str,
        incident_started_at: datetime | None = None,
        depth: int = 2,
        budget: int = 40,
    ) -> GraphContext:
        """Expand from seed services into a ranked, budgeted structural context.

        Every traversal failure propagates as ``SourceUnavailable``. That is the
        contract: the caller records one evidence gap for the graph and continues
        with lower confidence, rather than this layer inventing a partial context
        that looks complete.
        """
        if budget < 1:
            raise ValueError("budget must be >= 1")
        seeds = _unique(seed_services)[:MAX_SEEDS]
        recorder = QueryRecorder()
        started_at = incident_started_at or self._clock.now()

        if not seeds:
            return GraphContext(
                incident_id=incident_id,
                seeds=(),
                services=(),
                edges=(),
                deployments=(),
                commits=(),
                owners={},
                causal_paths=(),
                blast_radius=BlastRadius(),
                provenance=self._provenance(recorder, depth, budget, 0, None, seeds),
            )

        # --- structure -----------------------------------------------------
        best_hops: dict[str, int] = {}
        meta: dict[str, tuple[str, str]] = {}  # service_id -> (name, label)
        edges: dict[tuple[str, str, str], Mapping[str, Any]] = {}
        radii: list[BlastRadius] = []

        for seed in seeds:
            view = await self._traversal.neighbourhood(seed, depth, recorder=recorder)
            for node in view["nodes"]:
                node_id = str(node["id"])
                hops = int(node["hops"])
                if hops < best_hops.get(node_id, depth + 1):
                    best_hops[node_id] = hops
                meta.setdefault(node_id, (str(node["name"]), str(node["label"])))
            for edge in view["edges"]:
                edges[(str(edge["source"]), str(edge["target"]), str(edge["type"]))] = edge
            radii.append(
                await self._traversal.blast_radius(seed, depth, recorder=recorder)
            )

        # Seeds are distance zero even when the graph does not know them yet:
        # dropping an unknown seed would hide the very service the alert named.
        for seed in seeds:
            best_hops[seed] = 0
            meta.setdefault(seed, (seed.rsplit(":", 1)[-1], "Service"))

        # --- causality -----------------------------------------------------
        paths: list[CausalPath] = []
        for source, target in _ordered_pairs(seeds)[:MAX_CAUSAL_PAIRS]:
            paths.extend(
                await self._traversal.causal_paths(
                    source, target, max_depth=depth + 2, recorder=recorder
                )
            )
        on_causal_path = {node for path in paths for node in path.nodes}

        # --- change lineage and ownership ----------------------------------
        deployments: list[DeploymentSummary] = []
        deploy_by_service: dict[str, list[DeploymentSummary]] = {}
        commits: list[CommitSummary] = []
        owners: dict[str, TeamRef] = {}

        # Lineage is fetched for the nearest candidates, not only the seeds. The
        # deployment that explains an incident is routinely on a neighbour, and a
        # temporal weight that can only ever fire on a seed - which already scores
        # highest on distance - would never change an ordering.
        candidates = sorted(best_hops, key=lambda sid: (best_hops[sid], sid))[
            :MAX_DEPLOY_LOOKUPS
        ]
        for service_id in candidates:
            found = await self._traversal.recent_deployments_for(
                service_id, MAX_DEPLOYMENTS_PER_SERVICE, recorder=recorder
            )
            deploy_by_service[service_id] = found
            deployments.extend(found)
            commits.extend(
                CommitSummary(
                    sha=str(d.commit_sha),
                    repo=d.commit_repo,
                    author=d.commit_author,
                    authored_at=d.commit_authored_at,
                    deployment_id=d.deployment_id,
                    service_id=service_id,
                )
                for d in found
                if d.commit_sha
            )

        for seed in seeds:
            team = await self._traversal.owning_team(seed, recorder=recorder)
            if team is not None:
                owners[seed] = team

        # --- rank and truncate ---------------------------------------------
        scored = score_nodes(
            hops=best_hops,
            meta=meta,
            on_causal_path=on_causal_path,
            deployments=deploy_by_service,
            incident_started_at=started_at,
        )
        kept, truncation = apply_budget(scored, budget)
        kept_ids = {node.service_id for node in kept}

        context = GraphContext(
            incident_id=incident_id,
            seeds=tuple(seeds),
            services=kept,
            edges=tuple(
                edge
                for (src, dst, _), edge in sorted(edges.items())
                if src in kept_ids and dst in kept_ids
            ),
            deployments=tuple(deployments),
            commits=tuple(commits),
            owners=owners,
            causal_paths=tuple(dict.fromkeys(path.nodes for path in paths)),
            blast_radius=_merge_radii(radii, seeds),
            provenance=self._provenance(
                recorder, depth, budget, len(kept), truncation, seeds
            ),
        )
        log.info(
            "graph context expanded",
            incident_id=incident_id,
            seeds=len(seeds),
            candidates=len(scored),
            returned=len(kept),
            truncated=bool(truncation),
            causal_paths=len(context.causal_paths),
            queries=len(recorder.records),
        )
        return context

    async def to_evidence(
        self,
        evidence_store: EvidenceStore,
        incident_id: str,
        context: GraphContext,
    ) -> list[EvidenceItem]:
        """Persist the structural findings as citable evidence.

        Both items are written even when they are empty. An absent TOPOLOGY_PATH
        item would be read as "not looked at"; an empty one states that the graph
        was queried and holds no path, which is a finding in its own right.
        """
        queries = list(context.provenance.get("queries", ()))
        path_uri = _uri_for(queries, "causal_paths") or _uri_for(
            queries, "neighbourhood_nodes"
        )
        radius_uri = _uri_for(queries, "blast_radius") or path_uri

        paths = [list(path) for path in context.causal_paths]
        topology = await evidence_store.record(
            incident_id=incident_id,
            source="neo4j",
            source_type=SourceType.GRAPH,
            evidence_type=EvidenceType.TOPOLOGY_PATH,
            summary=(
                f"{len(paths)} structural path(s) between seed services "
                f"{', '.join(context.seeds)}"
                if paths
                else f"no structural path found between seed services "
                f"{', '.join(context.seeds)}"
            ),
            structured_value={
                "seeds": list(context.seeds),
                "paths": paths,
                "services": [s.as_dict() for s in context.services],
                "edges": [dict(e) for e in context.edges],
                "provenance": dict(context.provenance),
            },
            provenance_uri=path_uri,
        )

        radius = context.blast_radius
        blast = await evidence_store.record(
            incident_id=incident_id,
            source="neo4j",
            source_type=SourceType.GRAPH,
            evidence_type=EvidenceType.BLAST_RADIUS,
            summary=(
                f"{radius.size} service(s) downstream of "
                f"{', '.join(context.seeds)}"
                f"{'; customer facing' if radius.customer_facing else ''}"
            ),
            structured_value={
                "seeds": list(context.seeds),
                "directly_affected": list(radius.directly_affected),
                "downstream": list(radius.downstream),
                "customer_facing": radius.customer_facing,
                "estimated_request_share": radius.estimated_request_share,
                "owners": {sid: team.name for sid, team in context.owners.items()},
            },
            provenance_uri=radius_uri,
        )
        return [topology, blast]

    @staticmethod
    def _provenance(
        recorder: QueryRecorder,
        depth: int,
        budget: int,
        returned: int,
        truncation: Mapping[str, Any] | None,
        seeds: Sequence[str],
    ) -> dict[str, Any]:
        return {
            "seeds": list(seeds),
            "depth": depth,
            "budget": budget,
            "returned": returned,
            "truncated": dict(truncation) if truncation else None,
            "queries": [record.as_dict() for record in recorder.records],
            "scoring": {
                "distance_weight": W_DISTANCE,
                "causal_weight": W_CAUSAL,
                "temporal_weight": W_TEMPORAL,
                "temporal_window_s": TEMPORAL_WINDOW_S,
            },
        }


# --------------------------------------------------------------------------- #
# ranking - pure functions, no I/O, no model                                   #
# --------------------------------------------------------------------------- #


def temporal_boost(
    deployments: Sequence[DeploymentSummary], incident_started_at: datetime
) -> tuple[float, str | None]:
    """Boost for the deployment closest in time to the incident start.

    Linear decay over ``TEMPORAL_WINDOW_S`` in both directions: a deploy that
    landed just before the incident and one that landed just after are both
    worth reading, and deciding which caused which is not this layer's job.
    """
    best = 0.0
    reason: str | None = None
    for deployment in deployments:
        when = deployment.deployed_at
        if when is None:
            continue
        age = abs((incident_started_at - when).total_seconds())
        if age >= TEMPORAL_WINDOW_S:
            continue
        value = W_TEMPORAL * (1.0 - age / TEMPORAL_WINDOW_S)
        if value > best:
            best = value
            reason = f"deploy {deployment.deployment_id} {int(age)}s from incident start"
    return best, reason


def score_nodes(
    *,
    hops: Mapping[str, int],
    meta: Mapping[str, tuple[str, str]],
    on_causal_path: set[str],
    deployments: Mapping[str, Sequence[DeploymentSummary]],
    incident_started_at: datetime,
) -> tuple[ScoredNode, ...]:
    """Score every candidate and order it. Deterministic for identical input."""
    scored: list[ScoredNode] = []
    for service_id, hop_count in hops.items():
        name, label = meta.get(service_id, (service_id, "Service"))
        reasons: list[str] = []

        distance = W_DISTANCE / (1.0 + max(0, hop_count))
        reasons.append("seed" if hop_count == 0 else f"{hop_count} hop(s) from seed")

        causal = 0.0
        if service_id in on_causal_path:
            causal = W_CAUSAL
            reasons.append("on causal path between seeds")

        temporal, temporal_reason = temporal_boost(
            deployments.get(service_id, ()), incident_started_at
        )
        if temporal_reason is not None:
            reasons.append(temporal_reason)

        scored.append(
            ScoredNode(
                service_id=service_id,
                name=name,
                label=label,
                hops=hop_count,
                score=round(distance + causal + temporal, _SCORE_PRECISION),
                reasons=tuple(reasons),
            )
        )
    # service_id breaks ties so two equally scored nodes never swap places
    # between runs of the same investigation.
    scored.sort(key=lambda n: (-n.score, n.service_id))
    return tuple(scored)


def apply_budget(
    scored: Sequence[ScoredNode], budget: int
) -> tuple[tuple[ScoredNode, ...], dict[str, Any] | None]:
    """Keep the top ``budget`` nodes and describe what was dropped."""
    if len(scored) <= budget:
        return tuple(scored), None
    kept = tuple(scored[:budget])
    dropped = scored[budget:]
    return kept, {
        "budget": budget,
        "candidates": len(scored),
        "dropped": len(dropped),
        "cutoff_score": kept[-1].score,
        "highest_dropped_score": dropped[0].score,
    }


# --------------------------------------------------------------------------- #
# helpers                                                                      #
# --------------------------------------------------------------------------- #


def _unique(values: Sequence[str]) -> list[str]:
    return list(dict.fromkeys(v for v in values if v))


def _ordered_pairs(seeds: Sequence[str]) -> list[tuple[str, str]]:
    return [(a, b) for a in seeds for b in seeds if a != b]


def _merge_radii(radii: Sequence[BlastRadius], seeds: Sequence[str]) -> BlastRadius:
    """Union of per-seed blast radii, with the seeds themselves removed.

    A seed appearing in its own blast radius would double-count the failure that
    started the investigation.
    """
    seed_set = set(seeds)
    direct = sorted({s for r in radii for s in r.directly_affected} - seed_set)
    downstream = sorted(
        ({s for r in radii for s in r.downstream} - seed_set) - set(direct)
    )
    return BlastRadius(
        directly_affected=direct,
        downstream=downstream,
        customer_facing=any(r.customer_facing for r in radii),
        estimated_request_share=max((r.estimated_request_share for r in radii), default=0.0),
    )


def _uri_for(queries: Sequence[Mapping[str, Any]], operation: str) -> str:
    for query in queries:
        if query.get("operation") == operation:
            return str(query.get("uri") or "")
    return ""


def uri_for_operation(operation: str, params: Mapping[str, Any], *, depth: int | None) -> str:
    """Provenance URI for a traversal an operator may want to re-run by hand."""
    return provenance_uri(rendered_cypher(operation, depth=depth), params)


__all__ = [
    "MAX_CAUSAL_PAIRS",
    "MAX_DEPLOYMENTS_PER_SERVICE",
    "MAX_DEPLOY_LOOKUPS",
    "MAX_SEEDS",
    "TEMPORAL_WINDOW_S",
    "W_CAUSAL",
    "W_DISTANCE",
    "W_TEMPORAL",
    "CommitSummary",
    "GraphContext",
    "GraphRAG",
    "ScoredNode",
    "apply_budget",
    "score_nodes",
    "temporal_boost",
    "uri_for_operation",
]
