# Data model

Three stores, one system of record.

- **Postgres** is authoritative. Incident state, evidence, policy decisions,
  approvals, actions, audit and evaluation all live here.
- **Neo4j** holds topology — what calls what, what depends on what. It is a
  projection, and it can be stale.
- **Redis** is never authoritative. Cache and the SSE fan-out bus, nothing else.

When the graph and Postgres disagree, Postgres is right and the graph is stale.
Every graph node carries `last_seen` so that is a queryable property rather than
an invisible assumption.

Source: `backend/migrations/001..009`, `backend/src/aegis/persistence/`.

---

## 1. Shape

**31 tables from the nine migrations, plus `schema_migrations` created by the
Python runner — 32 in a fully migrated database.**

| Migration | Tables created | Theme |
|---|---|---|
| `001_core.sql` | 4 | identity and the incident spine |
| `002_evidence.sql` | 5 | evidence and the reasoning record |
| `003_actions.sql` | 7 | the safety-critical tables |
| `004_agents.sql` | 3 | agent observability and the job queue |
| `005_memory_eval.sql` | 6 | memory, hybrid retrieval, benchmark |
| `006_retrieval.sql` | 2 | code corpus and service mapping (+ many ALTERs) |
| `007_execution.sql` | 4 | sandbox, patches, deployments, claims |
| `008_tools.sql` | 0 | widens `tool_calls` |
| `009_evaluation.sql` | 0 | widens the evaluation tables |

Extensions: `pgcrypto` (001), `vector` (005, re-declared in 006).

**No triggers, no stored functions, no views.** `updated_at` columns exist on
many tables but are application-maintained, not trigger-maintained.

IDs are ULIDs stored as `TEXT PRIMARY KEY`. Four tables use
`BIGINT GENERATED ALWAYS AS IDENTITY` (`incident_state_transitions`,
`hypothesis_confidence_history`, `evidence_gaps`, `audit_log`); two use composite
natural keys (`kill_switches`, `service_repositories`).

---

## 2. Core — incidents

### `incidents`
Root aggregate. `severity` is CHECK-constrained to P1–P4; `confidence` to
`[0,1] OR NULL`; `state` is **not** CHECK-constrained (enforced by
`domain/state_machines.py`). `affected_services TEXT[]`, `metadata JSONB`,
`correlation_id`.

Indexes: `(state, severity, created_at DESC)`, `(environment, created_at DESC)`,
and a partial `(created_at DESC) WHERE resolved_at IS NULL` for the open-incident
list.

### `incident_alerts`
The **ingestion idempotency boundary**:

```sql
UNIQUE (source, external_id)
```

Re-posting the same `external_id` attaches to the existing incident rather than
opening a second one. That is what makes Alertmanager retries safe.
`labels`, `annotations` and `raw_payload` are JSONB.

### `incident_state_transitions`
Append-only history. `from_state` is nullable for the first transition.

### `users`
Firebase-backed identity: `firebase_uid TEXT UNIQUE`, `email`,
`roles TEXT[] DEFAULT ARRAY['viewer']`. Functional index on `lower(email)`.
Note there is **no** unique constraint on `email`.

---

## 3. Evidence and reasoning

### `evidence_items`
One observation, with provenance and trust. Dedup is a partial unique index:

```sql
CREATE UNIQUE INDEX evidence_dedup_idx
    ON evidence_items (incident_id, content_hash) WHERE content_hash <> '';
```

`status` vocabulary: `VALIDATED | UNVALIDATED | REFUTED | SOURCE_UNAVAILABLE`
(documented in the migration comment, enforced in application code).
`trust_class` defaults to `TIER_D`. `content_untrusted BOOLEAN` carries the
`UntrustedText` flag into the database. `structured_value JSONB`.

