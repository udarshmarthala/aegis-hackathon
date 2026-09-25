# Architecture

What the processes are, how a request moves through them, and which parts are
allowed to decide things.

Higher-level narrative: [HLD.md](HLD.md). Module detail: [LLD.md](LLD.md).

---

## 1. Processes

```
┌──────────────┐        ┌───────────────────────────────────────────┐
│  Alertmanager│        │             Operator (browser)            │
│  / any source│        └──────────────────┬────────────────────────┘
└──────┬───────┘                           │ HTTPS + SSE
       │ POST /v1/alerts                   │
       │ X-Aegis-Ingest-Token              ▼
       │                        ┌──────────────────────┐
       │                        │  web (Next.js 15)    │
       │                        │  22 routes, App Rtr  │
       │                        └──────────┬───────────┘
       │                                   │ /v1/*
       ▼                                   ▼
┌───────────────────────────────────────────────────────────────────┐
│                       api (FastAPI)                               │
│  16 routers · correlation middleware · typed error translation    │
│  Firebase authn · role authz · migrations in lifespan             │
└───────┬──────────────────────────────────────────┬────────────────┘
        │ enqueue workflow_jobs (same transaction) │ read
        ▼                                          │
┌───────────────────────────────────────────────┐  │
│                 worker                        │  │
│  FOR UPDATE SKIP LOCKED · bounded concurrency │  │
│  LangGraph investigation (12 nodes)           │  │
│  gate chain · execution · verification        │  │
└───────┬───────────────────────────────────────┘  │
        │                                          │
        │  ┌───────────────────────────────────────┴──────────────┐
        └─▶│           the tool boundary (mcp/)                   │
           │  36 tools · 35 read · 1 write · scopes · budgets     │
           └───┬──────────────┬───────────────┬──────────────┬────┘
               │              │               │              │
               ▼              ▼               ▼              ▼
        ┌────────────┐ ┌───────────┐ ┌──────────────┐ ┌───────────┐
        │ Prometheus │ │  Neo4j    │ │  GitHub /    │ │  runtime  │
        │ Tempo Loki │ │ topology  │ │  Slack       │ │  adapter  │
        └────────────┘ └───────────┘ └──────────────┘ └─────┬─────┘
                                                            │ writes
┌───────────────────────────────────────────────┐           ▼
│  Postgres  — the system of record (32 tables) │   ┌──────────────┐
│  Redis     — cache + SSE bus, never truth     │   │ the observed │
└───────────────────────────────────────────────┘   │   workload   │
                                                    └──────────────┘
```

Two long-lived processes, one composition root. `container.py` builds the object
graph and **both** the API and the worker use it — the alternative, each process
wiring its own components, is how an API ends up enforcing a different policy
from the worker that actually executes.

---

## 2. Package layout and dependency direction

```
backend/src/aegis/            129 Python modules
  core/         config, logging, errors, resilience, clock, ids
  domain/       pydantic models, enums, state machines      (no I/O)
  persistence/  postgres repositories, migrations, job queue
  evidence/     store, validator, derived confidence
  graph/        neo4j ontology, ingest, traversal, GraphRAG
  retrieval/    hybrid lexical + vector + graph, code localisation
  memory/       incident memory write and read paths
  policy/       risk tiers, rule engine, kill switches
  execution/    gate, registry, executors, leases, approvals, sandbox
  verification/ claims, deterministic engine, store
  agents/       LangGraph workflow, model router, versioned prompts
  telemetry/    prometheus, tempo, loki, otel
  integrations/ github, slack, langsmith, runtime adapters
  evaluation/   harness, evaluators, ablations, reports
  mcp/          the tool boundary: types, registry, invoker, server, tools
  api/          FastAPI routers, security, SSE
  worker/       durable workflow worker
  container.py  the composition root
```

Dependencies point **inward**. `domain/` imports nothing from outer layers and
performs no I/O, so every state machine and policy rule is unit-testable without
infrastructure. Package boundaries map 1:1 to `ESD.md` §41.

---

## 3. The three request paths

### 3.1 Alert ingestion

