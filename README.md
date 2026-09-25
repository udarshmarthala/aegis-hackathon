# Aegis 2.0

An AI SRE control plane. It observes a distributed system, investigates incidents
against real telemetry, forms competing hypotheses, grounds every claim in citable
evidence, and puts a deterministic policy engine between any model output and any
production change.

**Specs (intent):** `PRD.md` · `ESD.md` · `AIArchitecture.md` · `Aegis_UIUX_Spec.md`
**Design:** [docs/architecture.md](docs/architecture.md) · [docs/HLD.md](docs/HLD.md) · [docs/LLD.md](docs/LLD.md)
**Working guide:** [CLAUDE.md](CLAUDE.md)

---

## What makes this different from "an LLM with tools"

A tool-calling agent with a `restart_service` function has exactly one safety
mechanism: the prompt. Aegis has none that depend on the prompt.

| Property | How it is enforced — not by instruction |
|---|---|
| A model's output cannot become an execution | `ValidatedAction`'s constructor demands a module-private token held only by `ActionGate`. Every executor's signature takes `ValidatedAction`; none takes a proposal. |
| A destructive action cannot be executed | Tier-3 types (`delete_data`, `rotate_secret`, `run_migration`, `modify_security_policy`) have **no executor registered at all**. An import-time assertion enforces it. There is nothing to call. |
| Risk tier cannot be argued down | Tier is a static table keyed on action type. No gate reads the proposal's `reason` text. |
| A claim cannot outrun its evidence | Unresolvable, foreign or refuted citations are rejected; the orchestrator abstains rather than asserting. |
| An outage cannot read as health | "Found nothing" and "could not look" are distinct states end to end. `VERIFIED` requires *every* claim to pass, so an unmeasurable claim can never reach it. |
| Untrusted text cannot become instruction | `UntrustedText` annotation is checked at **tool-registration time**; a tool that would hand a model raw log text fails to register at startup. |
| An agent cannot widen its own limits | Nodes hold a read-only `BudgetView` with no mutators. The guard is owned by the orchestrator. |
| Two workers cannot act on one resource | A Postgres partial unique index arbitrates, not an application check. |
| A stale approval cannot be used | `expires_at NOT NULL`, re-checked immediately before the write. |
| Autonomy cannot default on | Missing config, unreadable policy store, unknown action type ⇒ deny. |

> A fluent explanation is not a correct diagnosis, and model confidence is not
> authorization.

---

## Architecture

```
 alert ──▶ api (FastAPI, 16 routers) ──▶ workflow_jobs (Postgres, same txn)
              │                                   │
              │ /v1/*                             ▼
              ▼                         worker ──▶ LangGraph, 12 nodes
     web (Next.js 15, 22 routes)                   │
                                                   ▼
                                    ┌── the tool boundary (36 tools) ──┐
                                    │  35 read · 1 write · scoped      │
                                    └──┬────────┬────────┬─────────┬───┘
                                       ▼        ▼        ▼         ▼
                                  Prometheus  Neo4j   GitHub   runtime
                                  Tempo/Loki  topology Slack    adapter
                                                                   │
  ActionProposal                                                   │
     │ schema → evidence → policy → authz → lease                  │
     ▼                                                             ▼
  ValidatedAction ─▶ execute ─▶ verify ─▶ commit / rollback ─▶ the observed
                                                                 workload

  Postgres = system of record (32 tables) · Neo4j = topology · Redis = never authoritative
```

Full diagram and layer detail: [docs/architecture.md](docs/architecture.md).

---

## Current capabilities

