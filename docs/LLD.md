# Aegis 2.0 — Low-Level Design

**Companion to:** [HLD.md](HLD.md) · **Normative specs:** `ESD.md`, `AIArchitecture.md`
**Status:** as built. Every type, constant and SQL fragment below was read out of
the source. Where a spec says otherwise, §14 records the divergence.

This document fixes module boundaries, key types, invariants and algorithms. It
is the contract a reviewer checks an implementation against.

---

## 1. Layering and dependency direction

```text
        api/  worker/  mcp/            <- delivery (I/O, no business rules)
              |
  agents/ execution/ verification/     <- orchestration and capability
  evaluation/ retrieval/ memory/ graph/
              |
         policy/  evidence/            <- decision and provenance
              |
       persistence/  telemetry/        <- gateways to external state
       integrations/
              |
            domain/                    <- pure types, enums, state machines
              |
             core/                     <- config, errors, logging, resilience
```

**Rule:** dependencies point downward only. `domain/` imports nothing above
`core/` and performs no I/O, which makes every state machine and policy rule
unit-testable without a database.

> The rule is currently enforced by review and by mypy's import graph. There is
> **no** automated layering check — `scripts/` contains only `seed_topology.py`
> and `ci/`.

`container.py` sits outside the stack as the composition root. It is the one
place that may know about every layer, and both the API and the worker build
from it — the alternative is an API that enforces a different policy from the
worker that executes.

## 2. `core/` — foundations

| Module | Contents |
|---|---|
| `config.py` | `Settings` (pydantic-settings). Validates the whole `.env` at import and raises `ConfigError` at boot rather than defaulting permissively. `allowed_tiers` returns an empty frozenset whenever autonomy is off. |
| `errors.py` | `AegisError` and 14 subclasses. Each carries `code`, `http_status`, `retryable`, `context`. |
| `logging.py` | structlog. Binds `correlation_id` / `incident_id` via `ContextVar`. The redaction processor runs **last**, so it also covers values injected by libraries. |
| `resilience.py` | `with_timeout`, `retry_async` (full jitter, only for `retryable`), `CircuitBreaker` (closed/open/half-open), `Bulkhead`, and `guarded_call` which composes all four. |
| `ids.py` | `new_id(prefix)` — ULID-based, k-sortable, so timeline queries stay index-friendly. Typed prefixes (`ACTION`, `LEASE`, `SANDBOX`, `VERIFICATION`, …). |
| `clock.py` | `Clock` protocol, `SYSTEM_CLOCK`, injectable fakes. **No module calls `datetime.now()` directly** — this is what makes time-dependent policy testable. |

### 2.1 Resilience is composed, not decorated

There is no `@breaker` decorator. Call sites use `guarded_call`, which applies
timeout, bounded retry, breaker and bulkhead together — so an unbounded `await`
requires deliberately bypassing the helper.

```python
async with self._bulkhead:
    return await guarded_call(_do, timeout_s=..., attempts=..., breaker="prometheus")
```

Breaker state is **process-local by design**: a shared breaker would need a
network round trip to decide whether to make a network call. It is exposed
through `breaker_states()` and rendered at `/settings`; it is **not** exported as
a Prometheus metric, because the API exposes no metrics endpoint (§14).

## 3. `domain/` — pure model

### 3.1 Closed vocabularies

`Severity` P1–P4 · `IncidentState` (12) · `ActionState` (**11**) · `RiskTier`
0–3 · `PolicyEffect` ALLOW / REQUIRE_HUMAN / BLOCK · `ActionType` (13) ·
`TrustClass` TIER_A–D with weights 1.0 / 0.8 / 0.5 / 0.2 · `SourceType` (10) ·
`EvidenceType` (16) · `EvidenceStatus` (4, including **SOURCE_UNAVAILABLE**) ·
`HypothesisState` (5) · `MetricDirection` (3) · `FailureClass` (16) ·
`AgentRole` (10) · `ServiceHealth` (4) · `ClaimOutcome` (4, including
**UNAVAILABLE**) · `VerificationVerdict` (5) · `VerificationTestKind` (10).

Every one is closed on purpose: a string where an enum belongs is how an LLM
eventually invents an action type, a risk tier or a severity that no policy rule
covers.

