# Operator runbooks

For operating **Aegis itself**. For what Aegis does to the systems it watches,
see [agents.md](agents.md) and [execution.md](execution.md).

Every procedure here uses commands that exist in this repository.

---

## 1. Stop all autonomous action, now

The global kill switch fails closed and takes effect on the **next** policy
decision — it does not interrupt an action already executing.

**Console:** `/policies` → global kill switch toggle.

**API:**

```bash
curl -X POST http://localhost:8000/v1/policy/kill-switch \
  -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"scope":"global","target":"","reason":"incident 1234"}'
```

Scopes: `global`, `environment`, `action_type`, `service`. Any engaged switch
blocks; the effective state is the OR of all of them.

**Release:** `DELETE /v1/policy/kill-switch` with the same scope and target, or
the console toggle.

**Verify:** `GET /v1/policy/kill-switch` lists what is engaged, and
`GET /v1/policy` shows the autonomy posture as actually configured.

**Belt and braces:** set `AUTONOMY_ENABLED=false` in `.env` and restart the
worker. `Settings.allowed_tiers` then returns an empty set and policy rule 6
requires a human for every write.

An in-flight action holds a lease for at most `RESOURCE_LEASE_TTL_SECONDS`
(300 s). To stop the worker entirely: `docker compose ... stop worker` — it
drains in-flight jobs within `stop_grace_period: 45s`.

---

## 2. An action is stuck awaiting approval

```bash
curl -s http://localhost:8000/v1/approvals -H "Authorization: Bearer $TOKEN"
```

Or the `/approvals` page, which orders by time remaining and renders every
request in full.

Three outcomes: `approved`, `rejected`, `more_evidence`.

```bash
curl -X POST http://localhost:8000/v1/approvals/$APPROVAL_ID/decide \
  -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"decision":"approved","note":"verified the blast radius"}'
```

Requires the `approver` role. If the approval has expired the decision is
refused — that is correct. Re-queue the investigation
(`POST /v1/incidents/{id}/reinvestigate`) so a fresh proposal is gated against
the system as it is now, rather than reviving a decision made against a system
that no longer exists.

Waiting for a human does not hold a worker: the graph ends at `finalize` and the
approval enqueues fresh work when it lands.

---

## 3. A resource lease is stuck

Symptom: every proposal for one resource is blocked with `resource_locked`.

```sql
SELECT id, resource_type, resource_id, holder, incident_id, acquired_at, expires_at
  FROM resource_leases
 WHERE released_at IS NULL
 ORDER BY acquired_at;
```

A lease expires on its own after `RESOURCE_LEASE_TTL_SECONDS` and the next
acquire reaps it in the same transaction. **Do not manually delete a live lease**
unless you have confirmed the holder is dead — that is the one operation that can
put two writers on one resource.

Expiry frees the lock. It is deliberately **not** a statement that the resource is
in a good state; whether the original action left it half-changed is a separate
question, answered by the verification run.

`GET /v1/actions/{action_id}/leases` shows concurrency state for one action's
target.

---

## 4. Investigations are not starting

Check the queue:

```sql
SELECT status, count(*) FROM workflow_jobs GROUP BY status;
SELECT id, incident_id, kind, status, attempts, max_attempts, locked_by, last_error
  FROM workflow_jobs
 WHERE status IN ('queued','running')
 ORDER BY created_at;
```

| Observation | Meaning |
|---|---|
| rows `queued`, none `running` | no worker is claiming. Check `docker compose ps worker` and its logs. |
| rows `running` with a stale `locked_at` | the holder died; the worker's reaper requeues them. |
| `attempts >= max_attempts`, status `failed` | parked deliberately to avoid a poison loop. Read `last_error`. |
| nothing at all | ingestion is not creating jobs — check the API logs for the alert POST. |

At most one active job exists per `(incident_id, kind)`, enforced by
`workflow_jobs_one_active_idx`. A second `reinvestigate` while one is running is
a no-op by design.

**The worker reads Postgres, not SQS.** If you are looking at an SQS queue depth
metric in AWS, it will be zero and that is expected.

---

## 5. Confidence is low and nothing is proposed

This is usually correct behaviour, not a fault. Work down the chain:

```bash
curl -s http://localhost:8000/v1/investigations/$INCIDENT_ID/gaps \
  -H "Authorization: Bearer $TOKEN"
```

Or the `/investigations/[incidentId]` page, `EvidenceGapPanel`.

| Gap | Effect |
|---|---|
| `prometheus` unavailable | **no Tier-A evidence** ⇒ policy rule 7 refuses any autonomous action |
| `graph` unavailable | blast radius from telemetry alone; `gap_ratio` rises |
| `github` unavailable | no commit or deployment correlation |
| `embeddings` unconfigured | retrieval runs lexical-only and reports `degraded=True` |
| memory empty | no precedent to recall |

Then `GET /health` for the component picture, and `/settings` for the capability
list with reasons.

An abstained diagnosis is a first-class outcome. `has_abstained_diagnosis` makes
policy rule 5 require a human — acting on an explicit "we do not know" is never
autonomous.

---

## 6. Verification came back PARTIALLY_VERIFIED

That is not a pass, and it is not a bug.

```bash
curl -s http://localhost:8000/v1/actions/$ACTION_ID -H "Authorization: Bearer $TOKEN"
```

The response carries `verification_claims` with per-claim `outcome`,
`before_value`, `after_value` and `threshold`. Look for `UNAVAILABLE`:

- `UNAVAILABLE` ⇒ the measurement could not be taken. Fix the source and re-run;
  do not infer success from the absence of a failure signal.
- `INCONCLUSIVE` ⇒ measured, but the signal does not decide it. Often a missing
  baseline (fewer than `MIN_SAMPLES_FOR_COMPARISON = 3` samples).

