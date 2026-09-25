# Verification

Proving a remediation worked, without asking a model whether it worked.

Source: `backend/src/aegis/verification/` — `claims.py`, `engine.py`, `store.py`.

---

## 1. The primitive

```
CLAIM + EVIDENCE + TEST + RESULT + TIMESTAMP
```

A remediation is not verified because a command returned zero, and not because a
deployment went green. It is verified when the specific condition that defined
the incident is demonstrably gone, and nothing else broke in the process.

Expressing that as a list of independently testable claims buys three things an
aggregate boolean does not:

- **Disputability.** An operator can disagree with one claim without rejecting
  the whole verdict, and can see exactly which measurement changed their mind.
- **Partial honesty.** Four claims pass and one cannot be measured ⇒
  `PARTIALLY_VERIFIED` with the gap named, rather than rounding up to success.
- **Regression detection.** Protected-metric claims use the same machinery as
  the target metric, so "the error rate fell but latency doubled" is a
  first-class outcome rather than an unnoticed side effect.

### `ClaimTest` — declared before the measurement

```python
@dataclass(frozen=True, slots=True)
class ClaimTest:
    kind: VerificationTestKind
    metric: str | None = None
    resource_id: str | None = None
    direction: MetricDirection | None = None
    threshold: float | None = None
    tolerance: float = 0.0
    window_seconds: int = 300
    spec: dict[str, Any] = field(default_factory=dict)
```

Declaring the test up front is what stops post-hoc rationalisation: the threshold
cannot be chosen after seeing the result.

`VerificationTestKind` is a closed set of ten: `metric_threshold`,
`metric_delta`, `protected_metric`, `health_probe`, `instance_ready`,
`reproduction`, `regression_suite`, `trace_error_rate`, `log_pattern_absent`,
`dependency_health`.

`VerificationClaim` adds `protected: bool` — a "nothing else broke" claim rather
than a goal.

---

## 2. Four claim outcomes

`domain/enums.py :: ClaimOutcome`

| Outcome | Meaning |
|---|---|
| `PASS` | measured, and the test succeeded |
| `FAIL` | measured, and the test failed |
| `INCONCLUSIVE` | measured, but the signal does not decide it |
| `UNAVAILABLE` | **could not be measured at all** |

`UNAVAILABLE` exists for the same reason `EvidenceStatus` has
`SOURCE_UNAVAILABLE`. A metric we could not read is not a metric that came back
healthy. Collapsing the two would let a Prometheus outage read as a successful
remediation — the single most dangerous failure mode a verification system can
have.

---

## 3. Five verdicts, and why an unmeasurable claim can never be VERIFIED

`decide_verdict` is pure, total and order-independent.

```python
def decide_verdict(results: list[ClaimResult]) -> VerificationVerdict:
    if not results:
        return VerificationVerdict.INCONCLUSIVE
    if any(r.is_regression for r in results):
        return VerificationVerdict.REGRESSION_DETECTED
    if any(r.outcome is ClaimOutcome.FAIL for r in results):
        return VerificationVerdict.FAILED
    if all(r.outcome is ClaimOutcome.PASS for r in results):
        return VerificationVerdict.VERIFIED
    goal_results = [r for r in results if not r.claim.protected]
    if goal_results and all(
        r.outcome in (ClaimOutcome.UNAVAILABLE, ClaimOutcome.INCONCLUSIVE)
        for r in goal_results
    ):
        return VerificationVerdict.INCONCLUSIVE
    return VerificationVerdict.PARTIALLY_VERIFIED
```

Precedence, strongest signal first:

| # | Verdict | Condition |
|---|---|---|
| 1 | `REGRESSION_DETECTED` | any **protected** claim failed — even if every goal claim passed |
| 2 | `FAILED` | any goal claim failed |
| 3 | `INCONCLUSIVE` | no claims at all, or every goal claim was unmeasurable |
| 4 | `VERIFIED` | **every** claim passed |
| 5 | `PARTIALLY_VERIFIED` | some passes plus unavailable or inconclusive claims |

The structural consequence: `VERIFIED` requires `all(... is PASS)`. A single
`UNAVAILABLE` claim makes that predicate false, so the run falls through to
`PARTIALLY_VERIFIED` (or `INCONCLUSIVE` if the goal itself was unmeasurable).