Two carry the "found nothing ≠ could not look" rule into the type system:
`EvidenceStatus.SOURCE_UNAVAILABLE` and `ClaimOutcome.UNAVAILABLE`.

`FailureClass.is_harness_failure` marks `ENVIRONMENT_FAILURE`,
`PROVIDER_FAILURE` and `OBSERVABILITY_FAILURE`, so a broken benchmark
environment is never scored as a model-quality failure.

### 3.2 State machines

```text
RECEIVED -> TRIAGING -> INVESTIGATING -> DIAGNOSING -> DEBUGGING -> VERIFYING
   -> AWAITING_APPROVAL -> REMEDIATING -> MONITORING -> RESOLVED
   (ESCALATED and BLOCKED reachable from active states)
```

`domain/state_machines.py` exposes `can_transition_incident(src, dst)` and
`can_transition_action(src, dst)` over explicit adjacency tables, not scattered
conditionals. `IncidentState.is_terminal` is `RESOLVED` alone;
`ActionState.is_terminal` covers `SUCCESS`, `FAILED`, `ROLLED_BACK`, `BLOCKED`
and `EXPIRED`.

Transitions are persisted to `incident_state_transitions` with actor, reason and
timestamp. `ActionRepository.transition` is **conditional on the expected current
state**, so a concurrent writer loses rather than silently clobbering.

### 3.3 Key aggregates

`domain/models.py` holds 23 model classes. The two that carry the most weight:

```python
class EvidenceItem(BaseModel):         # immutable once written
    id: str
    incident_id: str
    source: str                        # "prometheus" | "tempo" | "neo4j" | ...
    source_type: SourceType
    evidence_type: EvidenceType
    observed_at: datetime | None       # when the world showed it
    retrieved_at: datetime             # when Aegis looked
    resource_id: str | None
    structured_value: dict[str, Any]   # machine comparable
    content: UntrustedText | str | None
    provenance_uri: str                # exact query needed to reproduce it
    trust_class: TrustClass            # assigned by the store, never by a caller
    status: EvidenceStatus
    content_hash: str                  # sha256 — tamper evidence and dedup
```

`observed_at` versus `retrieved_at` is deliberate: correlating a deploy with a
latency change requires knowing when something *happened*, not when it was
fetched.

`UntrustedText` is the envelope every free-text field from outside must travel
in. Its enforcement lives in `mcp/types.py :: validate_output_model`, which
refuses to register a tool whose output model would hand a model raw log text.

## 4. `evidence/` — provenance

`EvidenceStore` is append-only and content-addressed. `trust_for(source_type)`
is the single assignment point, defaulting unknown sources to Tier D.
`record_unavailable(...)` is a **separate method** from `record(...)`.

`EvidenceValidator.validate_citations` returns a `ValidationReport` separating
`resolved` / `unknown` / `foreign` / `refuted` / `unavailable` plus
`tier_a_count`. A claim whose citations do not resolve is rejected;
`abstain(...)` turns the rejection into a legitimate `Diagnosis`.

### 4.1 Confidence derivation (never the model's number)

```python
CONFIDENCE_MODEL_VERSION = "1.0.0"
W_COVERAGE = 0.40 · W_CORROBORATION = 0.25 · W_TEST_PASS = 0.20
W_SOURCE_RELIABILITY = 0.15 · W_CONTRADICTION_PENALTY = 0.30 · W_GAP_PENALTY = 0.10
```

`ConfidenceBreakdown` exposes every input so the UI can explain the number.
Weights are versioned and calibrated against the benchmark with Brier score and
ECE, so the number means something measurable.

Full detail: [evidence.md](evidence.md).

## 5. `policy/` — the deterministic gate

`decide(ctx) -> PolicyDecision` is a **pure function** of a `PolicyContext`.

### 5.1 Twelve rules, and all of them run

```python
_RULES = (
    _rule_kill_switch, _rule_tier_three, _rule_concurrency, _rule_rate_limit,
    _rule_abstained_diagnosis, _rule_autonomy_disabled, _rule_evidence_quality,
    _rule_confidence_floor, _rule_blast_radius, _rule_rollback_required,
    _rule_verification_required, _rule_production_allowlist,
)
```