`REGRESSION_DETECTED` means a **protected** metric failed even though the goal
metric improved. Treat it as a failed remediation; `requires_rollback` is true.

---

## 7. A dependency is down

`GET /health` classifies every component:

```json
{"postgres": {"status": "...", "hard_dependency": true, "affects": ["everything"]}}
```

| Down | Impact |
|---|---|
| **Postgres** | the only hard dependency. `/health/ready` returns 503; the API should be restarted or the database restored. Nothing else matters until it is back. |
| Neo4j | topology evidence gap; blast radius degraded. Investigations continue. |
| Redis | live SSE streaming stops; pages fall back to polling. No correctness impact. |
| Prometheus | metric evidence gap, **and verification claims resolve `UNAVAILABLE`**. No autonomous action is possible. |
| Tempo / Loki | trace and log evidence gaps. |
| LangSmith | trace view only. Explicitly never a control-plane dependency. |
| GitHub / Slack | change analysis and notification degrade to evidence gaps. |

Circuit-breaker state is at `/settings` (`CircuitBreakers`) and in
`core.resilience.breaker_states()`.

---

## 8. Following one incident across every system

Everything is correlated by one id.

```bash
curl -i http://localhost:8000/v1/incidents/$ID -H "Authorization: Bearer $TOKEN"
# → X-Correlation-ID: <cid>
```

Then:

| System | Lookup |
|---|---|
| structured logs | `correlation_id=<cid>` — every line in the request carries it |
| audit | `GET /v1/audit/incident/$ID`, or `SELECT * FROM audit_log WHERE correlation_id = '<cid>'` (dedicated index) |
| agent runs | `GET /v1/investigations/$ID/runs` |
| tool calls | `GET /v1/investigations/$ID/tools` |
| evidence gaps | `GET /v1/investigations/$ID/gaps` |
| OTel trace | the same correlation id |
| LangSmith | `agent_runs.langsmith_run_id` |

You can also pass your own: send `X-Correlation-ID` on the request and it is
echoed and bound throughout.

---

## 9. Reviewing what an autonomous action actually did

```bash
curl -s "http://localhost:8000/v1/actions?state=SUCCESS" -H "Authorization: Bearer $TOKEN"
curl -s http://localhost:8000/v1/actions/$ACTION_ID    -H "Authorization: Bearer $TOKEN"
```

The detail response carries the full decision trail: the policy decision with
every gate result and the `context_snapshot`, the approval (if any) with its
decider, the execution report, and the verification run with its claims.

The question "did a person authorise this?" is answered by
`audit_log.actor_type = 'human'` — no code path in Aegis writes a human approval
on behalf of a model.

To replay a policy decision: `policy_decisions.context_snapshot` is a complete,
replayable record of exactly what the engine saw. Feeding it back through
`policy.engine.decide` must yield the same effect, because `decide` is a pure
function.

---

## 10. Rotating a credential

**Never** put a secret in a proposal, a prompt or a log. The log redaction
processor runs last and masks secret-shaped values, but that is a backstop, not a
policy.

`rotate_secret` is a **tier-3** action type. It is representable so policy can
name and block it, and `execution/registry.py` registers no executor for it.
There is nothing to call. Rotate credentials by your normal out-of-band process,
then restart the API and worker so `Settings` is re-read.

The sandbox refuses any environment variable whose name contains `SECRET`,
`TOKEN`, `PASSWORD`, `KEY` or `CREDENTIAL`, and inherits nothing from the host.

---

## 11. Schema changes

Migrations are **forward-only**. Editing an applied file is a hard error:

> `migration {name} changed after it was applied; add a new migration instead of
> editing history`

Add `backend/migrations/010_*.sql`, make it idempotent (`IF NOT EXISTS`,
`ADD COLUMN IF NOT EXISTS`), and let the API or worker apply it at boot under the
advisory lock. There is no down-migration path and no rollback of a migration —
write the compensating change as a new file.

---

## 12. Clearing a stuck fault in the local workload

```bash
curl -X DELETE http://localhost:8080/admin/fault
curl -s      http://localhost:8080/health   # {"status":"ok","service":"gateway","fault":"none"}
```

Only `gateway` publishes a host port; reach `checkout` and `payment` with
`docker compose ... exec`.

A benchmark run clears its faults in `_finish`, including on the timeout and
error paths, and a cleanup failure logs rather than failing the scenario. If a
suite was killed hard, clear the fault manually.

---

## 13. Things that will look broken and are not

| Symptom | Reality |
|---|---|
| Prometheus target `aegis-api` is down | the API exposes no `/metrics` endpoint ([observability.md](observability.md#4-known-gap-aegis-exposes-no-prometheus-metrics)) |
| `/debug` and `/deployments` are empty | nothing writes `sandbox_runs`, `remediation_patches` or `deployment_attempts` ([execution.md](execution.md#8-what-is-not-implemented)) |
| SQS queue depth is always zero | the worker consumes from Postgres |
| the stdio MCP server reports unavailable | the optional `mcp` package is not installed; the internal tool boundary is unaffected ([mcp-tools.md](mcp-tools.md#6-the-stdio-mcp-server)) |
| production never acts autonomously | the service allowlist is hard-coded empty ([policy.md](policy.md#5-known-limitation-the-production-allowlist-is-empty)) |
| `no_verifier` scores identically to `full` | that ablation is a no-op ([evaluation.md](evaluation.md#6-known-gaps)) |

---

## See also

- [local-development.md](local-development.md) · [observability.md](observability.md)
- [policy.md](policy.md) · [execution.md](execution.md) · [verification.md](verification.md)
- [cicd.md](cicd.md) — deploy and rollback procedures for AWS
