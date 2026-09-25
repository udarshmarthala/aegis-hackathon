# Testing

860 tests: 849 unit, 11 integration. The unit suite needs no infrastructure at
all; the integration suite skips rather than fails when there is none.

Source: `backend/tests/`.

---

## 1. Running them

```bash
cd C:\dev\aegis-2.0\backend

.venv/Scripts/python.exe -m pytest tests -q              # everything
.venv/Scripts/python.exe -m pytest tests/unit -q         # 849, no infra needed
.venv/Scripts/python.exe -m pytest tests/integration -q  # 11, needs Postgres
.venv/Scripts/python.exe -m pytest tests -m "not integration" -q
```

`pyproject.toml`:

```toml
[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]
addopts = "-q --strict-markers"
markers = [
  "integration: requires live infrastructure",
  "chaos: dependency-failure simulation",
]
```

`--strict-markers` means a typo'd marker is an error, not a silently unmarked
test.

Lint and types:

```bash
cd backend && .venv/Scripts/python.exe -m ruff check src tests
cd backend && .venv/Scripts/python.exe -m mypy src
```

Or via the Makefile from the repo root: `make test`, `make lint`,
`make typecheck` (these use a bare `python`, so activate the venv first).

---

## 2. Why integration tests skip instead of failing

`backend/tests/conftest.py`:

> Unit tests must run with no infrastructure at all — that is what makes them
> usable in a pre-commit hook and in a pull request from a laptop on a train.
> Integration tests need real datastores, so they are marked and skipped rather
> than failed when those are absent: **a skipped integration test is honest,
> while a failing one on a machine with no Postgres is noise that trains people
> to ignore a red build.**

```python
database = Database(settings)
try:
    await database.connect()
except Exception:  # absent infra is a skip, not a failure
    pytest.skip(f"postgres unavailable at {host}:{port}: {exc}")
```

In CI the service containers are guaranteed, so the same tests fail loudly there.

### Connection defaults

```python
DEFAULT_TEST_PG_PORT = int(os.getenv("AEGIS_TEST_PG_PORT", "55433"))
DEFAULT_TEST_PG_HOST = os.getenv("AEGIS_TEST_PG_HOST", "localhost")
```

55433 matches the compose publish port. Also overridable:
`AEGIS_TEST_NEO4J_URI`, `AEGIS_TEST_PROMETHEUS_URL`, `AEGIS_TEST_REDIS_HOST`.
`otel_traces_enabled` is forced off.

---

## 3. What unit tests cover

28 modules under `backend/tests/unit/`. They document the guarantees.

| File | Guarantee it pins down |
|---|---|
| `test_execution_gate.py` | `ValidatedAction` cannot be constructed outside `ActionGate`; each of the five gates rejects what it should |
| `test_execution_service.py` | lease always released; authorisation re-checked before the write; rollback on failure; no auto-retry of non-idempotent actions |
| `test_policy_engine.py` | every one of the 12 rules, in isolation and in combination; BLOCK beats REQUIRE_HUMAN; default-deny |
| `test_mcp_invoker.py` | a write tool is refused without a `ValidatedAction`, and the action is re-checked for liveness before the handler runs |
| `test_mcp_registry.py` | read and write sets are disjoint; `permitted_for` fails closed |
| `test_mcp_tools.py` / `test_mcp_wiring.py` | every tool's schema, scope and output contract |
| `test_memory_contamination.py` | an abstained, unverified or unapproved outcome cannot become a memory |
| `test_graph_ontology.py` | label/rel-type injection attempts raise `ValidationError` |
| `test_graph_traversal.py` | depth clamping; `SourceUnavailable` propagates rather than becoming an empty list |
| `test_retrieval_fusion.py` | RRF as a pure function |
| `test_retrieval_chunking.py` | chunk boundaries, overlap, line numbers, dedup |
| `test_retrieval_embeddings.py` | unconfigured provider reports, never returns zero vectors |
| `test_retrieval_localization.py` | the narrowing stages and their caps |
| `test_evaluation_scenarios.py` | per-file: every scenario parses, is filed in the right category, and leaks no ground truth |
| `test_evaluation_harness.py` | the SUT never receives ground truth; the fault spec goes only to the environment; one test per failure class; safety outranks correctness |
| `test_evaluation_metrics.py` | every evaluator's arithmetic; the judge fence; `None` ≠ 0 |
| `test_integrations_*.py` | GitHub, Slack, LangSmith, Loki, Tempo, runtime — read/write method disjointness, degradation behaviour |
| `test_container_graph_capability.py` | a missing optional capability degrades rather than fails |
| `test_workflow_tools.py` | workflow nodes reach the world only through the tool boundary |
| `test_api_evaluation.py` | the evaluation router's contract |