Only capabilities confirmed present in the code. Gaps are in
[Current limitations](#current-limitations).

**Investigation.** A 12-node LangGraph state machine: triage → investigate →
(topology ‖ memory ‖ change analysis) → hypothesize → diagnose → localize code →
plan → execute → learn → finalize. The re-investigation loop requires abstention
*and* remaining budget *and* a loop counter under its limit, so it provably
terminates. [docs/agents.md](docs/agents.md)

**Evidence.** Append-only, content-addressed, with provenance URIs. Trust class
is assigned from the source registry — four tiers, weighted 1.0 / 0.8 / 0.5 /
0.2 — never by a model. Confidence is derived from coverage, corroboration, test
pass rate, source reliability, contradictions and gaps, under a versioned weight
model. [docs/evidence.md](docs/evidence.md)

**Policy.** Twelve deterministic rules over an explicit context struct. `decide`
is a pure function: same input, same decision, replayable during an audit. Every
rule runs even after the first failure, so the record names every blocker.
Default is deny. [docs/policy.md](docs/policy.md)

**Execution.** Five pre-execution gates, eight registered executors across tiers 1
and 2, Postgres-arbitrated leases, expiring approvals bound to one action id, and
a containerised sandbox with no network, no inherited credentials and a refused
secret-shaped env allowlist. [docs/execution.md](docs/execution.md)

**Verification.** CLAIM + EVIDENCE + TEST + RESULT. Thresholds declared before
the measurement. Five verdicts; protected-metric failure produces
`REGRESSION_DETECTED` even when the goal metric improved.
[docs/verification.md](docs/verification.md)

**Knowledge.** Neo4j topology with deterministic GraphRAG ranking and clamped
traversal depth. Hybrid retrieval fusing lexical, vector, graph scope and recency
by RRF. Hierarchical code localisation with capped stages and a recorded stage
trail. Incident memory that refuses to record an abstained, unverified or
unapproved outcome. [docs/graph.md](docs/graph.md) ·
[docs/retrieval.md](docs/retrieval.md)

**Tool boundary.** 36 tools — 35 read, 1 write — behind twelve scopes, with
strict-mode schema validation, permission checks that fail closed, budget
charging, bounded execution, retry only where idempotent, and a `tool_calls` row
for every invocation including denials. [docs/mcp-tools.md](docs/mcp-tools.md)

**Operator surface.** 22 frontend routes: incidents, live systems, service graph,
reliability, recurring failures, investigation transparency, approvals, audit,
policies, AI evaluation. Live incidents stream over SSE.

**Evaluation.** 52 ground-truth scenarios across 19 categories. The system under
test is structurally sealed from ground truth — `GroundTruthLeak` is raised at
build time if a sealed token reaches the payload. Scoring is deterministic —
each metric name is claimed by exactly one evaluator at import time — and the
single LLM judge is forbidden, also at import time, from claiming any metric a
deterministic evaluator owns. [docs/evaluation.md](docs/evaluation.md)

**Verified state.** 860 tests pass (849 unit + 11 integration). ruff and mypy are
clean over 129 source files. The frontend builds 22 routes. The full Docker stack
runs.

---

## Run it locally

Needs Docker Desktop and a `.env` at the repo root (copy `.env.example`).

```bash
make up      # docker compose -f infra/docker/docker-compose.yml --env-file .env ... up -d --build
make logs
make down
```

Or, with the observability and workload profiles:

```bash
docker compose -f infra/docker/docker-compose.yml --env-file .env \
  --project-name aegis-2-0 \
  --profile observability --profile workload up -d --build
```

`--env-file .env` is **not optional** — Docker resolves `.env` relative to the
compose file, which lives in `infra/docker/`.

| Surface | URL |
|---|---|
| Web UI | http://localhost:3000 |
| API docs | http://localhost:8000/docs |
| Health | http://localhost:8000/health |
| Prometheus | http://localhost:9090 |
| Neo4j browser | http://localhost:7474 |
| **Postgres** | **localhost:55433** |

Postgres publishes on **55433**: 5432 is commonly owned by a native PostgreSQL
whose listener beats Docker's proxy, and 55432 collided with another project on
the reference machine.

| Profile | Services |
|---|---|
| *(default)* | postgres, redis, neo4j, api, worker, web |
| `observability` | otel-collector, prometheus, tempo, loki |
| `workload` | gateway, checkout, payment, loadgen |

Only **Postgres** is a hard dependency. Everything else degrades into a recorded
evidence gap that lowers confidence — it never takes the platform down.

Full detail, including sign-in and troubleshooting:
[docs/local-development.md](docs/local-development.md).

---

## Reproduce a sample incident

```bash
# 1. inject a fault into the reference workload (never into Aegis)
curl -X POST http://localhost:8080/admin/fault \
  -H 'Content-Type: application/json' \
  -d '{"mode":"pool_exhaustion","magnitude_ms":900,"probability":1.0}'

# 2. fire an alert
curl -X POST http://localhost:8000/v1/alerts \
  -H 'Content-Type: application/json' \
  -H "X-Aegis-Ingest-Token: $ALERT_INGEST_TOKEN" \
  -d '{"external_id":"demo-1","title":"Checkout p99 latency cascade",
       "severity":"P1","environment":"local","service_hint":"checkout"}'

# 3. watch http://localhost:3000/incidents

# 4. clear the fault
curl -X DELETE http://localhost:8080/admin/fault
```

Re-posting the same `external_id` attaches to the existing incident rather than
opening a second one — ingestion is idempotent by a `UNIQUE (source, external_id)`
constraint, not by an application check.

Seed the topology graph first if you want `/graph` to render:

```bash
backend/.venv/Scripts/python.exe scripts/seed_topology.py
```

The reference workload implements four fault modes: `none`, `latency`, `error`,
`pool_exhaustion`. Faults live in the workload, never in Aegis, so a benchmark
scenario cannot hand the agent privileged knowledge of the injected fault.

---

## Tests

```bash
cd backend
.venv/Scripts/python.exe -m pytest tests -q          # 860
.venv/Scripts/python.exe -m pytest tests/unit -q     # 849, needs no infrastructure
.venv/Scripts/python.exe -m ruff check src tests
.venv/Scripts/python.exe -m mypy src

cd ../frontend && npm run build && npm run lint && npm run typecheck
```

The 11 integration tests **skip** rather than fail when Postgres is absent — a
skipped integration test is honest; a failing one on a laptop with no database
trains people to ignore a red build. They exist to prove the database's own
behaviour: that the partial unique index really does reject a second live lease,
that `ON CONFLICT` really does return the original row.

[docs/testing.md](docs/testing.md)

---

## Evaluation

```bash
backend/.venv/Scripts/python.exe eval/run.py --suite smoke
backend/.venv/Scripts/python.exe eval/run.py --list-ablations
```

Suites: `full` (52), `smoke` (one per category, 19), or any single category.
Reports land in `eval/reports/` as JSON and Markdown.

Exit codes are contractual: `0` ok, `1` could not start, `2` at least one unsafe
scenario, `3` a safety metric regressed against `--compare`. Note what is
missing — "quality got worse" is never an exit failure. Only safety is.

Scoring is deterministic. Eight evaluators own named metrics, claimed at import
time; an LLM judge may only score `root_cause_semantic_match` and
`explanation_quality`, and judged metrics are dropped before pass/fail
classification. Safety outranks correctness: a perfect diagnosis that executed an
unapproved tier-2 action is a failure.

Environment failures are classified separately and excluded from quality
aggregates — a broken Prometheus is never scored as a model mistake.

[docs/evaluation.md](docs/evaluation.md)

---

## Deployment

**Local:** Docker Compose, 14 service definitions, three profiles.

**AWS:** ECS Fargate behind an ALB, RDS PostgreSQL 16 on Graviton, Neo4j as a
single EFS-backed Fargate task reachable only through Cloud Map, optional
ElastiCache. Terraform in `infra/terraform/` with a bootstrap layer (local state)
and staging/production compositions. GitHub OIDC to AWS — no access keys. Seven
CI workflows.

Those five documents were authored separately and are not duplicated here:
[cicd.md](docs/cicd.md) · [terraform.md](docs/terraform.md) ·
[aws-architecture.md](docs/aws-architecture.md) ·
[cost-strategy.md](docs/cost-strategy.md) · [security.md](docs/security.md).

---

## Current limitations

Honest, and each one verified in the code rather than assumed.

**Integrations unconfigured in the reference environment.** `GITHUB_TOKEN`,
`SLACK_BOT_TOKEN` and `SLACK_WEBHOOK_URL` are empty. Change analysis therefore
degrades to a recorded evidence gap and notifications are unavailable. The
degradation is correct and visible; the capability is simply unexercised.

**The worker consumes from Postgres, not SQS.** No code in `backend/src`
references SQS. The Terraform provisions a queue and a DLQ and defines
queue-depth autoscaling against them — both are inert. Already recorded in
`docs/aws-architecture.md` § "Known gaps".

**The stdio MCP server is unavailable.** The optional `mcp` package is not
installed, so `server_status()` reports it unavailable with a reason. That is by
design — an optional transport must never stop the control plane booting. The
internal tool boundary, all 36 tools, permissions, budgets and auditing are
unaffected.

**Patch generation and staging deployment have tables but no driver.**
`remediation_patches`, `deployment_attempts` and `sandbox_runs` exist in
migration 007 and are read by `/v1/deployments*`, but nothing in `backend/src`
inserts a row. `PROMOTE_PATCH` is a declared tier-2 action type with no executor
and no pipeline. The `/debug` and `/deployments` pages will be empty.

**Three of eight ablations are no-ops.** `AblationConfig` declares flags for
`use_evidence_verifier`, `multi_agent` and `use_execution_verification`, but
`apply()` has no branch for them and the names appear nowhere else in the
repository. `single_agent` and `no_verifier` are real and asserted by effect; a
guard test iterates every ablation flag and fails if one changes nothing. There is
no `no_execution_verification` arm, because verification is inseparable from the
rollback decision it drives. Earlier drafts described three arms that ran the
full architecture and are merely labelled otherwise.

**Fault injection covers three modes of seventeen.** `FaultMode` declares 17
values; the injector can apply `latency`, `error`, `pool_exhaustion` (plus the
no-op `none`) — exactly what the reference workload implements. Roughly 27 of 52
scenarios name a mode it cannot apply, and because `strict` defaults to `False`
those scenarios inject nothing and are then scored as if the fault were present.
Use a single injectable category for a number that means something.

**Autonomous action in production is currently impossible.** Policy rule 12 tests
a service allowlist, and `ActionGate._build_context` hard-codes
`service_allowlist=frozenset()` with no configuration source. Any proposal
carrying a `service_id` in a production environment becomes `REQUIRE_HUMAN`. This
fails safe, but the capability is not exercisable today.

**Aegis exposes no Prometheus metrics about itself.** There is no `/metrics`
endpoint — no `prometheus_client` registry anywhere in `backend/src`. The
`aegis-api` scrape job in `infra/prometheus/prometheus.yml` will always fail.
`ESD.md` §44's "emitted metrics" criterion is not met for the control plane;
logs, traces, audit and `agent_runs`/`tool_calls` are wired.

**No integration tests beyond Postgres semantics.** The integration tests
cover lease arbitration, idempotency-key conflict and conditional-transition
races. There are none for the API, the workflow end to end, Neo4j, Redis or the
sandbox.

**No end-to-end browser suite.** The frontend has 43 unit tests (Vitest and
React Testing Library) covering the API client's 401/403 contract, the auth
credential boundary, the source-unavailable-versus-empty distinction and the
formatting helpers. `next build` now enforces lint and types, so a green build
means both passed — but it does not run the tests.

**Also absent:** `infra/kind/` is an empty directory and there is no root
`package.json`.

---

## Layout

```
backend/src/aegis/   129 modules: core, domain, persistence, evidence, graph,
                     retrieval, memory, policy, execution, verification, agents,
                     telemetry, integrations, evaluation, mcp, api, worker
backend/migrations/  001..009, 31 tables (+ schema_migrations)
backend/tests/       849 unit + 11 integration
frontend/            Next.js 15, 22 routes, pure-black design system
workload/            instrumented reference app Aegis observes
eval/                52 ground-truth scenarios, harness, fault injector
infra/docker/        compose stack    infra/otel, infra/prometheus, infra/terraform
docs/                design, operations and deployment documentation
```

Dependencies point inward. `domain/` performs no I/O, so every state machine and
policy rule is unit-testable without infrastructure.

---

## Documentation

| Document | Covers |
|---|---|
| [architecture.md](docs/architecture.md) | processes, layers, request paths, where authority lives |
| [agents.md](docs/agents.md) | the LangGraph workflow, budgets, checkpointing |
| [evidence.md](docs/evidence.md) | trust tiers, `UntrustedText`, derived confidence |
| [policy.md](docs/policy.md) | risk tiers, the twelve rules, kill switches |
| [execution.md](docs/execution.md) | the gate chain, leases, approvals, the sandbox |
| [verification.md](docs/verification.md) | claims, verdicts, why `UNAVAILABLE` blocks `VERIFIED` |
| [graph.md](docs/graph.md) | topology ontology, ingestion, GraphRAG ranking |
| [retrieval.md](docs/retrieval.md) | hybrid fusion, code localisation, incident memory |
| [mcp-tools.md](docs/mcp-tools.md) | the tool boundary and its enforcement |
| [data-model.md](docs/data-model.md) | all 32 tables, the Postgres/Neo4j/Redis split |
| [observability.md](docs/observability.md) | telemetry as evidence, and Aegis's own visibility |
| [testing.md](docs/testing.md) · [evaluation.md](docs/evaluation.md) | tests, and the benchmark |
| [local-development.md](docs/local-development.md) · [runbooks.md](docs/runbooks.md) | running it, and operating it |
| [cicd.md](docs/cicd.md) · [terraform.md](docs/terraform.md) · [aws-architecture.md](docs/aws-architecture.md) · [cost-strategy.md](docs/cost-strategy.md) · [security.md](docs/security.md) | deployment |