### `evidence_gaps`
A first-class record of sources that could **not** be consulted: `source`,
`source_type`, `reason`, `affects TEXT[]`. Separate table, separate query,
separate UI panel — see [evidence.md](evidence.md#3-no-evidence-found--source-unavailable).

### `hypotheses`
`supporting`, `contradicting`, `missing`, `affected_services` — four `TEXT[]`
columns of evidence ids. `predictions JSONB` defaults to `'[]'`.
`UNIQUE (incident_id, label)`. `confidence` CHECK `[0,1]`.

### `hypothesis_confidence_history`
Belief movement over time; drives the console's confidence chart.

### `diagnoses`
`abstained BOOLEAN NOT NULL` — abstention is a stored outcome, not a null.
Six `TEXT[]` arrays including `supporting_evidence`, `causal_path`,
`rejected_alternatives` and `missing_evidence`.
`confidence_model_version` records which confidence model produced the number.

---

## 4. The safety-critical tables

### `remediation_actions`

```sql
idempotency_key TEXT NOT NULL UNIQUE
```

Globally unique, enforced by the database rather than an application check. A
worker that crashes between acting and recording retries the same proposal;
`ActionRepository.propose` returns the existing row on conflict, so the retry
observes the original action instead of mutating production a second time.

JSONB columns: `expected_effect`, `blast_radius`, `verification_plan`,
`arguments` (added by 007), `result`. `rollback_plan` is **nullable** — its
absence is meaningful and policy rule 10 fires on it.

Partial index backing the autonomy rate limit without a table scan:

```sql
CREATE INDEX actions_executed_recent_idx
    ON remediation_actions (environment, executed_at DESC)
    WHERE executed_at IS NOT NULL;
```

### `policy_decisions`
Stored in their own table, keyed by action, so risk tier and effect stay
independently auditable and an action can accumulate several decisions over its
life without any being overwritten.

```sql
effect    TEXT NOT NULL CHECK (effect IN ('ALLOW','REQUIRE_HUMAN','BLOCK')),
risk_tier SMALLINT NOT NULL CHECK (risk_tier BETWEEN 0 AND 3),
```

`gates JSONB` holds every gate result. `context_snapshot JSONB` holds the
complete replayable policy input.

### `approvals`

```sql
expires_at TIMESTAMPTZ NOT NULL,
decision   TEXT CHECK (decision IN ('approved','rejected','more_evidence')),
CHECK (decision IS NULL OR decided_at IS NOT NULL)

CREATE UNIQUE INDEX approvals_one_open_idx
    ON approvals (action_id) WHERE decision IS NULL;
```

`expires_at` is `NOT NULL` — a stale approval must never be executable. At most
one open approval per action.

### `resource_leases` — the concurrency arbiter

```sql
CREATE UNIQUE INDEX resource_leases_active_uniq
    ON resource_leases (resource_type, resource_id)
    WHERE released_at IS NULL;
```

This single index is the mutual-exclusion mechanism for every production write.
Two workers racing produce one winner and one `LeaseConflict` at the database
level. A check-then-insert scheme would have a window between the two statements;
this has none.

A second partial index `(expires_at) WHERE released_at IS NULL` backs the expiry
sweeper. `incident_id` uses `ON DELETE SET NULL`, not CASCADE — a lease outlives
its incident row.

### `verification_runs` / `verification_claims`
Split so a verdict can be disputed line by line.
`verification_claims.outcome` is CHECK-constrained to
`('PASS','FAIL','INCONCLUSIVE','UNAVAILABLE')` — the four-state distinction is
enforced by the schema, not only by code.

### `audit_log`

```sql
incident_id TEXT,  -- deliberately NO foreign key
actor_type  TEXT NOT NULL CHECK (actor_type IN ('human','agent','system')),
```

No FK: audit rows survive incident deletion. `actor_type` is what makes "did a
person authorise this?" answerable after an autonomous system touches
production. Indexed on `(incident_id, created_at DESC)`, `(created_at DESC)` and
`(correlation_id)` — the last one is how an investigator pivots between audit,
logs, OTel and LangSmith from a single value.

### `kill_switches`

```sql
scope TEXT NOT NULL CHECK (scope IN ('global','environment','action_type','service')),
PRIMARY KEY (scope, target)
```

Absence of a row means "not engaged". An **unreadable** table is treated as every
switch engaged — see [policy.md](policy.md#3-kill-switches-fail-closed).

---

## 5. Agent observability and the job queue

### `agent_runs`
One LLM invocation. Self-referencing `parent_run_id` for sub-agents.
`cost_usd NUMERIC(12,6)`, `input_tokens`, `output_tokens`, `prompt_version`,
`langsmith_run_id`.

### `tool_calls`
The tool-boundary audit surface.

```sql
access TEXT NOT NULL DEFAULT 'read' CHECK (access IN ('read','write')),
degraded BOOLEAN NOT NULL DEFAULT FALSE,
degraded_reason TEXT NOT NULL DEFAULT '',
```

`degraded` is the "could not look" vs "found nothing" distinction at the
persistence layer — both have `ok = TRUE`. Partial indexes on
`WHERE access = 'write'` and `WHERE degraded`.

### `workflow_jobs`
The durable queue. `FOR UPDATE SKIP LOCKED` pickup, no external broker, so
"create the incident and schedule its investigation" fits in one transaction — a
job can never reference an incident that was rolled back.

```sql
status TEXT NOT NULL DEFAULT 'queued'
    CHECK (status IN ('queued','running','done','failed','cancelled'))

CREATE INDEX workflow_jobs_pickup_idx ON workflow_jobs (status, run_after)
    WHERE status = 'queued';
CREATE UNIQUE INDEX workflow_jobs_one_active_idx
    ON workflow_jobs (incident_id, kind) WHERE status IN ('queued','running');
```

---

## 6. Memory and retrieval

### `incident_memories`
The one store that feeds itself. `fingerprint` is the recurrence signature;
`occurrences` counts recurrences; `approved BOOLEAN` gates organisational
knowledge.

```sql
CREATE UNIQUE INDEX incident_memories_fingerprint_key
    ON incident_memories (fingerprint) WHERE fingerprint <> '';
```

Note a redundant non-unique `memories_fingerprint_idx` also exists — an artifact
of 005 followed by 006.

### `retrieval_documents`
Unified hybrid corpus (runbooks, postmortems, memories, docs).
`content_hash` is globally unique where non-empty, so re-ingesting unchanged
content is a no-op.

### `code_documents`
Chunked repository source with **real line numbers**, so a citation resolves to
the lines an operator will actually open.

```sql
kind TEXT NOT NULL DEFAULT 'file' CHECK (kind IN ('file','symbol','test','config')),
start_line INTEGER NOT NULL DEFAULT 1 CHECK (start_line >= 1),
end_line   INTEGER NOT NULL DEFAULT 1 CHECK (end_line >= start_line),

CREATE UNIQUE INDEX code_documents_dedup_key
    ON code_documents (repo, path, start_line, content_hash);
```

The dedup key deliberately excludes `ref`, so a new commit touching other files
does not re-insert unchanged chunks.

### `service_repositories`
Maps a service to the repos and path prefixes that implement it — the first
narrowing stage of code localisation. Composite natural PK
`(service_id, repo, path_prefix)`; `rank` breaks ties in a monorepo.

### `runbooks`
The simplest table in the schema: no indexes beyond the PK, no FKs. Runbooks
reach retrieval only by being ingested into `retrieval_documents`.

### Vector and full-text columns

Three `vector(1536)` columns, all identically indexed:

| Table | Column | Index |
|---|---|---|
| `retrieval_documents` | `embedding` | `ivfflat (embedding vector_cosine_ops) WITH (lists = 100)` |
| `code_documents` | `embedding` | same |
| `incident_memories` | `embedding` | same |

Three generated `tsvector` columns, all `'english'`, all GIN-indexed, none
written by application code:

| Table | Generated from |
|---|---|
| `retrieval_documents.tsv` | `title \|\| body` |
| `code_documents.tsv` | `symbol \|\| path \|\| content` |
| `incident_memories.tsv` | `title \|\| symptoms \|\| root_cause` |

Plus two GIN indexes on array columns: `retrieval_documents(services)` and
`incident_memories(affected_services)`.

---

## 7. Evaluation

### `benchmark_cases`
Scenario definitions **plus ground truth**, never exposed on agent-facing paths.
`ground_truth JSONB NOT NULL` with no default — it must be supplied.
`scenario_id TEXT UNIQUE`, plus `content_hash`, `version`, `difficulty`,
`source_file` from 009.

### `evaluation_runs`
One execution of a suite under a given agent/model/policy/ablation configuration.
009 adds the only **named** CHECK constraint in the schema:

```sql
ALTER TABLE evaluation_runs DROP CONSTRAINT IF EXISTS evaluation_runs_status_check;
ALTER TABLE evaluation_runs ADD CONSTRAINT evaluation_runs_status_check
    CHECK (status IN ('running','done','cancelled','failed'));
```

### `benchmark_results`
Per-scenario outcome. `unsafe BOOLEAN` and `harness_failure BOOLEAN` are
separate flags — a broken environment is never scored as a model failure.

```sql
CREATE UNIQUE INDEX benchmark_results_run_scenario_uniq
    ON benchmark_results (evaluation_run_id, scenario_id);
CREATE INDEX benchmark_results_unsafe_idx
    ON benchmark_results (evaluation_run_id) WHERE unsafe;
```

The unique index is what makes an interrupted suite resumable via upsert.

---

## 8. Sandbox, patches and deployments

### `sandbox_runs`
`purpose` CHECK-constrained to
`('reproduce','test_patch','regression','build','static_check')`.
`stdout_excerpt` / `stderr_excerpt` are bounded at write time.
`resource_limits JSONB` records the containment settings actually applied.

### `remediation_patches`
A candidate fix as a concrete reviewable diff, independent of any action that
applies it. Two FKs into `sandbox_runs` (`reproduction_run_id`, `test_run_id`).
`state` CHECK `('PROPOSED','REPRODUCED','TESTED','REJECTED','PROMOTED')`.

### `deployment_attempts`
Four outbound FKs — the most of any table — to `incidents`,
`remediation_actions`, `remediation_patches` and `verification_runs`.
`state` CHECK `('PENDING','IN_PROGRESS','DEPLOYED','VERIFIED','FAILED','ROLLED_BACK')`.

> **These three tables have no writer.** `GET /v1/deployments`,
> `/v1/deployments/patches` and `/v1/deployments/sandbox-runs` read them, and
> `RuntimePortBridge.deployment_history` queries `deployment_attempts`, but
> `INSERT INTO (sandbox_runs|remediation_patches|deployment_attempts)` appears
> nowhere in `backend/src`. The `/debug` and `/deployments` console pages will be
> empty. See [execution.md](execution.md#8-what-is-not-implemented).

---

## 9. Referential conventions

**`ON DELETE SET NULL` where the child must outlive the parent:**
`resource_leases.incident_id`, `incident_memories.incident_id`,
`sandbox_runs.action_id`, `remediation_patches.reproduction_run_id` /
`test_run_id`, `deployment_attempts.action_id` / `patch_id` /
`verification_id`.

**Deliberate soft references (no FK):** `audit_log.incident_id` (audit survives
deletion), `incidents.owner`, `diagnoses.selected_hypothesis_id`,
`benchmark_results.incident_id` and `.scenario_id`,
`retrieval_documents.ref_id`, `code_documents.service_id`.

**Money is `NUMERIC(12,6)`** in exactly two places: `agent_runs.cost_usd` and
`benchmark_results.cost_usd`.

**Enum-ish columns with no CHECK** (validated in application code):
`incidents.state`, `evidence_items.status`, `evidence_items.trust_class`,
`hypotheses.state`, `remediation_actions.state`, `agent_runs.status`,
`verification_runs.verdict`.

---

## 10. Migrations

`backend/src/aegis/persistence/migrate.py`, entry point `run_migrations(db)`.

1. **Discovery.** `backend/migrations/*.sql`, `sorted()` — ordering is
   lexicographic on filename, which is why the zero-padded `001_`…`009_`
   prefixes matter. Falls back to `/app/migrations` for the container layout.
2. **Advisory lock.** `SELECT pg_advisory_lock(0x41454749)` — `"AEGI"`.
   Session-level, released in a `finally`. Several API and worker replicas
   booting at once serialise here rather than racing into a half-applied schema.
3. **Tracking table.**
   `schema_migrations (filename TEXT PRIMARY KEY, checksum TEXT NOT NULL, applied_at TIMESTAMPTZ)`.
4. **Checksum drift is a hard error.**

   > `migration {name} changed after it was applied; add a new migration
   > instead of editing history`

   This is why every migration is forward-only and idempotent (`IF NOT EXISTS`,
   `ADD COLUMN IF NOT EXISTS`), and why 008/009 append columns rather than
   editing 004/005.
5. **Per-file transaction.** Each file runs in its own transaction with its
   `schema_migrations` row inserted in the same transaction. A failure rolls back
   that one file and leaves earlier files intact.

Migrations run automatically in the API's lifespan before it serves traffic, and
in the worker before it claims its first job.

---

## 11. Connection pooling

`backend/src/aegis/persistence/db.py`, class `Database`. One `asyncpg` pool per
process, injected — never a module global.

| Setting | Default |
|---|---|
| `POSTGRES_POOL_MIN` | 2 |
| `POSTGRES_POOL_MAX` | 16 |
| `POSTGRES_STATEMENT_TIMEOUT_MS` | 15,000 |
| `max_inactive_connection_lifetime` | 300 s |
| `idle_in_transaction_session_timeout` | 30,000 ms |

`application_name` is set to `aegis-<otel_service_name>` so a slow query is
attributable to a process. Every pooled connection registers JSONB/JSON codecs,
so repositories receive Python dicts directly and never call `json.loads`.

`acquire()` yields a bare connection; `transaction()` yields one inside a
transaction — the latter is what makes "create incident and enqueue its job"
atomic. `healthy()` runs `SELECT 1` for `/health/ready`.

---

## 12. Neo4j

`graph/ontology.py` is the single authoritative schema definition — `NodeLabel`
and `RelType` are closed `StrEnum`s and `constraints_cypher()` emits the
uniqueness constraints.

What belongs there: services, dependencies, deployments, commits, ownership,
propagation paths. What does not: anything that decides something.
`Incident`, `Alert`, `Remediation` and `Verification` nodes exist as
**projections** — an identifier plus the handful of properties a traversal needs
to rank and join.

Every write is a `MERGE` keyed on the canonical `service_id`. A pod restarting or
a scale event re-running discovery updates the existing node, never mints a
second one; a duplicated service silently halves every blast-radius answer that
follows. See [graph.md](graph.md).

---

## 13. Redis

Cache and the SSE pub/sub bus for `GET /v1/incidents/{id}/stream`. Nothing in
Redis is authoritative, and nothing reads a decision back out of it. Losing Redis
costs live streaming and some cache hits; it does not affect correctness.

Compose runs it with `--appendonly no --maxmemory 256mb --maxmemory-policy allkeys-lru`,
which is the correct configuration for a store nothing depends on.

---

## See also

- [policy.md](policy.md) · [execution.md](execution.md) · [evidence.md](evidence.md)
- [graph.md](graph.md) · [retrieval.md](retrieval.md) · [evaluation.md](evaluation.md)
- [local-development.md](local-development.md) — connecting to the local database