Rules 1–4 return `BLOCK`; rules 5–12 return `REQUIRE_HUMAN`. **Every rule is
evaluated** even after the first failure, and `BLOCK` outranks `REQUIRE_HUMAN`,
so the persisted decision lists every reason an action was held back rather than
only the first. No decisive rule ⇒ `ALLOW` with
`matched_rule="all_gates_passed"`.

```python
@dataclass(frozen=True)
class PolicyDecision:
    effect: PolicyEffect
    risk_tier: RiskTier         # computed independently of effect
    matched_rule: str
    reasons: list[str]
    gates: list[GateResult]     # per-rule pass/fail with reason
    policy_version: str         # "1.0.0"
    decided_at: datetime
```

`risk_tier` comes from a static `ActionType -> ActionProfile` table, asserted
complete at import. It is never derived from model output — the mitigation for
talking an unsafe action into a lower class.

Evidence-quality floors by tier: 0.00 / 0.45 / 0.60 / 1.00. Rule 7 additionally
refuses any action with **zero Tier-A evidence**.

### 5.2 Kill switches

`KillSwitchState` is an immutable snapshot loaded once per decision over four
scopes (global, environment, action type, service). `PolicyStore` returns
`KillSwitchState.fail_closed(reason)` if the table cannot be read — an unreadable
store means every switch engaged.

Full detail: [policy.md](policy.md).

## 6. `execution/` — the gate chain and the executors

### 6.1 `ValidatedAction`

```python
_GATE_TOKEN: Final = object()          # module-private, never exported

@dataclass(frozen=True, slots=True)
class ValidatedAction:
    token: Any
    def __post_init__(self) -> None:
        if self.token is not _GATE_TOKEN:
            raise _Unauthorised(...)
```

Every executor signature takes a `ValidatedAction`; none takes a proposal. This
is the structural reason an agent cannot execute anything: it has no way to
manufacture the input type.

`still_valid(now)` re-tests lease expiry and approval usability, and is called
again immediately before the write — validation and execution are separated by
real time.

### 6.2 The chain

`ActionGate.validate` runs **five** gates — schema, evidence, policy, authz,
lease — and returns either a `ValidatedAction` or a `GateRejection` carrying the
full gate list. A rejection is a normal outcome; a malformed proposal or a
fabricated citation **raises**, so a bug is never mistaken for a policy decision.

The lease is acquired **last**, after policy and authorisation have said yes.
Taking it earlier would hold the resource for the whole approval TTL.

### 6.3 Registry

`execution/registry.py` maps 8 of the 13 action types to an executor. Tier-3
types are absent and an **import-time assertion** enforces it:

```python
assert not (set(_REGISTRY) & tier_three_actions())
```

`executor_for` raises `PolicyViolation` rather than returning `None`, so a
caller cannot read a missing executor as "nothing to do".

`promote_patch` is tier 2 and also absent: promotion belongs to a deployment
pipeline, which is not implemented (§14).

### 6.4 Ports and adapters

`execution/ports.py` states what the safety-critical code needs
(`RuntimeReadPort`, `RuntimeWritePort`, `CachePort`). `integrations/runtime.py`
provides `RuntimeAdapter` — an ABC over Docker Compose, Kubernetes and ECS with
disjoint `READ_METHODS` / `WRITE_METHODS` class variables (a test asserts the
disjointness). `execution/adapters.py` bridges the two.

Every write takes an `idempotency_key` and **converges** rather than repeating;
none is retried inside the adapter (`attempts=1`); each returns a `WriteResult`
naming the exact API call performed. **No shell** — Docker Engine API,
Kubernetes API or AWS API only.

The bridge raises "unsupported" rather than returning a successful-looking no-op
for an operation the backend cannot perform.

### 6.5 Idempotency and leases

```sql
idempotency_key TEXT NOT NULL UNIQUE          -- remediation_actions

CREATE UNIQUE INDEX resource_leases_active_uniq
    ON resource_leases (resource_type, resource_id)
    WHERE released_at IS NULL;
```

The key is unique **globally**, not composite. `ActionRepository.propose`
returns the existing row on conflict, so a retried proposal observes the original
action rather than acting twice.

Lease acquisition reaps any expired lease and inserts the new one **in one
transaction**, with the audit row written inside the same transaction. Expiry
frees the lock; it is not a statement that the resource is in a good state.

### 6.6 `ExecutionService`

