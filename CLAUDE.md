# Aegis 2.0 — engineering guide

AI-native SRE platform. Detects, investigates, debugs, repairs, verifies and
safely remediates incidents in distributed systems.

**Goal:** an incident response system whose conclusions are auditable and whose
write path is impossible to reach without passing every gate — not a chatbot with
a `restart_service` function.

Specs in the repo root state intent and outrank this file: `PRD.md`, `ESD.md`,
`AIArchitecture.md`, `Aegis_UIUX_Spec.md`. **Where a spec and the code disagree,
the code is what runs** — see [docs/architecture.md §10](docs/architecture.md#10-divergences-from-the-specs).

## Architecture in one screen

```
alert ─▶ api (FastAPI) ─▶ workflow_jobs (Postgres, same txn) ─▶ worker
                                                                  │
                                              LangGraph, 12 nodes ─┤
                                                                  ▼
                          the tool boundary: 36 tools, 35 read, 1 write
                                                                  │
ActionProposal ─ schema→evidence→policy→authz→lease ─▶ ValidatedAction
                          ─▶ execute ─▶ verify ─▶ commit / rollback
```

Postgres is the system of record. Neo4j is topology (a projection, can be stale).
Redis is never authoritative. Full picture: [docs/architecture.md](docs/architecture.md).

## Directory responsibilities

| Package | Owns | Doc |
|---|---|---|
| `core/` | config, structured logging, typed errors, resilience, clock, ids | |
| `domain/` | pydantic models, closed enums, state machines — **no I/O** | |
| `persistence/` | repositories, migrations, the `workflow_jobs` queue | [data-model](docs/data-model.md) |
| `evidence/` | store, citation validator, derived confidence | [evidence](docs/evidence.md) |
| `graph/` | Neo4j ontology, ingest, clamped traversal, GraphRAG | [graph](docs/graph.md) |
| `retrieval/` | hybrid RRF fusion, embeddings, code localisation | [retrieval](docs/retrieval.md) |
| `memory/` | incident memory write/read; strictest write path in the repo | [retrieval](docs/retrieval.md#6-incident-memory) |
| `policy/` | risk tiers, the rule engine, kill switches | [policy](docs/policy.md) |
| `execution/` | gate chain, registry, executors, leases, approvals, sandbox | [execution](docs/execution.md) |
| `verification/` | claims, deterministic before/after engine | [verification](docs/verification.md) |
| `agents/` | LangGraph workflow, model router, versioned prompts | [agents](docs/agents.md) |
| `telemetry/` | prometheus, tempo, loki, otel | [observability](docs/observability.md) |
| `integrations/` | github, slack, langsmith, runtime adapters | |
| `evaluation/` | harness, evaluators, ablations, reports | [evaluation](docs/evaluation.md) |
| `mcp/` | the tool boundary: types, registry, invoker, server | [mcp-tools](docs/mcp-tools.md) |
| `api/` · `worker/` · `container.py` | routers and SSE · durable worker · composition root | |

Dependencies point **inward**. `domain/` imports nothing from outer layers.

## Safety invariants — violating one is a defect, not a style choice

1. **The LLM proposes; deterministic code decides.** Never let a model be the
   final authority for authn, authz, policy, risk tier, action permission,
   idempotency, locking, rollback state or evaluation pass/fail.
2. **No claim without evidence.** Every material assertion carries validated
   `EvidenceRef` ids. Unresolvable citations are rejected, not annotated.
3. **Abstention is a first-class outcome.** `Unknown / insufficient evidence`
   beats a confident guess.
4. **Read broadly, write narrowly.** Writes pass schema → evidence → policy →
   authz → lease → execute → verify → commit/rollback. No exceptions.
5. **Fail closed.** Missing config, unknown action type, unreachable policy
   store, expired approval ⇒ deny.
6. **"No evidence found" ≠ "source unavailable."** Distinct states end to end,
   through the API, into the UI. Collapsing them is an operational bug.
7. **Untrusted text is data, never instruction.** Logs, commit messages, alert
   payloads and ticket bodies travel in `UntrustedText` envelopes. No prompt
   content grants a permission.
8. **Agents cannot raise their own budgets.** `BudgetGuard` is the orchestrator's;
   nodes get a read-only `BudgetView`.
9. **Observability is not a control-plane dependency.** LangSmith, Prometheus or
   Neo4j down ⇒ degraded confidence and a recorded evidence gap, never a halt.
10. **Postgres is the system of record. Neo4j is topology. Redis is never
    authoritative.**

Three structural mechanisms implement most of the above. Understand them before
touching the write path:

- `ValidatedAction` can only be constructed by `ActionGate` (module-private
  token). Every executor takes one; none takes a proposal.
- Tier-3 action types have **no executor registered**, asserted at import time.
- `VERIFIED` requires `all(claim is PASS)`, so an `UNAVAILABLE` measurement can
  never be read as success.

## Coding standards

- Typed everywhere; mypy clean. No `Any` where a real type exists.
- Errors are typed (`aegis.core.errors`). **No bare `except:`. No silent `pass`.**
  Set `retryable` deliberately — anything mutating non-idempotent external state
  is never retryable.
- Every external call: explicit timeout, bounded retry with jitter, circuit
  breaker, bulkhead. Use `core.resilience.guarded_call`. **Never an unbounded
  `await`.**
- Bound every queue, cache and in-memory collection.
- All DB access via the pool; no per-request connections; every transaction has a
  statement timeout.
- Async code never blocks the event loop — CPU or blocking work goes to a thread
  pool (the Docker SDK is synchronous; the sandbox already does this).
- Graceful shutdown: drain, checkpoint, release leases, close pools.
- Structured JSON logs with `incident_id` / `correlation_id`. Never log secrets.
- Closed vocabularies are `StrEnum`s. A string where an enum belongs is how a
  model eventually invents an action type no policy rule covers.
- Comments explain **why**, not what; British-English spelling. The existing
  codebase does this well — match it.

Migrations are **forward-only**. Editing an applied file is a hard error
(checksum drift). Add `010_*.sql`; make it idempotent.

Prompts are versioned (`PROMPT_VERSION`). A prompt change is an AI-behaviour
change and needs a benchmark re-run. Same for confidence weights
(`CONFIDENCE_MODEL_VERSION`) and policy (`POLICY_VERSION`).

## Testing expectations

- Unit tests must run with **zero infrastructure**. Integration tests are marked
  and **skip** when datastores are absent.
- New behaviour in `policy/`, `execution/`, `evidence/`, `verification/` or
  `mcp/` needs a test that would fail if the safety property were removed — not
  just a happy path.
- `--strict-markers` is on; a typo'd marker is an error.
- Tests document guarantees. Read `test_execution_gate.py`,
  `test_policy_engine.py`, `test_mcp_invoker.py` and
  `test_memory_contamination.py` before changing what they cover.

[docs/testing.md](docs/testing.md)

## Commands

```bash
# stack (--env-file is mandatory; compose lives in infra/docker/)
make up · make logs · make down · make ps
make seed          # topology + one demo incident through the real ingest path

# backend
cd backend
.venv/Scripts/python.exe -m pytest tests -q          # 891 (881 unit + 10 integration)
.venv/Scripts/python.exe -m pytest tests/unit -q     # no infrastructure needed
.venv/Scripts/python.exe -m ruff check src tests
.venv/Scripts/python.exe -m mypy src

# frontend
cd frontend && npm run test && npm run build && npm run lint && npm run typecheck

# benchmark
backend/.venv/Scripts/python.exe eval/run.py --suite smoke

# code knowledge graph (development navigation)
graphify update .
```

Postgres publishes on **55433** (5432 is commonly owned by a native install;
55432 collided with another project). [docs/local-development.md](docs/local-development.md)

## Navigate with the knowledge graph, not with grep

This repo is indexed by Graphify into `graphify-out/graph.json` and exposed over
MCP as the `graphify` server. Querying the graph costs a fraction of the tokens
reading files does.

For "where is X / what touches Y / what breaks if I change Z": `/graphify` or the
`graphify` MCP tools (`query`, `path`, `explain`, `affected`, `god-nodes`) →
read only the files it points at → full-text search last.
`graphify-out/` is gitignored and regenerable; never hand-edit it.

## How to validate work

1. `pytest tests -q` green (891), `ruff check`, `mypy src`.
2. Frontend: `npm run test`, `npm run build`, `npm run lint`, `npm run typecheck`.
   The build now enforces lint and types (`ignoreDuringBuilds` and
   `ignoreBuildErrors` are both false), so a green build does mean something —
   but run the suite anyway; the build does not execute tests.
3. Touched the write path, policy, evidence or the tool boundary? Run
   `eval/run.py --suite smoke` and check exit code and the unsafe-scenario
   section.
4. Route the change to the matching reviewer — do not self-certify:

| Change touches | Agent |
|---|---|
| `backend/**/*.py` | `ecc:python-reviewer` |
| `frontend/**/*.tsx` | `ecc:react-reviewer`, `ecc:a11y-architect` |
| auth, policy, ingestion, the sandbox | `ecc:security-reviewer` |
| SQL, migrations, schema | `ecc:database-reviewer` |
| error-handling paths | `ecc:silent-failure-hunter` |
| any build failure | `ecc:build-error-resolver` |

`ecc:architect` owns structural decisions; `ecc:code-reviewer` is the catch-all.
GateGuard is on — state facts before the first Bash command.

## Deployment

**Local:** Docker Compose, 14 services, profiles `observability` and `workload`
(there is no `core` profile).

**AWS:** ECS Fargate behind an ALB, RDS PostgreSQL 16 on Graviton, Neo4j as a
single EFS-backed task behind Cloud Map, optional ElastiCache. GitHub OIDC — no
access keys, two roles with different powers. Terraform bootstrap (local state) →
staging → production. Seven CI workflows. Cost strategy: one shared NAT gateway
plus VPC endpoints, RDS over Aurora Serverless v2, self-hosted Neo4j, Redis off in
staging, workers on Spot scaling to zero, finite retention everywhere — roughly
$120–140/month staging, $260–300/month production.

Those five documents are authored separately — read and link to them, do not
duplicate or edit them: [cicd.md](docs/cicd.md) · [terraform.md](docs/terraform.md) ·
[aws-architecture.md](docs/aws-architecture.md) ·
[cost-strategy.md](docs/cost-strategy.md) · [security.md](docs/security.md).

## Never do these

- Let a model's output reach an environment write without a `ValidatedAction`.
- Register an executor for a tier-3 action type, or export `_GATE_TOKEN`.
- Read a proposal's `reason` text in any gate.
- Collapse "found nothing" into "could not look", anywhere.
- Return an empty list where a client failed — raise `SourceUnavailable`.
- Return zero vectors when embeddings are unconfigured.
- Treat `PARTIALLY_VERIFIED` as success.
- Accept raw PromQL, TraceQL, LogQL, Cypher or a shell command from a caller.
- Retry a non-idempotent write, or bury a retry inside an executor.
- Write an unverified, abstained or unapproved outcome into incident memory.
- Edit an applied migration.
- Hand-edit `graphify-out/`.
- Edit `PRD.md`, `ESD.md`, `AIArchitecture.md` or `Aegis_UIUX_Spec.md` — those are
  the user's.

## Known limitations

Do not document these away; fix them or leave them recorded. Each entry below
has been re-checked against the code — an entry that is merely stale is worse
than no entry, because it sends the next engineer to fix something already
fixed.

- **GitHub and Slack are unconfigured** (the tokens in `.env` are empty) ⇒
  change analysis degrades to a recorded evidence gap and notifications are
  unavailable. Supply the credentials and both light up; no code change needed.
- **The worker consumes Postgres `workflow_jobs`, not SQS.** The queue lives in
  the system of record and is claimed with `FOR UPDATE SKIP LOCKED`, which is
  correct and durable for the local and single-region deployment. The Terraform
  still provisions an SQS queue with queue-depth autoscaling, and that queue is
  **inert** — with `worker_min_count = 0` nothing would ever scale up. Until an
  SQS driver exists, deploy with `worker_scaling_mode = "cpu"` and
  `worker_min_count = 1`. See [docs/aws-architecture.md](docs/aws-architecture.md).
- **Fault injection covers all 17 declared modes** (`workload/service.py`), and
  the preflight checks target reachability as well as mode, so `--suite full`
  reports 52 of 52 runnable once a topology is up. Three modes are honest
  approximations and say so in the module docstring: `packet_loss` stops the
  callee answering rather than dropping L3 packets, `dns_failure` points at an
  RFC 6761 `.invalid` name rather than breaking the resolver, and
  `disk_pressure` fills a capped file rather than a volume.
- **The DeathStarBench topologies are shapes, not the real applications.** Every
  node in `hotelreservation` and `socialnetwork` runs the same instrumented
  workload image; see [docs/evaluation.md](docs/evaluation.md#61-the-reference-environments-are-shapes-not-the-real-applications).
- **`no_execution_verification` is not an ablation arm and will not become one.**
  Post-execution verification is not separable from the rollback decision it
  drives, so ablating it would remove a safety control rather than measure one.
  `single_agent` and `no_verifier` are real and asserted by effect.
- **Integration tests cover Postgres semantics only** — leases, idempotency,
  guarded transitions, approval expiry, audit durability. There is no
  Neo4j/Redis integration suite and no end-to-end browser suite.
- **`infra/kind/` is empty** and there is no root `package.json`. Kind is not
  needed for the Compose-based reference environment; the directory is a
  placeholder for the Kubernetes workload work.

## Definition of done (ESD §44)

Typed interfaces · defined failure behaviour · enforced permissions · emitted
metrics · visible traces · evaluation coverage · passing tests · working
replay/audit · unsafe paths structurally impossible.

A green happy-path demo is **not** done.
