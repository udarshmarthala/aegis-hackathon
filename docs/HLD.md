# Aegis 2.0 — High-Level Design

**Status:** as-built, reconciled against the code · **Derived from:** `PRD.md`, `ESD.md`, `AIArchitecture.md`
**Companion:** `docs/LLD.md` (module and class level) · `docs/architecture.md` (process and layer view)

> Where this document and the code disagree, the code wins. Divergences that
> remain are listed in §11.

---

## 1. What this system is

Aegis is an evidence-driven agentic control system for production operations.
It closes the loop **Detect → Triage → Understand → Correlate → Diagnose →
Debug → Repair → Verify → Communicate → Prevent**, with an LLM supplying
reasoning and deterministic code owning every consequential decision.

The single design idea that shapes everything below:

> A fluent explanation is not a correct diagnosis, and model confidence is not
> authorization. Intelligence is probabilistic; control is deterministic.

## 2. Context diagram

```text
  Alertmanager ─┐                                   ┌─→ Slack / status page
  OTel Collector┤                                   │
  Deploy events ┼→ ┌───────────────────────────┐ ───┤
  GitHub        ┘  │        A E G I S          │    └─→ LangSmith (AI traces)
                   │                           │
  Operators ──────→│  API · Worker · Web UI    │───────→ Environment adapters
  (Firebase auth)  └───────────────────────────┘        (Compose / K8s / ECS)
                             │
              ┌──────────────┼──────────────┐
              ▼              ▼              ▼
          PostgreSQL       Neo4j          Redis
        (system of      (topology,     (cache + SSE bus;
          record)        a projection)   never authoritative)
```

Aegis observes a target workload it does not own. The workload reaches Aegis
only as telemetry and events; Aegis reaches the workload only through a narrow,
policy-gated adapter interface.

## 3. Runtime components

| Component | Responsibility | Scaling | Failure behaviour |
|---|---|---|---|
| **aegis-api** (FastAPI) | Ingestion, REST, SSE, auth, approvals. Never runs long work. | Stateless, horizontal | Fails requests; workflows unaffected |
| **aegis-worker** | Executes LangGraph investigations from the durable queue | Horizontal by incident partition | Checkpoint resume; leases expire |
| **aegis-web** (Next.js) | Operator control plane | Stateless | Degrades to cached/empty states |
| **PostgreSQL** | System of record + pgvector retrieval | Primary + replicas | Hard dependency; API fails closed |
| **Neo4j** | Topology, causal paths, incident similarity | Single + replicas | **Soft** — evidence gap, confidence reduced |
| **Redis** | Cache and SSE fan-out only | Single | **Soft** — never authoritative; losing it costs live streaming |
| **OTel / Prometheus / Tempo / Loki** | Evidence sources | Independent | **Soft** — explicit `SOURCE_UNAVAILABLE` |
| **Tool boundary** (`mcp/`) | The only route to the outside: 36 tools, 35 read, 1 write | In-process registry | Bounded retry → evidence gap. The *stdio* MCP transport is optional and currently unavailable. |
| **Sandbox runner** | Isolated patch execution | Ephemeral, per run | Killed on timeout; run marked failed |

**Hard versus soft dependency is the core availability decision.** Only Postgres
is hard. Everything else degrades into a *recorded, visible* evidence gap rather
than an outage. This is what `PRD.md` §13 demands and what makes the platform
survivable over long uptimes.

## 4. The request paths

### 4.1 Alert ingestion (must be under 1s P95)

```text
Alertmanager ──bearer token──→ POST /v1/alerts
                                  │
                       1. validate schema + token
                       2. dedup on (source, external_id) — idempotent
                       3. INSERT incident + alert (one transaction)
                       4. enqueue workflow job
                       5. 202 Accepted
```

The API never investigates inline. Work is durable before acknowledgement, so a
worker crash loses nothing.

### 4.2 Investigation (the product)

```text
worker picks job → LangGraph run (12 nodes, Postgres checkpointer when available)
   triage
   → investigate            (metrics per suspected service, fixed 900s window)
   → parallel fan-out, three branches, merged by a list reducer:
        ├ analyze_topology  (GraphRAG over Neo4j)
        ├ recall_memory     (recurrence signature + hybrid similarity)
        └ analyze_changes   (deploys, commits — degrades without GitHub)
   → hypothesize
   → diagnose ── abstained AND loops < limit AND budget left ──→ investigate
              │ concluded
   → localize_code          (services → repos → commits → files → symbols → tests)
   → plan_remediation       (an ActionProposal, or none)
   → execute_remediation    (the gate chain, then execute + verify)
   → learn                  (memory write, only if eligible)
   → finalize
```

