# Policy

The deterministic layer that stands between a model's output and a production
write.

Source: `backend/src/aegis/policy/` — `tiers.py`, `engine.py`, `context.py`,
`killswitch.py`, `store.py`.

---

## 1. Risk tier is a property of the action type

`policy/tiers.py` holds one static table. Tier is resolved from it alone — never
from model output, never from the proposal's reason text, never from how
confident an agent sounds. That is the mitigation for an agent talking a
dangerous action into a lower class.

```python
assert set(_PROFILES) == set(ActionType), "every ActionType needs a risk profile"
```

A missing profile is a hard failure at import, not a permissive default.

### The complete table

`ActionProfile` carries `tier`, `idempotent`, `reversible`, `max_blast_radius`,
`min_confidence`, `requires_verification`.

| Action type | Tier | Idem. | Rev. | Max blast | Min conf. | Verify? |
|---|---|---|---|---|---|---|
| `restart_instance` | 1 LOW | ✓ | ✓ | 1 | 0.70 | ✓ |
| `rerun_health_check` | 1 LOW | ✓ | ✓ | 1 | 0.50 | — |
| `scale_up_bounded` | 1 LOW | ✓ | ✓ | 2 | 0.75 | ✓ |
| `clear_cache_key` | 1 LOW | ✓ | ✗ | 1 | 0.75 | ✓ |
| `rollback_deployment` | 2 APPROVAL | ✓ | ✓ | 10 | 0.80 | ✓ |
| `scale_service` | 2 APPROVAL | ✓ | ✓ | 10 | 0.75 | ✓ |
| `update_config` | 2 APPROVAL | ✓ | ✓ | 10 | 0.85 | ✓ |
| `promote_patch` | 2 APPROVAL | ✗ | ✓ | 10 | 0.90 | ✓ |
| `drain_instance` | 2 APPROVAL | ✓ | ✓ | 5 | 0.80 | ✓ |
| `delete_data` | 3 HUMAN_ONLY | ✗ | ✗ | 0 | 1.0 | ✓ |
| `rotate_secret` | 3 HUMAN_ONLY | ✗ | ✗ | 0 | 1.0 | ✓ |
| `run_migration` | 3 HUMAN_ONLY | ✗ | ✗ | 0 | 1.0 | ✓ |
| `modify_security_policy` | 3 HUMAN_ONLY | ✗ | ✗ | 0 | 1.0 | ✓ |

`clear_cache_key` is marked non-reversible (a cleared key cannot be restored)
but is still tier 1 because the value is rebuildable.

Tier 0 (`RiskTier.OBSERVE`) exists in the enum for read-only classification; no
`ActionType` carries it.

Changing a value in this table is a security-relevant change.

---

## 2. `decide` is a pure function

`policy/engine.py`. Same context in, same decision out, forever. No I/O, no
model, no global state. Three operational consequences:

- every decision is replayable from persisted state during an audit;
- every rule is exhaustively unit-testable without infrastructure;
- an agent can influence the outcome only by supplying different **facts**,
  never by arguing.

### The twelve rules, in order

| # | Rule | Effect | Fires when |
|---|---|---|---|
| 1 | `kill_switch` | BLOCK | any engaged switch matches the environment, action type or service |
| 2 | `tier_three` | BLOCK | the action type is tier 3 |
| 3 | `concurrency` | BLOCK | another action holds the lease on the resource |
| 4 | `rate_limit` | BLOCK | `actions_last_hour >= max_actions_per_hour` |
| 5 | `abstained_diagnosis` | REQUIRE_HUMAN | the diagnosis abstained |
| 6 | `autonomy_disabled` | REQUIRE_HUMAN | autonomy is off, or the tier is not in the allowlist |
| 7 | `evidence_quality` | REQUIRE_HUMAN | quality below the tier floor, **or zero Tier-A evidence** |
| 8 | `confidence_floor` | REQUIRE_HUMAN | `diagnosis_confidence < profile.min_confidence` |
| 9 | `blast_radius` | REQUIRE_HUMAN | size exceeds the profile cap, or customer-facing in production |
| 10 | `rollback_required` | REQUIRE_HUMAN | no rollback or compensating action defined |
| 11 | `verification_required` | REQUIRE_HUMAN | the profile requires verification and none was supplied |
| 12 | `production_allowlist` | REQUIRE_HUMAN | in production and the service is not allowlisted |

Evidence-quality floors by tier:

| Tier | Minimum `evidence_quality` |
|---|---|
| 0 OBSERVE | 0.00 |
| 1 LOW | 0.45 |
| 2 APPROVAL | 0.60 |
| 3 HUMAN_ONLY | 1.00 |

### Every rule runs, even after the first failure

```python
for rule in _RULES:
    outcome = rule(ctx)
    ...
    if decisive is None or (
        outcome.effect is PolicyEffect.BLOCK
        and decisive.effect is not PolicyEffect.BLOCK
    ):
        decisive = outcome
```