A few that are worth reading as specifications in their own right:

- `test_the_system_under_test_never_receives_ground_truth` — asserts the payload
  has no `ground_truth`/`fault` key, that the scenario id appears nowhere in it,
  and that the alert object has no `root_cause_service` attribute at all;
- `test_safety_outranks_correctness_in_classification` — a perfect diagnosis that
  executed an unapproved tier-2 action is a failure;
- `test_unvalidated_citations_are_unmeasured_not_unsupported` — no validator
  yields `None`, not `0.0`;
- `test_undefined_quantities_are_none_not_zero`.

---

## 4. What integration tests cover

One file, 11 tests: `backend/tests/integration/test_persistence_integration.py`.

> The unit suite proves the logic; these prove the assumptions it rests on. Every
> test here would pass against a mock and still be wrong in production, because
> what is being checked is **the database's own behaviour**: does the partial
> unique index actually reject a second live lease, does `ON CONFLICT` actually
> return the original row, does a conditional UPDATE actually lose a race.
>
> Those are the guarantees the safety model is built on. If Postgres does not
> behave as the schema claims, no amount of application logic saves us.

Concretely: `resource_leases_active_uniq` rejecting a concurrent acquire with
`LeaseConflict`; `remediation_actions.idempotency_key` returning the original
row on a retried proposal; `approvals_one_open_idx` permitting one open approval;
conditional state transitions losing a race rather than clobbering.

Note what is **not** covered: there are no integration tests for the API, the
workflow end to end, Neo4j, Redis or the sandbox. The 11 tests are narrowly about
Postgres semantics. `docs/aws-architecture.md` § "Known gaps" records the same
point.

---

## 5. The frontend

`frontend/package.json` has five scripts:

```
dev        next dev
build      next build
start      next start
lint       next lint
typecheck  tsc --noEmit
```

**There is no `test` script and no test runner in the dependencies.** The
frontend is verified by `next build` (22 routes) plus `tsc --noEmit` plus
`next lint`.

Two things to know about the build:

- `next.config.mjs` sets `eslint: { ignoreDuringBuilds: true }`, so
  `npm run build` passing does **not** imply lint passing — run `npm run lint`
  separately;
- `eslint.config.mjs` enables `@typescript-eslint/no-explicit-any: 'error'`,
  `no-unused-vars: 'error'` and `no-console` (allowing `warn`/`error`). Its
  header notes the repo previously had no ESLint config, so `npm run lint`
  dropped into Next's interactive setup and never actually ran.

---

## 6. Definition of done

From `ESD.md` §44, restated because it is the bar:

> Typed interfaces · defined failure behaviour · enforced permissions · emitted
> metrics · visible traces · evaluation coverage · passing tests · working
> replay/audit · unsafe paths structurally impossible.

A green happy-path demo is not done.

One honest note against that list: **"emitted metrics" is not met for the control
plane itself.** There is no `/metrics` endpoint on the API — no `prometheus_client`
registry, no `make_asgi_app`, no instrumentator anywhere in `backend/src`. The
`aegis-api` scrape job in `infra/prometheus/prometheus.yml` targets
`api:8000/metrics` and will always fail. Aegis *consumes* Prometheus as an
evidence source; it does not currently *expose* metrics about itself. Tracing
(OTel) and structured logs are wired.

---

## 7. CI

Seven workflows in `.github/workflows/`: `ci-backend.yml`, `ci-frontend.yml`,
`ci-security.yml`, `ci-terraform.yml`, `deploy-staging.yml`,
`deploy-production.yml`, `docker.yml`. See [cicd.md](cicd.md) — that document is
owned by another author; read it, do not duplicate it here.

---

## See also

- [evaluation.md](evaluation.md) — the benchmark, which is a different thing from tests
- [local-development.md](local-development.md) — bringing up the infra the integration suite wants
- [cicd.md](cicd.md) — pipelines and rollback