Every node writes state to the Postgres checkpoint and streams an event to the
UI. **Every loop is bounded by an explicit counter and a wall-clock budget.**

### 4.3 Action execution (the safety path)

No shortcut exists around this chain, and the chain lives outside agent reach:

```text
ActionProposal → schema → evidence → policy → authz → lease
  ⇒ ValidatedAction (constructible only by ActionGate)
  → idempotent execute → verification → commit | rollback | escalate

Five pre-execution gates. The kill switch and the risk-tier check are rules 1
and 2 *inside* the policy gate, not separate steps. Twelve deterministic rules
run inside that one gate; all of them run, so the persisted decision names every
blocker rather than only the first.
```

Risk tier and policy decision are computed and persisted **separately**, so a
model cannot argue an action into a lower risk class.

## 5. Data architecture

| Store | Holds | Why this store |
|---|---|---|
| Postgres | incidents, evidence, hypotheses, actions, approvals, policy decisions, leases, audit, benchmark results, checkpoints | transactional truth, constraints, replay |
| Postgres + pgvector | embeddings of runbooks, postmortems, memories | avoids a second datastore for a first-class but small corpus |
| Neo4j | Service / Endpoint / Deployment / Commit / Incident graph | dependency traversal and causal paths are graph-shaped; recursive SQL becomes unmaintainable |
| Redis | cache, SSE pub/sub | speed only, never truth. Leases and the autonomy rate limit are Postgres-only — there is no Redis fast path. |

**The concurrency arbiter is Postgres**, via a partial unique index on active
resource leases. Redis holds a fast-path hint, never the decision.

## 6. Trust and security model

Four evidence trust classes (`AIArchitecture.md` §13) are carried end to end:
`TIER_A` machine observations, `TIER_B` structured metadata, `TIER_C` human
text, `TIER_D` untrusted free text. Tier D is always delivered to the model
inside a delimited `UntrustedText` envelope and can never influence policy.

```text
LLM → MCP tool request → policy → MCP server → scoped credentials → target
```

The agent process holds **no** infrastructure credentials. It learns only
"succeeded or failed, and this is the result". Firebase verifies *identity*;
Aegis policy decides *authority*. The two are never conflated.

The `UntrustedText` requirement is enforced at tool-registration time, not by
convention: a tool whose output model would hand a model raw log text fails to
register at startup.

## 7. Autonomy tiers

| Tier | Example | Path |
|---|---|---|
| 0 | read telemetry | automatic within budget |
| 1 | restart a stateless task | autonomous **only if** every gate and all twelve policy rules pass |
| 2 | rollback deploy, config change | human approval, TTL bounded |
| 3 | destructive DB op, secrets, migrations | **no autonomous code path exists** |

Tier 3 is enforced structurally: those action types are absent from the executor
registry, so there is nothing for policy to accidentally allow.

## 8. AI quality as a product surface

Benchmark scenarios carry ground truth and never leak it to the agent — a
separate process, a separate store, and a fault injector isolated from Aegis.
Deterministic evaluators run first; LLM-as-judge is used only where ground truth
genuinely cannot decide. Release gates block on safety regression, grounding
regression, RCA regression, unsafe-action increase, and budget blowout.

## 9. Key trade-offs taken

| Decision | Alternative rejected | Why |
|---|---|---|
| LangGraph durable execution | Temporal | sufficient durability without a second orchestration platform (ESD §26) |
| Postgres + pgvector | dedicated vector DB | corpus is small; one less hard dependency |
| Neo4j as a *soft* dependency | graph as hard dependency | topology loss should reduce confidence, not cause an outage |
| SVG graph rendering first | WebGL | correctness and accessibility first; escalate only when scale demands |
| Built-in reference workload + K8s adapter | DeathStarBench only | keeps the loop runnable locally; the adapter contract keeps core workload-agnostic |
| Single Python distribution, layered modules | ten separate packages | same enforced boundaries, far simpler build and dependency graph |

## 10. Failure-mode summary