```
POST /v1/alerts  (X-Aegis-Ingest-Token, constant-time compare)
  → validate
  → INSERT incident_alerts   UNIQUE (source, external_id)
  → create or attach incident
  → INSERT workflow_jobs     ← same transaction
  → 202 Accepted
```

The idempotency boundary is a database constraint, not an application check, so
an Alertmanager retry storm produces one incident. The job is enqueued in the
same transaction as the incident, so a job can never reference an incident that
was rolled back.

### 3.2 Investigation

The worker claims a job and runs the twelve-node LangGraph. See
[agents.md](agents.md) for the graph, the bounded loop and the budget model.

Everything the workflow touches outside itself goes through the tool boundary.
A node with no invoker records an evidence gap rather than calling a client
directly — an unauthorised read is not a read.

### 3.3 Action execution — the safety path

```
ActionProposal (a model can build this)
  │
  ├─ gate 1  schema      cross-field validity
  ├─ gate 2  evidence    every citation resolves, belongs here, is not refuted
  ├─ gate 3  policy      12 deterministic rules, default deny
  ├─ gate 4  authz       a live human approval for tier 2
  └─ gate 5  lease       Postgres partial unique index arbitrates
  │
  ▼
ValidatedAction  (only ActionGate can construct this)
  │
  ├─ still_valid(now)?   lease and approval re-checked at the last moment
  ├─ execute             one bounded adapter call, never retried internally
  ├─ verify              CLAIM/EVIDENCE/TEST/RESULT, five verdicts
  └─ commit or rollback  lease released in a finally
```

See [execution.md](execution.md), [policy.md](policy.md),
[verification.md](verification.md).

---

## 4. Where authority lives

The single organising principle: **the LLM proposes, deterministic code
decides.**

| Question | Decided by | Never by |
|---|---|---|
| risk tier | static table keyed on action type (`policy/tiers.py`) | model output |
| allow / require-human / block | `policy.engine.decide`, a pure function | model output |
| trust class of evidence | source registry (`evidence/store.py`) | model output |
| confidence | derived from coverage, corroboration, test outcomes | model self-report |
| did the remediation work | measured before/after against pre-declared thresholds | model assertion |
| may this tool be called | scope + environment check in `ToolInvoker` | anything in the prompt |
| may this action run | the gate chain, then `ValidatedAction` | anything in the prompt |
| is the budget spent | `BudgetGuard`, owned by the orchestrator | the agent |

Every one of these is a pure function or a table lookup, so every one is
replayable during an audit.

---

## 5. What makes this different from "an LLM with tools"

A tool-calling agent with a `restart_service` function has one safety mechanism:
the prompt. Aegis has none that depend on the prompt.

| Property | How it is enforced |
|---|---|
| A model's output cannot become an execution | `ValidatedAction`'s constructor demands a module-private token only `ActionGate` holds |
| A destructive action cannot be executed | tier-3 types have **no executor registered at all** — nothing to call |
| A claim cannot outrun its evidence | `EvidenceValidator` rejects unresolvable citations; the orchestrator abstains |
| An outage cannot read as health | `UNAVAILABLE` and `SOURCE_UNAVAILABLE` are distinct states end to end; `VERIFIED` requires `all(PASS)` |
| Untrusted text cannot become instruction | `UntrustedText` is checked at tool-registration time, not documented in a prompt |
| An agent cannot widen its own limits | nodes hold a read-only `BudgetView` with no mutators |
| Two workers cannot act on one resource | a Postgres partial unique index, not an application check |
| A stale approval cannot be used | `expires_at NOT NULL`, re-checked immediately before the write |
| Autonomy cannot default on | missing config, unreadable policy store, unknown action type ⇒ deny |

Each of these is a structural property that survives a prompt injection, a model
upgrade and a busy week.

---

## 6. Degradation model

Only **Postgres** is a hard dependency. Everything else is optional and its
absence produces a component that reports itself unavailable:

```python
@dataclass(frozen=True, slots=True)
class Capability:
    name: str
    configured: bool
    reason: str = ""
```

Tracked capabilities: `tempo`, `loki`, `github`, `slack`, `langsmith`,
`embeddings`, `code_retrieval`, `incident_memory`, `graph`, `runtime`, `redis`,
`tools`. Each is surfaced through `GET /health` and the `/settings` page with a
reason, so an operator seeing degraded results knows **which** source was
missing rather than only that confidence was low.

