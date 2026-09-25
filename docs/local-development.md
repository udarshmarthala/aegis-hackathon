# Local development

Verified commands for the reference environment: Windows 11, Docker Desktop,
Python 3.12 in `backend/.venv`, Node 20 for the frontend.

---

## 1. Prerequisites

- Docker Desktop (the sandbox also needs the Docker socket)
- Python 3.12 — the repo's virtualenv lives at `backend/.venv`
- Node 20 and npm, for the frontend
- A `.env` file at the repo root. Copy `.env.example` and fill it.

---

## 2. The `.env` file is mandatory, and so is `--env-file`

Compose does **not** pick up `.env` automatically here, because the compose file
lives at `infra/docker/docker-compose.yml` and Docker resolves `.env` relative to
the compose file, not the repo root. Every invocation therefore needs
`--env-file .env` explicitly. The Makefile bakes it in:

```make
COMPOSE := docker compose -f infra/docker/docker-compose.yml \
           --env-file .env --project-name aegis-2-0
```

Four variables have `:?` guards and will stop compose dead if unset:
`POSTGRES_PASSWORD`, `NEO4J_PASSWORD`, `ALERT_INGEST_TOKEN` (and
`INTERNAL_SIGNING_KEY` is validated by the app at boot).

Separately, the `api` and `worker` services carry **both** `env_file: ../../.env`
and an explicit `environment:` block. That is deliberate: a hand-curated env list
had previously dropped `GITHUB_TOKEN` and `LLM_REQUEST_TIMEOUT_S`, so the whole
file is loaded and the explicit block only overrides container-specific values.

---

## 3. Bringing up the stack

```bash
make up      # docker compose ... up -d --build
make logs    # tail everything
make ps
make down    # stop, keep volumes
```

Or directly, with the observability and workload profiles:

```bash
docker compose -f infra/docker/docker-compose.yml --env-file .env \
  --project-name aegis-2-0 \
  --profile observability --profile workload up -d --build
```

### Profiles

| Profile | Services |
|---|---|
| *(default — no flag)* | `postgres`, `redis`, `neo4j`, `api`, `worker`, `web` |
| `observability` | `otel-collector`, `prometheus`, `tempo`, `loki` |
| `workload` | `gateway`, `checkout`, `payment`, `loadgen` |

There is **no `core` profile**. The default set already is the core stack.

Compose forbids `.` in project names, so the project is `aegis-2-0` while the
images are tagged `aegis-2.0-backend`, `aegis-2.0-web` and
`aegis-2.0-workload`.

### Published ports

| Service | Host port | Env override |
|---|---|---|
| Web UI | 3000 | `WEB_PUBLISH_PORT` |
| API | 8000 | `API_PUBLISH_PORT` |
| **Postgres** | **55433** | `POSTGRES_PUBLISH_PORT` |
| Redis | 6379 | `REDIS_PUBLISH_PORT` |
| Neo4j HTTP / Bolt | 7474 / 7687 | `NEO4J_HTTP_PORT` / `NEO4J_BOLT_PORT` |
| Prometheus | 9090 | `PROMETHEUS_PUBLISH_PORT` |
| Tempo | 3200 | fixed |
| Loki | 3100 | fixed |
| OTel collector | 4317 / 4318 | fixed |
| Workload gateway | 8080 | `GATEWAY_PUBLISH_PORT` |

### Why Postgres is on 55433

Both obvious alternatives were tried and rejected:

- **5432** — a natively installed PostgreSQL commonly owns it, and its listener
  wins over Docker's port proxy, so connections silently land on the wrong
  database;
- **55432** — found held by an unrelated project on the reference machine.

55433 is the value that actually works. Containers still reach each other on
5432 over the compose network; only the host publish port differs. The test
suite's default matches (`AEGIS_TEST_PG_PORT`, default `55433`).

### Surfaces

| Surface | URL |
|---|---|
| Web UI | http://localhost:3000 |
| API docs | http://localhost:8000/docs |
| Health | http://localhost:8000/health |
| Liveness / readiness | `/health/live`, `/health/ready` |
| Prometheus | http://localhost:9090 |
| Neo4j browser | http://localhost:7474 |

Only **Postgres** is a hard dependency. Everything else degrades into a recorded
evidence gap that lowers confidence; it never takes the platform down.
`GET /health` reports each component with a `hard_dependency` flag and an
`affects` list.

---

## 4. Signing in

`frontend/src/lib/auth.ts` supports three modes:

| Mode | Available when |
|---|---|
| `firebase` | `NEXT_PUBLIC_FIREBASE_API_KEY`, `..._AUTH_DOMAIN` and `..._PROJECT_ID` are all set |
| `local` | `NEXT_PUBLIC_AEGIS_ENV === 'local'` |
| `unconfigured` | neither |

For local work, set `AUTH_DEV_MODE=true` and `AUTH_DEV_BYPASS_TOKEN=<something>`
in `.env`, then paste that token into the dev-token field on the landing page.
The token is never bundled into the page, and `signInWithDevToken` throws unless
the environment is local.

Roles are hierarchical: `viewer` ⊂ `responder` ⊂ `approver` ⊂ `admin`. An unknown
role grants nothing.

---

## 5. Running a sample incident end to end