Owns `execute → verify → commit | rollback`. Guarantees: the lease is released in
a `finally`; authorisation is re-checked immediately before the write;
verification failure triggers rollback and a failed rollback escalates loudly;
nothing is auto-retried unless the action profile says the type is idempotent.

### 6.7 Sandbox

`SandboxRunner.run(spec)` — network `none` by default, read-only root,
`cap_drop: ALL`, `no-new-privileges`, `pids_limit: 512`, memory and swap capped
equal, tmpfs-only `/workspace` (512 m) and `/tmp` (128 m), wall-clock kill,
output truncated at 32,000 chars, orphan reaping by the `aegis.sandbox` label.

The container environment is built from an **explicit allowlist** and inherits
nothing; `SandboxSpec.__post_init__` refuses any variable whose name contains
`SECRET`, `TOKEN`, `PASSWORD`, `KEY` or `CREDENTIAL`.

The Docker SDK is synchronous, so every call is dispatched to a thread — blocking
the event loop here would stall every other incident in the process.

Full detail: [execution.md](execution.md).

## 7. `verification/` — deterministic proof

The primitive is `CLAIM + EVIDENCE + TEST + RESULT + TIMESTAMP`. `ClaimTest`
declares the threshold **before** the measurement, which is what stops post-hoc
rationalisation.

```python
def decide_verdict(results) -> VerificationVerdict:
    # 1 protected failure -> REGRESSION_DETECTED
    # 2 any goal failure  -> FAILED
    # 3 no claims, or every goal unmeasurable -> INCONCLUSIVE
    # 4 all PASS          -> VERIFIED
    # 5 otherwise         -> PARTIALLY_VERIFIED
```

Pure, total and order-independent. `VERIFIED` requires `all(PASS)`, so a single
`UNAVAILABLE` claim can never reach it. `is_success` is true for `VERIFIED`
alone — `PARTIALLY_VERIFIED` is deliberately not a pass.

`MIN_SAMPLES_FOR_COMPARISON = 3`. The baseline is captured **before** the action;
claims with a missing baseline resolve `INCONCLUSIVE` rather than borrowing a
default. No LLM participates.

Persisted as `verification_runs` plus one `verification_claims` row per claim, so
a verdict can be disputed line by line. Nothing updates a completed run.

Full detail: [verification.md](verification.md).

## 8. `graph/` — Neo4j

`graph/ontology.py` is the single authoritative schema. `NodeLabel` and
`RelType` are closed enums because Cypher cannot parameterise a label;
`label_token` / `rel_token` / `rel_union_token` are the only supported way to
render one, and an injection attempt raises `ValidationError`.

All writes are `MERGE` on the canonical `service_id`, and `last_seen` is set on
every node and edge. Batches are bounded twice — rows per statement and rows per
call.

`graph/traversal.py` clamps depth to `MAX_DEPTH` (the rendered variable-length
bound is one of seven possible strings) and returns typed results — `CausalPath`,
`DeploymentSummary`, `TeamRef`, `BlastRadius` — rather than dicts, because a key
rename would break topology ingestion silently. Nothing catches
`SourceUnavailable`; "no dependents" and "could not ask" reach the caller as
different things.

`graph/graphrag.py` ranks deterministically on structural distance, temporal
relevance and causality. The node budget is a hard cap and truncation is recorded
in provenance (`GraphContext.truncated`), because a silently shortened context is
indistinguishable from a small blast radius.

Full detail: [graph.md](graph.md).

## 9. `retrieval/` and `memory/`

```text
query -> [lexical tsvector | vector pgvector | graph scope | recency]
      -> reciprocal rank fusion -> bounded result
```

```python
RRF_K = 60
WEIGHT_LEXICAL = 1.0 · WEIGHT_VECTOR = 0.9 · WEIGHT_GRAPH = 0.7 · WEIGHT_RECENCY = 0.5
RECENCY_HALF_LIFE_DAYS = 14.0
MAX_QUERY_CHARS = 1_000 · MAX_RESULT_LIMIT = 50
```

Fusion combines **ranks**, not scores, so neither ranker's distribution has to be
calibrated against the other. `reciprocal_rank_fusion` is a pure function.

There is **no rerank stage**. Bounding is by result count plus per-chunk
character caps (`DEFAULT_CHUNK_CHARS = 2_000`, `MAX_CHUNKS_PER_DOCUMENT = 200`),
not by a token budget.