> Optional capabilities degrade, they do not fail. Only Postgres is required,
> because without a system of record there is nothing to be correct about.

Construction never performs I/O it can defer: clients connect lazily, so a slow
or absent dependency delays its first use rather than blocking boot and failing a
readiness probe that would otherwise have passed.

---

## 7. Data stores

| Store | Role | Authoritative? |
|---|---|---|
| **Postgres 16 + pgvector** | incidents, evidence, policy, actions, audit, memory, retrieval corpora, evaluation, job queue | **yes** |
| **Neo4j 5** | topology: what calls what, deployments, ownership, causal paths | no — a projection with `last_seen` |
| **Redis 7** | cache and the SSE fan-out bus | **never** |

See [data-model.md](data-model.md).

---

## 8. Frontend

Next.js 15.1.3, React 19, App Router, 22 routes (21 pages + `/api/healthz`).
Two route groups: `(marketing)` for the public landing page, `(console)` for the
authenticated operator surface.

The `(console)` layout guard is explicitly documented in its own source as *a
courtesy, not a security boundary* — authorisation is decided server-side by role
on every `/v1/*` request.

Design system: pure-black monochrome, tokens defined as CSS variables in
`globals.css` and consumed by name in `tailwind.config.ts` (no hex literals in
the Tailwind config). Roughly 95% monochrome, 3% semantic status, 2% accent.

Live incidents subscribe to SSE; everything else uses TanStack Query with
explicit polling intervals.

---

## 9. Deployment

Local: Docker Compose, 14 service definitions across three profiles. See
[local-development.md](local-development.md).

AWS: ECS Fargate behind an ALB, RDS PostgreSQL on Graviton, Neo4j as a single
EFS-backed task, optional ElastiCache. Terraform in `infra/terraform/` with a
bootstrap layer and staging/production compositions. See
[aws-architecture.md](aws-architecture.md), [terraform.md](terraform.md),
[cicd.md](cicd.md), [cost-strategy.md](cost-strategy.md) and
[security.md](security.md) — all authored separately and not duplicated here.

One divergence worth carrying forward: the Terraform provisions an SQS queue and
a DLQ, and defines queue-depth autoscaling against them, but **the worker
consumes from the Postgres `workflow_jobs` table and no code in `backend/src`
references SQS**. Both the queue and that autoscaling policy are inert. This is
already recorded in `aws-architecture.md` § "Known gaps".

---

## 10. Divergences from the specs

The specs in the repo root (`PRD.md`, `ESD.md`, `AIArchitecture.md`,
`Aegis_UIUX_Spec.md`) outrank this file as statements of intent. Where the code
differs, the code is what runs:

| Spec says | Code does |
|---|---|
| "eight gates" (README, HLD §7) | **five** pre-execution gates in `ActionGate`, and **twelve** rules inside the policy engine. The counts come from different layers. |
| autonomous tier-1 remediation in production | impossible today: `service_allowlist` is hard-coded empty, so production always requires a human ([policy.md](policy.md#5-known-limitation-the-production-allowlist-is-empty)) |
| remediation patches, staging promotion | tables and read APIs exist, no writer ([execution.md](execution.md#8-what-is-not-implemented)) |
| ESD §44 "emitted metrics" | no `/metrics` endpoint on the API ([observability.md](observability.md#4-known-gap-aegis-exposes-no-prometheus-metrics)) |
| SQS-driven work distribution | Postgres `workflow_jobs` |
| eight ablations | five distinct configurations; three are no-ops ([evaluation.md](evaluation.md#6-known-gaps)) |

---

## See also

Every focused document: [agents.md](agents.md) · [evidence.md](evidence.md) ·
[policy.md](policy.md) · [execution.md](execution.md) ·
[verification.md](verification.md) · [graph.md](graph.md) ·
[retrieval.md](retrieval.md) · [mcp-tools.md](mcp-tools.md) ·
[data-model.md](data-model.md) · [observability.md](observability.md) ·
[testing.md](testing.md) · [evaluation.md](evaluation.md) ·
[local-development.md](local-development.md) · [runbooks.md](runbooks.md)
