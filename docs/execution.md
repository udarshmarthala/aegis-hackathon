# Execution

How a model's suggestion becomes a change to a running system — and why most of
them never do.

Source: `backend/src/aegis/execution/` (10 modules, ~3,600 lines).

---

## 1. The structural claim

`ValidatedAction` is the only type any executor accepts, and it can only be
constructed by `ActionGate.validate`.

`backend/src/aegis/execution/validated.py` holds a module-private sentinel:

```python
_GATE_TOKEN: Final = object()

@dataclass(frozen=True, slots=True)
class ValidatedAction:
    token: Any
    ...
    def __post_init__(self) -> None:
        if self.token is not _GATE_TOKEN:
            raise _Unauthorised(
                "ValidatedAction may only be constructed by ActionGate.validate"
            )
```

The token is never exported. Every executor signature in
`execution/executors.py` takes a `ValidatedAction`; none takes an
`ActionProposal`. An agent can build an `ActionProposal` — that is its job — but
it has no reachable path to a `ValidatedAction`.

This is the difference between a safety property enforced by the type system and
one enforced by reviewer vigilance. The gate chain cannot be skipped by calling a
different method, because there is no other method.

---

## 2. The gate chain

```
schema -> evidence -> policy -> authz -> lease -> execute -> verify -> commit/rollback
\________________ ActionGate ________________/\____ ExecutionService ____/
```

`ActionGate` owns the first five. `ExecutionService` owns execute, verify and
the commit/rollback decision. Every gate is deterministic: no gate consults a
model, and no gate reads the proposal's `reason` text. A persuasive
justification cannot move an action into a lower risk class.

### Gate 1 — schema (`_gate_schema`)

Cross-field rules Pydantic cannot express:

| Check | Rejection |
|---|---|
| `action_type` in the closed `ActionType` enum | unknown action type |
| `target.environment` has no surrounding whitespace | whitespace in environment |
| `target.resource_id` is non-empty | empty resource id |
| `len(idempotency_key) >= 8` | key too short to be collision-resistant |
| profile requires verification ⇒ `verification.target_metric` non-empty | no verification plan |
| `expected_effect.window_seconds > 0` | non-positive effect window |

Failures raise `ValidationError`. A malformed proposal is a bug, not a policy
outcome, so it raises rather than returning a rejection.

### Gate 2 — evidence (`_gate_evidence`)

Delegates to `EvidenceValidator.validate_citations(incident_id, supporting_evidence)`.
A proposal is rejected when any cited id does not exist, belongs to another
incident, was refuted, or came from an unavailable source. Rejection raises
`EvidenceError` — the action is not softened with a caveat, it stops.

### Gate 3 — policy (`policy.engine.decide`)

The full deterministic rule set, documented in [policy.md](policy.md). The gate
builds a `PolicyContext` from **persisted state** rather than from caller
arguments wherever it can, so an agent cannot influence a decision by supplying a
flattering number. Kill switches are loaded fail-closed.

The complete context is snapshotted into `policy_decisions.context_snapshot` so
an auditor can rebuild the inputs and confirm the same rules still yield the same
effect.

A `BLOCK` transitions the action to `BLOCKED` and returns a `GateRejection`.

### Gate 4 — authorisation

Only runs when the effect is `REQUIRE_HUMAN`. It looks for an existing, usable
approval bound to this action id. If none exists, or one lapsed, it opens an
`ApprovalRequest`, moves the action to `HUMAN_REQUIRED`, and returns a
`GateRejection` with `needs_approval=True`. "Nobody approved yet" and "an
approval expired" are recorded distinctly.

Two belt-and-braces checks follow:

```python
if risk_tier_for(proposal.action_type) is RiskTier.HUMAN_ONLY:
    raise PolicyViolation(f"{...} is tier 3 and has no execution path", ...)
```

and an `AuthorizationError` if policy somehow allowed an autonomous action whose
tier is not in `settings.allowed_tiers`.

### Gate 5 — lease

The lease is acquired **last**, after policy and authorisation have already said
yes. Taking it earlier would hold the resource while a proposal waited for a
human — potentially for the full approval TTL (900s by default).

