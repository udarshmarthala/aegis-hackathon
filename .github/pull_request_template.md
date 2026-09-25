## What changed and why

<!-- One paragraph. The reviewer should know what problem this solves before
     reading a single line of the diff. -->

## How it was verified

<!-- Commands run, tests added, evidence. "It builds" is not verification.
     ESD section 44: a green happy-path demo is not done. -->

- [ ] `make test` (or the relevant subset) passes locally
- [ ] `make lint` and `make typecheck` pass
- [ ] New behaviour has tests that fail without the change

---

## Architecture invariants (CLAUDE.md section 3)

Tick every line. If a line does not apply, tick it and write `n/a` beside it.
A line you cannot tick is a blocking discussion, not a nit.

- [ ] **1. The LLM proposes; deterministic code decides.** No model output is
      the final authority for authn, authz, policy, risk tier, action
      permission, idempotency, locking, rollback state, or evaluation
      pass/fail.
- [ ] **2. No claim without evidence.** Every material assertion carries
      validated `EvidenceRef` IDs; the validator still rejects a diagnosis with
      no evidence references.
- [ ] **3. Abstention is a first-class outcome.** Nothing here converts
      "unknown / insufficient evidence" into a confident answer.
- [ ] **4. Read broadly, write narrowly.** Any new write passes the full gate
      chain: schema → evidence → policy → authz → lease → execute → verify →
      commit/rollback.
- [ ] **5. Fail closed.** Missing config, unknown action type, unreachable
      policy store or expired approval results in deny, never allow.
- [ ] **6. "No evidence found" is not "source unavailable."** The two states
      stay distinct through the API and into the UI.
- [ ] **7. Untrusted text is data, never instruction.** Logs, commit messages,
      alert payloads and ticket bodies stay inside `UntrustedText` envelopes.
      No prompt content can grant a permission.
- [ ] **8. Agents cannot raise their own budgets.** Budget enforcement stays in
      the supervisor, outside agent reach.
- [ ] **9. Observability is not a control-plane dependency.** LangSmith,
      Prometheus or Neo4j being down degrades confidence and records an
      evidence gap; it never halts a workflow.
- [ ] **10. Postgres is the system of record.** Neo4j is topology. Redis is
      never authoritative.

## Reliability (CLAUDE.md section 4)

- [ ] Every new external call has an explicit timeout, a bounded retry with
      jitter, and a circuit breaker. No unbounded `await`.
- [ ] Every retry target is idempotent. No non-idempotent write is auto-retried.
- [ ] Every new queue, cache and in-memory collection is bounded.
- [ ] DB access goes through the pool; transactions have a statement timeout.
- [ ] No blocking work on the event loop.
- [ ] Errors are typed (`aegis.core.errors`). No bare `except:`, no silent `pass`.
- [ ] Logs are structured JSON with `incident_id` / `correlation_id` and contain
      no secrets, tokens or raw service-account material.

## Security and secrets

- [ ] No credential, token, private key or service-account material is added to
      the repository, to Terraform state, or to a log line.
- [ ] New IAM permissions are least-privilege and scoped to specific resources.
- [ ] Any new secret is read from Secrets Manager / SSM at runtime, not baked
      into an image or a task definition environment variable.

## Data and migrations

- [ ] Migrations are additive and backward compatible with the previously
      deployed image. A rollback does not revert migrations.
- [ ] No existing migration file was edited (the runner rejects a changed
      checksum).

## Infrastructure (only if `infra/terraform/**` changed)

- [ ] `terraform fmt -recursive` is clean and `terraform validate` passes.
- [ ] The plan posted on this PR contains no `delete` or `replace` action, or
      the destruction is explained here and expected.
- [ ] Cost impact is stated below.

**Cost impact:** <!-- e.g. "+$12/mo: one extra interface endpoint" or "none" -->

## Review routing (CLAUDE.md section 2)

- [ ] `backend/**/*.py` → `ecc:python-reviewer`
- [ ] `frontend/**/*.tsx` → `ecc:react-reviewer`, `ecc:a11y-architect`
- [ ] auth / policy / ingestion → `ecc:security-reviewer`
- [ ] SQL / migrations / schema → `ecc:database-reviewer`
- [ ] error-handling paths → `ecc:silent-failure-hunter`
