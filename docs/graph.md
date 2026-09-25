# The topology graph

Neo4j answers *which* nodes are structurally related. Python decides which of
them are worth an investigation's attention.

Source: `backend/src/aegis/graph/` — `ontology.py`, `client.py`, `ingest.py`,
`traversal.py`, `graphrag.py`.

---

## 1. What belongs in the graph, and what does not

Neo4j holds **topology**: what calls what, what a service depends on, which
deployment carried which commit, who owns a service, and the structural path an
incident propagated along. Those are traversals, and a traversal expressed as a
recursive SQL join degrades exactly when the estate gets large enough to need it.

**Postgres stays the system of record.** `Incident`, `Alert`, `Remediation` and
`Verification` nodes exist in the ontology as **projections** — an identifier plus
the handful of properties a traversal needs to rank and join. Nothing decides
anything from them.

When the graph and Postgres disagree, Postgres is right and the graph is stale.
That is why every node and edge carries `last_seen`: it turns "this topology is
three weeks old" from an invisible assumption into a queryable property, so a
traversal can be trusted or discounted on evidence rather than on faith.

---

## 2. The ontology is a closed enum, for injection safety

Labels and relationship types **cannot be parameterised in Cypher** — they are
interpolated into the pattern. So they are a closed `StrEnum` in
`graph/ontology.py`, and `label_token` / `rel_token` are the only supported way
to turn a string into one.

```python
def validate_label(value: str | NodeLabel) -> NodeLabel: ...
def label_token(value: str | NodeLabel) -> str: ...
def rel_token(value: str | RelType) -> str: ...
def rel_union_token(values: Iterable[str | RelType]) -> str: ...
```

A caller that passes `"Service) DETACH DELETE (n"` gets a `ValidationError`,
never a clause. There is one authoritative definition of the schema and no second
copy: `node_spec`, `rel_spec`, `natural_key`, `validate_node_properties`,
`validate_edge` and `constraints_cypher` all read from it.

Everything else in every query is bound as a parameter.

---

## 3. The client is a soft dependency

`graph/client.py`. Every method converts a driver failure into
`SourceUnavailable`:

> Losing topology degrades blast-radius analysis; it does not stop an
> investigation.

The caller records an evidence gap and continues with reduced confidence. The
client is wrapped in a `Bulkhead("neo4j", limit=8)` so a slow graph cannot
consume every worker slot. The driver is constructed lazily, so an absent Neo4j
delays its first use rather than blocking boot and failing a readiness probe that
would otherwise have passed.

`Container.ensure_graph_schema()` applies `constraints_cypher()` at startup,
best-effort, and marks the `graph` capability accordingly. `/health` reports it.

---

## 4. Ingestion — every write is a MERGE

`graph/ingest.py`. Identity is the canonical `service_id`, not the runtime
object.

A pod restarting, a container being rescheduled or a scale event re-running
discovery must **update** the existing node, never mint a second one. A
duplicated service silently halves every blast-radius answer that follows — the
kind of bug that produces confidently wrong output rather than an error.

`last_seen` is set on every node and edge each time it is observed.

Batches are bounded twice — rows per statement and rows per call. A discovery
source that suddenly reports a million edges degrades into a truncated ingest plus
a warning, not into an unbounded transaction that pins the driver pool.

Topology is derived from `service_call_edges` (Tempo trace spans) and from the
runtime adapter's service list. `POST /v1/graph/refresh` re-ingests on demand;
`scripts/seed_topology.py` seeds a local graph.

---

## 5. Traversal — depth is clamped, always

`graph/traversal.py`. Two rules shape every function.

**Depth is clamped.** An unbounded variable-length pattern on a real estate is
not a slow query, it is an outage: Neo4j will happily enumerate an exponential
number of paths while holding a connection the rest of the investigation needs.
`MAX_DEPTH` is the ceiling and callers cannot exceed it, whatever they ask for.