```bash
# 1. inject a fault into the reference workload (never into Aegis)
curl -X POST http://localhost:8080/admin/fault \
  -H 'Content-Type: application/json' \
  -d '{"mode":"pool_exhaustion","magnitude_ms":900,"probability":1.0}'

# 2. fire an alert
curl -X POST http://localhost:8000/v1/alerts \
  -H 'Content-Type: application/json' \
  -H "X-Aegis-Ingest-Token: $ALERT_INGEST_TOKEN" \
  -d '{"external_id":"demo-1","title":"Checkout p99 latency cascade",
       "severity":"P1","environment":"local","service_hint":"checkout"}'

# 3. watch it at http://localhost:3000/incidents

# 4. clear the fault
curl -X DELETE http://localhost:8080/admin/fault
```

`POST /v1/alerts` returns `202 Accepted`. Re-posting the same `external_id`
attaches to the existing incident rather than opening a second one — ingestion is
idempotent by the `UNIQUE (source, external_id)` constraint on
`incident_alerts`.

The gateway reaches `checkout` and `payment` only over the compose network, so
target them with `docker compose exec` if you want to fault a downstream service
directly. Only `gateway` publishes a host port.

The reference workload implements exactly four fault modes: `none`, `latency`,
`error`, `pool_exhaustion` (`workload/service.py`).

### Seeding topology

```bash
backend/.venv/Scripts/python.exe scripts/seed_topology.py
```

Then `/graph` in the console renders something. Without it the graph is empty and
`analyze_topology` records an evidence gap — which is a correct outcome, just not
an interesting demo.

Or seed everything at once, including a demo incident driven through the real
ingestion endpoint:

```bash
make seed                      # or: bash scripts/seed-demo.sh
bash scripts/seed-demo.sh --fault latency
bash scripts/seed-demo.sh --fault none      # topology and an alert, no fault
```

The script injects the fault into the **workload**, never into Aegis, so the
agent is never handed privileged knowledge of what was broken (ESD §16).

---

## 6. Backend without Docker

```bash
cd C:\dev\aegis-2.0\backend

# tests (849 unit tests need no infrastructure)
.venv/Scripts/python.exe -m pytest tests/unit -q

# everything (11 integration tests need Postgres on 55433, else they skip)
.venv/Scripts/python.exe -m pytest tests -q

.venv/Scripts/python.exe -m ruff check src tests
.venv/Scripts/python.exe -m mypy src
```

Migrations run automatically in the API lifespan and in the worker before it
claims its first job, under a `pg_advisory_lock`, so several replicas booting at
once serialise instead of racing.

---

## 7. Frontend

```bash
cd C:\dev\aegis-2.0\frontend
npm install
npm run dev        # http://localhost:3000
npm run build      # 22 routes
npm run lint
npm run typecheck
```

Base URL resolution (`frontend/src/lib/api.ts`):

- server side: `AEGIS_API_INTERNAL_URL` ?? `http://api:8000`
- browser: `NEXT_PUBLIC_API_BASE_URL` ?? `http://localhost:8000`

Running `npm run dev` outside compose means the browser talks to
`http://localhost:8000`, so the API must be published — it is, by default.

`npm run build` does **not** run ESLint (`eslint.ignoreDuringBuilds: true`), so
run `npm run lint` separately. There is no test runner configured.

---

## 8. The benchmark

```bash
cd C:\dev\aegis-2.0
backend/.venv/Scripts/python.exe eval/run.py --suite smoke
backend/.venv/Scripts/python.exe eval/run.py --list-ablations
backend/.venv/Scripts/python.exe eval/run.py --suite latency_increase --dry-run
```

Reports land in `eval/reports/` (gitignored). See [evaluation.md](evaluation.md),
including the known gaps — several scenarios name fault modes the injector cannot
apply, and three ablations are no-ops.

---

## 9. Common problems

| Symptom | Cause |
|---|---|
| Compose exits with "variable is not set" | missing `--env-file .env`, or an unset `POSTGRES_PASSWORD` / `NEO4J_PASSWORD` / `ALERT_INGEST_TOKEN` |
| Integration tests all skip | Postgres not published on 55433; check `docker compose ps` |
| Connections land on the wrong database | a native PostgreSQL owns 5432; that is why the publish port is 55433 |
| `web` healthcheck flapping | it probes `127.0.0.1`, not `localhost`, because the container resolves localhost to `::1` first while Next binds IPv4 only |
| `/graph` empty | run `scripts/seed_topology.py`, or check Neo4j in `/health` |
| `/debug` and `/deployments` empty | expected — no code writes `sandbox_runs`, `remediation_patches` or `deployment_attempts`. See [execution.md](execution.md#8-what-is-not-implemented) |
| Change analysis shows an evidence gap | `GITHUB_TOKEN` is empty. Correct behaviour; see [retrieval.md](retrieval.md#8-known-limitation-change-analysis-degrades-when-github-is-unconfigured) |
| Prometheus shows `aegis-api` down | expected — the API exposes no `/metrics` endpoint. See [observability.md](observability.md) |
| Autonomy never fires | `AUTONOMY_ENABLED=false` by default, and in production the service allowlist is empty. See [policy.md](policy.md#5-known-limitation-the-production-allowlist-is-empty) |

---

## 10. Things that do not exist

Recorded because earlier documentation referenced them:

- a root `package.json`, so `npm run graph:refresh` is not a command
- `docker compose --profile core` — there is no `core` profile
- `infra/kind/` — the directory exists but is empty
- a frontend test runner

---

## See also

- [architecture.md](architecture.md) — what all these processes are
- [testing.md](testing.md) · [evaluation.md](evaluation.md) · [runbooks.md](runbooks.md)
- [observability.md](observability.md) — what the telemetry stack does and does not give you