**An unmeasurable claim can never reach `VERIFIED`.** There is no branch that
treats absence of a failure signal as evidence of health.

```python
@property
def is_success(self) -> bool:
    """Only a full verification counts as success."""
    return self is VerificationVerdict.VERIFIED
```

`PARTIALLY_VERIFIED` deliberately does not count as success — treating it as a
pass is how an unverified change reaches production wearing a green badge.

`requires_rollback` is true for `FAILED` and `REGRESSION_DETECTED`.

---

## 4. The engine

`verification/engine.py`. Three rules carry the safety weight:

1. **A missing measurement is never a pass.** If Prometheus cannot be reached,
   the claim resolves `UNAVAILABLE` and the run cannot reach `VERIFIED`.
2. **The baseline is captured before the action runs.** Measuring afterwards and
   comparing against a remembered number would silently absorb whatever the
   action changed.
3. **Protected metrics are tested with the same machinery as the goal.**

```python
MIN_SAMPLES_FOR_COMPARISON = 3
```

A single scrape either side of a restart is noise, not a measurement.

### `Baseline`

```python
@dataclass(frozen=True, slots=True)
class Baseline:
    captured_at: datetime
    window_start: float
    window_end: float
    values: dict[str, float] = field(default_factory=dict)
    missing: dict[str, str] = field(default_factory=dict)
```

`missing` lists claims whose baseline could not be read. Those claims can still
be measured afterwards, but no **delta** conclusion is available for them, so
they resolve `INCONCLUSIVE` rather than borrowing a default.

### Sources

Measurements come from `PrometheusClient` (metric kinds), `RuntimeReadPort`
(health probes, instance readiness) and — for reproduction and regression
kinds — sandbox exit codes. Nothing in the module consults a model. Every claim
resolves from a number, an exit code or a probe result.

Each measurement is recorded as an `EvidenceItem` so the verdict is citable.

---

## 5. Persistence

Two tables, on purpose:

- **`verification_runs`** — one row per run: `verdict`, `passed`, `checks`
  (JSONB), `baseline_window`, `observation_window`, `started_at`,
  `completed_at`.
- **`verification_claims`** — one row per claim: `claim`, `test_kind`,
  `test_spec` (JSONB), `outcome` (CHECK-constrained to the four values),
  `before_value`, `after_value`, `threshold`, `evidence_ids`, `detail`.

The split is what lets the incident view show "latency did not regress" as its
own row with its own before/after numbers, instead of a single opaque pass or
fail an operator has to take on trust.

Nothing updates a completed run. A verification is a measurement at a point in
time; re-running produces a new row so the history of what was believed, and
when, stays intact.

`GET /v1/actions/{action_id}` returns the run together with its
`verification_claims` array.

---

## 6. Who calls it

`ExecutionService.execute` captures the baseline before the write, runs the
action, waits the observation window and evaluates the claims. A
`requires_rollback` verdict triggers the rollback path; a failed rollback
escalates loudly rather than being swallowed.

The plan itself comes from `ActionProposal.verification` — a `VerificationPlan`
with a non-empty `target_metric`, which gate 1 of the action chain enforces for
every action type whose profile sets `requires_verification`.

---

## 7. Evaluation note

The benchmark scores `verification_success` and treats `PARTIALLY_VERIFIED` as
**not** verified — asserted by
`tests/unit/test_evaluation_metrics.py`. The scoring path and the runtime path
agree on what "verified" means.

There is deliberately no `no_execution_verification` ablation arm. Post-execution
verification is not a component the workflow can decline: `ExecutionService.execute`
captures a baseline, verifies, then commits or rolls back inside one state machine,
and the verdict is what chooses between those branches. Ablating it would remove the
rollback decision - a safety control, not a measurable contribution.
See [evaluation.md](evaluation.md#6-known-gaps).

---

## See also

- [execution.md](execution.md) — the chain verification sits at the end of
- [evidence.md](evidence.md) — the same "found nothing ≠ could not look" rule
- [observability.md](observability.md) — what happens when Prometheus is down
- [data-model.md](data-model.md) — `verification_runs`, `verification_claims`