`EmbeddingClient` talks to Google AI Studio only, over the four-key ring in
`core/keyring.py`. An unconfigured provider is reported — never zero vectors —
and a dimension mismatch is a `ConfigError` raised at the client, not a Postgres
type error raised at insert.

`retrieval/code.py` narrows in capped stages (12 services → 8 repos → 50 commits
→ 40 files → 25 symbols → 15 tests), recording a `StageTrace` per stage so a
localisation failure is diagnosable.

`memory/store.py` refuses to write unless the diagnosis did not abstain, the
remediation was verified, and the memory is approved. `memory/recall.py` keeps
recurrence-signature matches and hybrid-similarity matches **separate**, because
collapsing them would give a loose resemblance the authority of an exact
recurrence. Memory is always Tier C.

Full detail: [retrieval.md](retrieval.md).

## 10. `agents/` — LangGraph

Twelve nodes:

```text
triage -> investigate -> {analyze_topology ‖ recall_memory ‖ analyze_changes}
       -> hypothesize -> diagnose
       -> (abstained AND loops < limit AND budget) -> investigate
       -> localize_code -> plan_remediation
       -> {execute_remediation -> learn | finalize} -> finalize -> END
```

- `IncidentState` (TypedDict) holds **references and summaries only**; evidence
  bodies live in Postgres. Parallel branches merge through `merge_lists`, not
  last-write-wins.
- `BudgetGuard` is checked on entry to every node. Nodes receive a frozen
  `BudgetView` with **no mutators**, so an agent cannot raise its own limits.
  `BudgetExhausted` is caught by `run_investigation` and treated as a normal
  partial outcome.
- The loop needs abstention **and** a counter under `AGENT_HYPOTHESIS_LOOP_LIMIT`
  **and** remaining budget. The LangGraph recursion limit is `2 * limit + 12`.
- **Human approval is not a LangGraph interrupt.** `_after_plan` routes to
  `finalize` when `awaiting_approval` is set: the graph ends, and an approval
  arriving through the API enqueues fresh work. Blocking a worker on a human
  would hold its budget and resources for the whole approval TTL.
- **`ValidatedAction` is never checkpointed.** It lives on `WorkflowDeps` as
  run-scoped `pending_action`, because it carries a live lease and a live
  approval. A resumed run re-gates rather than replaying a permission granted
  before the crash.
- A Postgres checkpointer is used when available; without it the graph runs,
  just without resume.
- `ModelRouter` maps a `TaskClass` to a model with provider fallback, validating
  structured output against a pydantic model. A parse failure is retried once and
  then surfaced so the orchestrator can abstain. Fallback preserves the schema and
  every gate, so degrading the model can never degrade safety.
- Prompts are versioned (`PROMPT_VERSION = "1.0.0"`), stamped onto `agent_runs`.

Full detail: [agents.md](agents.md).

## 11. `mcp/` — the tool boundary

36 tools, 35 read and 1 write, across 12 scopes. The registry is built at import
and frozen; duplicate names, unknown scopes, un-annotated untrusted output and a
write tool that does not demand a `ValidatedAction` all fail **at startup**.
`read_tools()` and `write_tools()` are asserted disjoint; `permitted_for` fails
closed.

`ToolInvoker.invoke` is the only entry point and enforces, in order: strict
schema validation, scope and environment permission, budget charge, bounded
execution, retry only when `retryable and idempotent`, a `ValidatedAction` for
any write, a `tool_calls` row for every outcome including denials, and a typed
`ToolResult` instead of any traceback.

`ToolResult.degraded` is what keeps "found nothing" and "could not look" distinct
at the boundary. No raw PromQL, TraceQL, LogQL, Cypher or shell command crosses
it.

Full detail: [mcp-tools.md](mcp-tools.md).

## 12. `api/` — FastAPI

- **16 routers**: `health` (mounted at root), and at `/v1`: `alerts`,
  `incidents`, `actions`, `approvals`, `audit`, `deployments`, `evaluation`,
  `graph`, `integrations`, `investigations`, `policy` (`policy_admin`),
  `reliability`, `stream`, `systems`, `tasks`.