The variable-length upper bound is interpolated rather than bound, because Cypher
does not accept a parameter there — but it is an `int` that has passed through
`_clamp_depth`, so the rendered text is one of seven possible strings.

**An empty list is an answer; an exception is not.** Nothing here catches
`SourceUnavailable`. "No dependents found" and "we could not ask" reach the caller
as different things, and the caller decides which becomes an evidence gap.

Every query is recorded by a `QueryRecorder` and rendered into a `provenance_uri`,
so an evidence item derived from the graph cites the exact Cypher and parameters
that produced it.

Typed results rather than dicts: `CausalPath`, `DeploymentSummary`, `TeamRef`,
`BlastRadius`. A dict would let a key rename break topology ingestion silently.

---

## 6. GraphRAG — deterministic ranking

`graph/graphrag.py`. The graph says which nodes are related; this decides which
are worth attention, in Python, from three measurable signals:

| Signal | What it measures |
|---|---|
| **structural distance** | hops from the nearest seed service |
| **temporal relevance** | a deployment landing near the incident start (`temporal_boost`) |
| **causality** | sitting on a shortest path between two seeds |

No model participates in the ranking. The same graph and the same incident produce
the same ordering on every run, which is what makes a replayed investigation
comparable to the original one.

```python
def score_nodes(...) -> list[ScoredNode]: ...
def apply_budget(...) -> tuple[list[ScoredNode], bool]: ...
```

**The budget is a hard cap, not a hint.** When more nodes qualify than fit, the
lowest-scoring ones are dropped and the truncation is recorded in provenance —
`GraphContext.truncated` — because a silently shortened context is
indistinguishable from a small blast radius.

`GraphRAG.to_evidence(...)` converts the context into `EvidenceItem`s with
`SourceType.GRAPH`, which the evidence store classifies as **Tier B**. Structured
metadata, not direct observation.

---

## 7. Exposure

### MCP tools (all `topology:read`, all read-only)

`service_neighbourhood`, `upstream_dependencies`, `blast_radius`, `causal_paths`,
`services_sharing_dependency`, `expand_graph_context`, `owning_team`.

There is no graph write tool. Ingestion is a platform operation, not an agent
capability.

### API

| Endpoint | Returns |
|---|---|
| `GET /v1/graph/neighbourhood` | service graph around one service |
| `GET /v1/graph/blast-radius` | what breaks if this service does |
| `GET /v1/graph/causal-paths` | paths between two services |
| `GET /v1/graph/dependencies` | upstream dependencies |
| `POST /v1/graph/refresh` | re-ingest topology from telemetry |

### Console

`/graph` renders a `ForceGraph` canvas (d3-force) with a `TopologyList` toggle, a
`NodeDetailPanel`, a `HealthLegend` and a depth selector (1–4).

---

## 8. Degradation in practice

With Neo4j down or unconfigured:

- `analyze_topology` records an evidence gap naming `graph` as the unavailable
  source;
- blast radius falls back to what telemetry alone supports;
- `ConfidenceBreakdown.gap_ratio` rises, so the derived confidence drops;
- policy rule 7 may then refuse an autonomous action for insufficient evidence
  quality.

The degradation is visible at every layer rather than silent at any of them. The
`no_graph` ablation reproduces exactly this state by substituting a `_NullGraph`
whose `healthy()` returns `False` and whose queries raise `SourceUnavailable` —
deliberately the same shape as a production outage.

---

## 9. Note on the development knowledge graph

`graphify-out/graph.json` and the `graphify` MCP server index **this repository's
source code** for navigation during development. That is entirely separate from
the operational topology graph described here. `graphify-out/` is gitignored and
regenerable; see [CLAUDE.md](../CLAUDE.md).

---

## See also

- [data-model.md](data-model.md#12-neo4j) — the Postgres/Neo4j/Redis split
- [evidence.md](evidence.md) — why graph evidence is Tier B
- [agents.md](agents.md) — the `analyze_topology` node
- [mcp-tools.md](mcp-tools.md) — the seven topology tools