| Failure | Behaviour |
|---|---|
| Postgres down | API 503, workers pause, nothing corrupts |
| Neo4j down | topology evidence gap, confidence reduced, investigation continues |
| Prometheus / Tempo / Loki down | `SOURCE_UNAVAILABLE` evidence, UI distinguishes it from "none found" |
| LLM provider down | fallback provider, same schema and same gates |
| LangSmith down | tracing dropped silently, workflow unaffected |
| Worker killed mid-run | resumes from last checkpoint; leases expire |
| Two agents, one resource | lease unique index rejects the second |
| Approval expired | execution refused, re-approval required |
| Sandbox hang | wall-clock kill, run marked failed, no patch promotion |

## 11. As built: where this design and the code differ

Recorded rather than quietly reconciled. Each was verified against the source.

| Design intent | As built |
|---|---|
| "all 8 gates" | **five** pre-execution gates in `ActionGate`, with **twelve** deterministic rules inside the policy gate. The two counts come from different layers; neither is eight. |
| Debug → patch → sandbox → promote | The sandbox runner, its containment and its MCP tools are complete. **Patch generation and promotion are not.** `remediation_patches`, `deployment_attempts` and `sandbox_runs` have tables and read APIs but no writer, and `promote_patch` has no executor. |
| Autonomous tier-1 action in production | Impossible today. Policy rule 12 tests a service allowlist that `ActionGate._build_context` hard-codes empty, so any production proposal with a `service_id` becomes `REQUIRE_HUMAN`. Fails safe; unexercisable. |
| Bounded fan-out ≤ 4 investigators | Three fixed parallel branches (topology, memory, changes). `AGENT_MAX_PARALLEL_INVESTIGATORS` exists in configuration but the graph shape is static. |
| Redis holds lease hints and rate limits | Neither. Leases are arbitrated by a Postgres partial unique index; the autonomy rate limit counts rows in `remediation_actions`. Redis is cache and the SSE bus. |
| MCP servers per integration | One in-process registry and invoker shared by the API, the worker and any external client. The **stdio transport is optional and currently unavailable** — the `mcp` package is not installed. The internal boundary is fully functional. |
| Work distribution over a queue service | The worker consumes the Postgres `workflow_jobs` table (`FOR UPDATE SKIP LOCKED`). The AWS Terraform provisions SQS and queue-depth autoscaling; both are inert. |
| Emitted metrics (ESD §44) | No `/metrics` endpoint exists on the API. Aegis consumes Prometheus as an evidence source but exposes nothing about itself; the `aegis-api` scrape job always fails. |
| Release gates on quality regression | `eval/run.py` exits non-zero only for **safety**: `2` for an unsafe scenario, `3` for a safety-metric regression. A quality drop is reported, never fatal. |
| Ablations across the architecture | Seven named, all distinct. `single_agent` removes the enrichment nodes from the graph; `no_verifier` skips citation grounding. `no_execution_verification` was removed rather than faked - verification is not separable from the rollback decision it drives. |
| Fault injection across failure classes | `FaultMode` declares 17 modes; the injector applies three (`latency`, `error`, `pool_exhaustion`) plus the no-op `none`. |

Full detail and reproduction for each: [architecture.md §10](architecture.md#10-divergences-from-the-specs),
[execution.md §8](execution.md#8-what-is-not-implemented),
[policy.md §5](policy.md#5-known-limitation-the-production-allowlist-is-empty),
[evaluation.md §6](evaluation.md#6-known-gaps),
[observability.md §4](observability.md#4-known-gap-aegis-exposes-no-prometheus-metrics).

## 12. Measured baseline

| Metric | Value |
|---|---|
| Backend modules | 129 |
| Database tables | 32 (31 from migrations + `schema_migrations`) |
| Migrations | 9, forward-only, checksum-guarded |
| Tests | 860 — 849 unit (no infrastructure), 11 integration (skip without Postgres) |
| Tool boundary | 36 tools: 35 read, 1 write, 12 scopes |
| Workflow nodes | 12 |
| Action types | 13 — 4 tier-1, 5 tier-2, 4 tier-3 |
| Registered executors | 8 (4 tier-1 + 4 tier-2; all tier-3 and `promote_patch` deliberately absent) |
| Policy rules | 12 |
| Verification verdicts / claim outcomes | 5 / 4 |
| Frontend routes | 22 |
| Benchmark scenarios | 52 across 19 categories |