Arbitration is done by Postgres, not by the application. See
[data-model.md](data-model.md#4-the-safety-critical-tables) and section 5 below.

---

## 3. Tier 3 has no executor

`execution/registry.py` maps `ActionType` to `Executor`. Tier-3 types are absent:

```python
_REGISTRY: Final[dict[ActionType, Executor]] = {
    # tier 1
    ActionType.RESTART_INSTANCE:    RestartInstanceExecutor(),
    ActionType.RERUN_HEALTH_CHECK:  RerunHealthCheckExecutor(),
    ActionType.SCALE_UP_BOUNDED:    ScaleUpBoundedExecutor(),
    ActionType.CLEAR_CACHE_KEY:     ClearCacheKeyExecutor(),
    # tier 2
    ActionType.ROLLBACK_DEPLOYMENT: RollbackDeploymentExecutor(),
    ActionType.SCALE_SERVICE:       ScaleServiceExecutor(),
    ActionType.UPDATE_CONFIG:       UpdateConfigExecutor(),
    ActionType.DRAIN_INSTANCE:      DrainInstanceExecutor(),
    # tier 3 - deliberately absent. There is nothing to call.
}

assert not (set(_REGISTRY) & tier_three_actions()), (
    "a tier-3 action type has an executor registered; this is a safety regression"
)
```

Eight executable types out of thirteen declared. Prohibition is expressed as the
absence of code rather than as a check that could be edited away, and the
assertion runs at **import time** rather than in a test that might be skipped.

`PROMOTE_PATCH` is tier 2 but also has no executor, on purpose: promotion belongs
to a deployment pipeline, not to a direct environment write. That pipeline is not
implemented — see [limitations](#8-what-is-not-implemented).

`executor_for` raises `PolicyViolation` rather than returning `None`, because a
caller receiving `None` might read it as "nothing to do" and mark the action
successful.

---

## 4. Execution, verification and rollback

`ExecutionService.execute(validated)` guarantees four things regardless of how
any step fails:

1. **The lease is always released.** It runs in a `finally`. Expiry is the
   backstop for a crashed worker; the normal path never relies on it.
2. **Authorisation is re-checked immediately before the write.**
   `ValidatedAction.still_valid(now)` re-tests the lease expiry and the
   approval's usability. Validation and execution are separated by real time,
   and that gap is exactly where a stale permission would otherwise be used.
3. **Verification failure triggers rollback; a failed rollback escalates
   loudly.** "Could not be undone and did not work" is the worst state the
   system can reach, and it is recorded explicitly rather than swallowed.
4. **Nothing is retried automatically** unless the action profile says the type
   is idempotent (`ExecutionService.is_retryable`).

Executors themselves never retry internally. `restart_instance` is idempotent;
`promote_patch` is not. The decision belongs to the caller, which knows the
profile. A retry loop buried in an executor would eventually replay a
non-idempotent write.

---

## 5. Leases

`execution/leases.py`. A lease is proof that this worker, and only this worker,
may act on a resource.

The arbiter is a Postgres partial unique index:

```sql
CREATE UNIQUE INDEX IF NOT EXISTS resource_leases_active_uniq
    ON resource_leases (resource_type, resource_id)
    WHERE released_at IS NULL;
```

Two workers racing produce one winner and one `LeaseConflict` at the database
level. Any scheme that checked for a lease and then inserted one would have a
window between the two statements; this has none.

Reaping an expired lease and inserting the new one happen in **one transaction**,
so the window between "this looks expired" and "I have taken it" does not exist.
The audit row is written inside the same transaction — an audit entry for a lease
that was rolled back would be a false record.

Default TTL: `RESOURCE_LEASE_TTL_SECONDS`, 300s.

Expiry frees the lock. It is deliberately **not** a licence to act: whether the
original action left the resource half-changed is a separate question the
verification engine answers.

---

## 6. Approvals

`execution/approvals.py`. Three rules make an approval meaningful rather than
ceremonial:

1. **Approvals expire.** `approvals.expires_at` is `NOT NULL`. An operator who
   approved a rollback twenty minutes ago approved it against the system as it
   was then. TTL is `APPROVAL_TTL_SECONDS`, 900s by default, enforced at grant
   time and re-checked at execution time.
2. **An approval is bound to one action id.** It is not a token for "restart
   something". A modified proposal needs a new approval. At most one open
   approval per action exists, enforced by
   `approvals_one_open_idx ON approvals (action_id) WHERE decision IS NULL`.
3. **The decision is recorded with the decider.** `actor_type='human'` in
   `audit_log` is what makes "did a person authorise this?" answerable. No code
   path in Aegis writes a human approval on behalf of a model.

`more_evidence` is a first-class third outcome alongside `approved` and
`rejected`. An operator who cannot yet decide should not be forced into a binary.

---

## 7. The sandbox

`execution/sandbox.py`. Where Aegis runs work it does not fully trust:
reproducing a failure, applying a candidate patch, running a test suite.

### The credential boundary

The container environment is **built from an allowlist, never inherited**:

```python
env = {
    "HOME": spec.workdir,
    "PATH": "/usr/local/bin:/usr/local/sbin:/usr/bin:/bin",
    "CI": "true",
    "AEGIS_SANDBOX": "1",
    "PYTHONDONTWRITEBYTECODE": "1",
    "PIP_DISABLE_PIP_VERSION_CHECK": "1",
}
env.update(spec.env)
```

A patch that exfiltrates `os.environ` finds nothing worth having. On top of
that, `SandboxSpec.__post_init__` refuses any caller-supplied variable whose
uppercased name contains `SECRET`, `TOKEN`, `PASSWORD`, `KEY` or `CREDENTIAL` —
refused outright rather than trusted to be harmless, because the cost of being
wrong is a leaked key.

### Containment

| Control | Value |
|---|---|
| Network | `spec.network or settings.sandbox_network`, default **`none`** |
| Memory | `SANDBOX_MEMORY_LIMIT` (2g), with `memswap_limit` equal — no swap escape |
| CPU | `SANDBOX_CPU_LIMIT` (2.0) as `nano_cpus` |
| PIDs | 512 |
| Root filesystem | `read_only: True` |
| Writable space | tmpfs only: `/workspace` 512m, `/tmp` 128m |
| Capabilities | `cap_drop: ["ALL"]` |
| Privilege escalation | `security_opt: ["no-new-privileges:true"]` |
| Wall clock | `SANDBOX_WALL_CLOCK_LIMIT_S` (300) |
| Output | truncated at 32,000 chars at write time |
| Patch size | 1 MiB max |
| Artifacts | 8 MiB max |

Cleanup runs in a `finally`. A crashed worker leaves at most one container, which
`reap_orphans` removes by the `aegis.sandbox` label.

The Docker SDK is synchronous, so every call is dispatched to a worker thread —
blocking the event loop here would stall every other incident in the process.

### No arbitrary shell

The bootstrap script separates checkout (exit 90), patch application (exit 91)
and the user command into distinct exit codes, so "the patch did not apply" is
distinguishable from "the tests failed". Conflating them would let a malformed
patch be reported as a failing fix.

The MCP sandbox tools expose a **closed set of command templates** (`pytest`,
`unittest`, `npm_test`, `go_test`, `make_test`) plus one pattern-validated
repo-relative target. A free `command` parameter would be a remote-code-execution
surface wearing a tool schema.

---

## 8. What is not implemented

Recorded honestly; these are database tables and read APIs without a writer.

| Surface | State |
|---|---|
| `remediation_patches` | Table exists (migration 007). `GET /v1/deployments/patches` reads it. **Nothing inserts a row.** Patch generation has no driver code. |
| `deployment_attempts` | Table exists. `GET /v1/deployments` reads it, and `RuntimePortBridge.deployment_history` queries it. **Nothing inserts a row.** The staging deployment pipeline is not implemented. |
| `sandbox_runs` | Table exists. `GET /v1/deployments/sandbox-runs` reads it. `SandboxResult` is documented as persisting "directly to `sandbox_runs`", but **no `INSERT INTO sandbox_runs` exists anywhere in `backend/src`**. |
| `PROMOTE_PATCH` | Declared tier-2 action type with no executor and no pipeline behind it. |

Consequence for the UI: `/debug` and `/deployments` render correctly but will be
empty in any real deployment.

Grep used to establish this:
`INSERT INTO (remediation_patches|deployment_attempts|sandbox_runs)` over
`backend/src` returns no matches.

---

## 9. Environment adapters

`integrations/runtime.py` provides one typed surface over three backends
(Docker Compose, Kubernetes, ECS). `execution/adapters.py` bridges it to
`execution/ports.py`.

Rules every write obeys:

- it takes an `idempotency_key` and **converges** rather than repeating — asking
  for three replicas when three are running is a recorded no-op, not a scale-up;
- it is never retried inside the module (`attempts=1` on every write call);
- it returns a `WriteResult` naming the exact API call performed.

Read and write method names are declared in disjoint `READ_METHODS` /
`WRITE_METHODS` class variables and a unit test asserts the disjointness.

**No shell.** Everything goes through the Docker Engine API, the Kubernetes API
or the AWS API. Nothing builds a command string.

The bridge does not invent capabilities: an operation the underlying adapter
cannot perform raises an explicit "unsupported" error rather than returning a
successful-looking no-op, which would cause verification to run against an
unchanged system and report a confusing failure.

---

## See also

- [policy.md](policy.md) — the rule engine behind gate 3
- [verification.md](verification.md) — what happens after `execute`
- [evidence.md](evidence.md) — what gate 2 validates against
- [mcp-tools.md](mcp-tools.md) — the single write tool and why it needs a `ValidatedAction`
- [data-model.md](data-model.md) — `remediation_actions`, `approvals`, `resource_leases`