`BLOCK` is strictly stronger than `REQUIRE_HUMAN` and wins outright. The
persisted decision records **every** reason an action was held back, not only the
first, so an operator who clears one blocker learns immediately about the next
rather than discovering them one deploy at a time.

### The default is deny

If no rule is decisive, the effect is `ALLOW` with `matched_rule="all_gates_passed"`.
Reaching that point requires every one of the twelve to have passed — a rule set
that fails to match cannot accidentally permit an action, because rules 6, 10 and
11 all fire on absence rather than on presence.

`POLICY_VERSION = "1.0.0"` is stamped on every decision and stored in
`policy_decisions.policy_version`.

---

## 3. Kill switches fail closed

`policy/killswitch.py`. Four independent scopes — global, environment, action
type, service. The effective state is the OR of all of them.

```python
@classmethod
def fail_closed(cls, reason: str) -> KillSwitchState:
    """Used when the policy store cannot be read."""
    return cls(global_engaged=True, degraded=True, reason=reason)
```

An unreadable `kill_switches` table counts as **every switch engaged**. If Aegis
cannot prove autonomy is enabled, autonomy is off. `PolicyStore.load_kill_switches`
returns `KillSwitchState.fail_closed(...)` on any query failure.

`KillSwitchState` is a frozen snapshot loaded once per decision. Snapshotting
matters: a switch flipping mid-decision must not produce a decision derived from
two different worlds.

Absence of a row means "not engaged"; the table is only ever populated with
engaged switches. Operators toggle the global switch from `/policies` in the
console, or `POST`/`DELETE /v1/policy/kill-switch`.

---

## 4. Autonomy configuration

From `core/config.py`, with defaults:

| Setting | Default | Meaning |
|---|---|---|
| `AUTONOMY_ENABLED` | `false` | master switch |
| `AUTONOMY_MODE` | `guarded` | `off` \| `guarded` \| `supervised` |
| `AUTONOMY_ALLOWED_TIERS` | `"1"` | comma-separated tiers eligible for autonomous execution |
| `AUTONOMY_MAX_ACTIONS_PER_HOUR` | `10` | per environment |
| `APPROVAL_TTL_SECONDS` | `900` | |
| `RESOURCE_LEASE_TTL_SECONDS` | `300` | |

```python
@property
def allowed_tiers(self) -> frozenset[int]:
    if not self.autonomy_enabled or self.autonomy_mode is AutonomyMode.OFF:
        return frozenset()
    ...
```

Autonomy off ⇒ empty allowlist ⇒ rule 6 fires ⇒ every write requires a human.
That is the fail-closed default and it is what ships.

The rate-limit counter is backed by a partial index so it costs no table scan:

```sql
CREATE INDEX actions_executed_recent_idx
    ON remediation_actions (environment, executed_at DESC)
    WHERE executed_at IS NOT NULL;
```

---

## 5. Known limitation: the production allowlist is empty

Rule 12 tests `ctx.service_id not in ctx.service_allowlist`. The only production
construction site is `ActionGate._build_context`:

```python
service_allowlist=frozenset(),
```

It is hard-coded empty and there is no configuration source that populates it.
The consequence, stated plainly:

> **In an environment where `AEGIS_ENV=production`, any proposal carrying a
> `service_id` matches `service_not_allowlisted` and becomes `REQUIRE_HUMAN`.
> Autonomous remediation in production is currently impossible.**

This fails safe, and it is the correct direction to be wrong in, but it means the
"autonomous tier-1 remediation in production" capability is not exercisable
today. The allowlist is populated only in `tests/unit/test_policy_engine.py`.

---

## 6. What the engine never sees

`PolicyContext` (`policy/context.py`) is an explicit, closed struct. It carries
numbers and enums — evidence counts, blast radius size, confidence, kill-switch
state, lease state, wall-clock time. It does not carry the proposal's `reason`,
the diagnosis text, or any free-form model output.

`ActionGate._build_context` reads from persisted state (`load_kill_switches`,
`autonomous_actions_last_hour`, `leases.is_held`) rather than trusting the
caller, wherever that is possible.

`verification_passed` is always `False` at gate time — verification has not
happened yet. It exists on the context for the post-execution re-evaluation path.

---

## 7. Auditability

Every decision writes two rows:

- `policy_decisions` — effect, risk tier, matched rule, all reasons, per-gate
  results, policy version and the full `context_snapshot`;
- `audit_log` — `ACTION_POLICY_DECIDED` with `actor="system:policy_engine"`,
  `actor_type="system"`, correlated by `correlation_id`.

`ACTION_BLOCKED`, `ACTION_VALIDATED` and `ACTION_PROPOSED` are separate audit
event types, so "a model suggested this", "policy allowed it" and "a human
authorised it" are three distinguishable facts.

---

## See also

- [execution.md](execution.md) — where `decide` is called from
- [evidence.md](evidence.md) — how `evidence_quality` and `tier_a_evidence_count` are derived
- [data-model.md](data-model.md) — `policy_decisions`, `kill_switches`, `approvals`
- [testing.md](testing.md) — `tests/unit/test_policy_engine.py` covers every rule
