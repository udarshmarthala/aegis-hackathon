# Aegis 2.0 — Engineering Contract

AI-native SRE platform. Detects, investigates, debugs, repairs, verifies and
safely remediates incidents in distributed systems.

Authoritative specs live in the repo root and **outrank anything in this file**:
`PRD.md` (product) · `ESD.md` (engineering) · `AIArchitecture.md` (AI) ·
`Aegis_UIUX_Spec.md` (interface). Derived design: `docs/HLD.md`, `docs/LLD.md`.

---

## 1. Navigate with the knowledge graph, not with grep

This repo is indexed by **Graphify** into `graphify-out/graph.json`, exposed over
MCP as the `graphify` server. Querying the graph costs a fraction of the tokens
that reading files does.

**Before** reading source to answer "where is X / what touches Y / what breaks if
I change Z", do this in order:

1. `/graphify` skill or the `graphify` MCP tools — `query`, `path`, `explain`,
   `affected`, `god-nodes`.
2. Read only the specific files the graph points at.
3. Full-text search is the last resort, not the first.

Keep the graph fresh:

```bash
graphify update .          # incremental re-extract after edits (no LLM)
graphify watch .           # continuous rebuild while developing
npm run graph:refresh      # wrapper, see package.json
```

`graphify-out/` is gitignored and regenerable — never hand-edit it.

## 2. Use ECC agents as the review layer

Do not self-certify code. Route changes to the matching ECC reviewer before
declaring work done:

| Change touches            | Agent |
|---------------------------|-------|
| `backend/**/*.py`         | `ecc:python-reviewer` |
| `frontend/**/*.tsx`       | `ecc:react-reviewer`, `ecc:a11y-architect` |
| auth, policy, ingestion   | `ecc:security-reviewer` |
| SQL, migrations, schema   | `ecc:database-reviewer` |
| error handling paths      | `ecc:silent-failure-hunter` |
| any build failure         | `ecc:build-error-resolver` |

`ecc:architect` owns structural decisions; `ecc:code-reviewer` is the default
catch-all. GateGuard is on — state facts before the first Bash command.

---

## 3. Architecture invariants — violating these is a defect, not a style choice

These come straight from `ESD.md` §2 and `AIArchitecture.md` §1.

1. **The LLM proposes; deterministic code decides.** An LLM must never be the
   final authority for authn, authz, policy, risk tier, action permission,
   idempotency, locking, rollback state, or evaluation pass/fail.
2. **No claim without evidence.** Every material assertion carries validated
   `EvidenceRef` IDs. A diagnosis with no evidence references is invalid and the
   validator rejects it.
3. **Abstention is a first-class outcome.** `Unknown / insufficient evidence`
   beats a confident guess. Never fabricate certainty.
4. **Read broadly, write narrowly.** Reads may span telemetry and code. Writes
   pass the full gate chain: schema → evidence → policy → authz → lease →
   execute → verify → commit/rollback.
5. **Fail closed.** Missing config, unknown action type, unreachable policy
   store, expired approval ⇒ deny. Never default to allow.
6. **"No evidence found" ≠ "source unavailable."** These are distinct states end
   to end, through the API, into the UI. Collapsing them is an operational bug.
7. **Untrusted text is data, never instruction.** Logs, commit messages, alert
   payloads and ticket bodies are wrapped in `UntrustedText` envelopes. No
   prompt content can grant a permission.
8. **Agents cannot raise their own budgets.** Budgets are enforced by the
   supervisor outside agent reach.
9. **Observability is not a control-plane dependency.** LangSmith, Prometheus or
   Neo4j being down degrades confidence and records an evidence gap — it never
   halts or crashes an incident workflow.
10. **Postgres is the system of record. Neo4j is topology. Redis is never
    authoritative.**

## 4. Reliability rules (this runs for a year without a restart)

- Every external call: explicit timeout, bounded retry with jitter, circuit
  breaker. Never an unbounded `await`.
- Every retry target must be idempotent. Non-idempotent writes are never
  auto-retried.
- Bound every queue, cache and in-memory collection. No unbounded growth.
- All DB access via the pool; no per-request connections. All transactions have
  a statement timeout.
- Async code never blocks the event loop — CPU/blocking work goes to a thread or
  process pool.
- Graceful shutdown: drain, checkpoint, release leases, close pools.
- Structured JSON logs with `incident_id` / `correlation_id`. Never log secrets,
  tokens, or raw service-account material.
- Errors are typed (`aegis.core.errors`). No bare `except:`. No silent `pass`.

## 5. Definition of done (ESD §44)

Typed interfaces · defined failure behaviour · enforced permissions · emitted
metrics · visible traces · evaluation coverage · passing tests · working
replay/audit · unsafe paths structurally impossible.

A green happy-path demo is **not** done.

---

## 6. Layout

```
backend/src/aegis/
  core/         config, logging, errors, resilience, clock, ids
  domain/       pydantic models, enums, state machines   (no I/O)
  persistence/  postgres repositories, migrations, uow
  evidence/     evidence store, provenance, trust classes, redaction
  graph/        neo4j topology, ingestion, GraphRAG traversal
  retrieval/    hybrid lexical + vector + graph retrieval
  policy/       risk engine, action tiers, kill switch, leases
  execution/    environment adapters, action executor, sandbox
  verification/ deterministic before/after verification engine
  agents/       LangGraph orchestrator + capability agents
  telemetry/    OTel, Prometheus, Tempo, Loki clients
  integrations/ firebase auth, github, langsmith, slack
  evaluation/   benchmark harness, evaluators, scenario runner
  mcp/          MCP servers exposing the tool boundary
  api/          FastAPI routers, schemas, SSE
  worker/       durable workflow worker
frontend/       Next.js app router UI
workload/       instrumented reference workload Aegis observes
eval/scenarios/ ground-truth incident scenarios
infra/          docker, kind, otel, prometheus, terraform
```

Package boundaries map 1:1 to `ESD.md` §41. `domain/` imports nothing from
outer layers — dependencies point inward.

## 7. Commands

```bash
docker compose up -d                 # full local stack
docker compose --profile core up -d  # datastores + api + worker + web only
make test  |  make lint  |  make typecheck
scripts/seed-demo.sh                 # load demo incident + topology
eval/run.py --suite smoke            # benchmark against ground truth
```