- **Auth:** `FirebaseVerifier` yields a `Principal(uid, email, roles)`;
  authorisation is a separate role check. Roles are hierarchical —
  `viewer ⊂ responder ⊂ approver ⊂ admin`, and an unknown role grants nothing.
  An unconfigured verifier reports unconfigured and the API rejects every
  authenticated request: fail closed, stay up.
- **Ingestion** uses a constant-time-compared `X-Aegis-Ingest-Token` header, not
  a user session.
- **SSE** (`/v1/incidents/{id}/stream`): Redis pub/sub fan-out, `ping` events,
  a monotonic sequence on every frame and `Last-Event-ID` resume. A client that
  falls too far behind is dropped and reconnects with its last id.
- **Correlation middleware** generates or echoes `X-Correlation-ID` and binds it
  into logging context for the whole request.
- A global handler maps `AegisError` to `{code, message, context}` with the right
  status, and an unhandled-exception handler never leaks internals.
- `docs_url` is disabled in production.

## 13. `persistence/`

Modules: `db.py`, `actions.py`, `audit.py`, `incidents.py`, `jobs.py`,
`migrate.py`. Repositories are thin and explicit — **there is no ORM and no
`UnitOfWork` class**; `Database.transaction()` is the transaction scope.

One asyncpg pool per process (`min 2` / `max 16`), `command_timeout` from
`POSTGRES_STATEMENT_TIMEOUT_MS` (15 s), `idle_in_transaction_session_timeout`
30 s, `application_name = aegis-<service>`, and JSONB codecs registered on every
connection so repositories never call `json.loads`.

`JobQueue` uses `FOR UPDATE SKIP LOCKED`, which gives competing workers
exactly-once delivery with no coordination between them, and keeps "create the
incident and schedule its investigation" inside one transaction.

`AuditLog` is append-only — there is no update or delete path in the module. A
failed write is logged at ERROR and counted; it is never silently dropped and
never aborts a remediation.

Migrations are ordered SQL files applied under `pg_advisory_lock(0x41454749)`,
one transaction per file, with a truncated-sha256 checksum guard that makes
editing an applied migration a hard error.

Indexes that carry correctness rather than speed: the active-lease partial
unique index, `approvals_one_open_idx`, `workflow_jobs_one_active_idx`,
`evidence_dedup_idx`, `remediation_actions.idempotency_key`,
`benchmark_results_run_scenario_uniq`.

Full detail: [data-model.md](data-model.md).

## 14. As built: gaps against this design

| Intended | Actual |
|---|---|
| "the eight gates" in a `gates.py` | No such module. **Five** gates in `ActionGate`, **twelve** rules inside the policy gate. |
| `UnitOfWork` | Does not exist. `Database.transaction()` is the scope. |
| `scripts/check_layering.py` in CI | Does not exist. Layering is enforced by review and mypy. |
| Breaker state exported as a metric | Exposed via `breaker_states()` and `/settings` only. The API has no `/metrics` endpoint at all. |
| Human approval as a LangGraph `interrupt` | The graph ends; the approval enqueues fresh work. |
| Retrieval budgeted by token count, with a rerank stage | Bounded by result count and chunk character caps. No rerank. |
| Debug → patch → sandbox → promote | Sandbox and its tools are complete. Nothing writes `remediation_patches`, `deployment_attempts` or `sandbox_runs`; `promote_patch` has no executor. |
| Integration tests with testcontainers; E2E and chaos suites; 80% coverage gate with 100% on `policy/` and `domain/` | 11 integration tests against a live local Postgres, which **skip** when it is absent. No E2E suite, no chaos suite, no coverage gate. The `chaos` pytest marker is declared but unused. |
| Autonomous tier-1 action in production | `service_allowlist` is hard-coded empty in `ActionGate._build_context`, so production always requires a human. |
| Work distributed over a queue service | Postgres `workflow_jobs`. The AWS SQS queue and its autoscaling policy are inert. |

Each is reproduced and explained in the focused documents:
[execution.md §8](execution.md#8-what-is-not-implemented) ·
[policy.md §5](policy.md#5-known-limitation-the-production-allowlist-is-empty) ·
[testing.md](testing.md) · [observability.md §4](observability.md#4-known-gap-aegis-exposes-no-prometheus-metrics) ·
[evaluation.md §6](evaluation.md#6-known-gaps).
