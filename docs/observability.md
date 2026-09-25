# Observability

Two distinct things share this word in Aegis, and conflating them causes real
confusion:

1. **Telemetry as an evidence source** — Prometheus, Tempo and Loki are what
   Aegis *reads* to investigate an incident.
2. **Observability of Aegis itself** — logs, traces, audit and LangSmith, so an
   operator can see what the platform did.

Both are covered here. Source: `backend/src/aegis/telemetry/`,
`backend/src/aegis/core/logging.py`, `backend/src/aegis/integrations/langsmith.py`,
`infra/otel/`, `infra/prometheus/`.

---

## 1. The governing invariant

> Observability is never a control-plane dependency.

LangSmith, Prometheus, Tempo, Loki or Neo4j being down degrades confidence and
records an evidence gap. It never halts or crashes an incident workflow.

The enforcement is per-module and consistent:

| Module | On failure |
|---|---|
| `telemetry/prometheus.py` | raises `SourceUnavailable` → caller records an evidence gap |
| `telemetry/tempo.py` | raises `SourceUnavailable` |
| `telemetry/loki.py` | raises `SourceUnavailable` |
| `graph/client.py` | raises `SourceUnavailable` |
| `telemetry/otel.py` | **swallows everything** — a missing collector degrades tracing and nothing else |
| `integrations/langsmith.py` | **swallows everything**, returns a falsy value |

The split is deliberate. A failed *read of evidence* must be visible to the
investigation, because it changes what can be concluded. A failed *emission of a
trace* must not be, because it changes nothing about the incident.

LangSmith has one deliberate exception:

> `trace_run` re-raises whatever the *traced body* raised. Tracing failures are
> absorbed; the work being traced is not, or a crashed agent step would look like
> a successful one.

---

## 2. Telemetry as evidence

### Prometheus

`telemetry/prometheus.py`. Queries are built from a small set of templates with
the service name bound as a PromQL label matcher. **Callers never pass raw
PromQL.** The closed metric catalogue lives in `mcp/tools/telemetry.py`;
`agents/workflow.py` uses exactly three of them:

```python
INVESTIGATION_METRICS: tuple[str, ...] = ("error_rate", "latency_p99", "request_rate")
METRIC_WINDOW_S = 900
```

The window is fixed rather than model-chosen so two runs of the same scenario are
comparable.

Metric evidence is **Tier A** (direct machine observation). It is the only tier
that satisfies policy rule 7's "no direct observation" check, so an autonomous
action is impossible without a working metrics source.

### Tempo

`telemetry/tempo.py`. TraceQL is rendered here, with caller values bound through
`escape_traceql`. `service_call_edges` returns a **frozen dataclass rather than a
dict**, because the graph package turns its output into `CALLS` edges and a key
rename would break topology ingestion silently.

Trace evidence is Tier A.

### Loki

`telemetry/loki.py`. Callers describe what they want with a `LogSelector`
(service, optional level, optional substring) and the module renders the LogQL.

> A service name lifted out of an alert payload is attacker-influenceable, and
> `{service="x"} |= ""} |= "secret"` is a query injection in exactly the way SQL
> is.

Log evidence is **Tier D** — untrusted free text — and log lines leave the module
wrapped in `UntrustedText`. See [evidence.md](evidence.md#2-untrustedtext--data-never-instruction).

### Local stack

`infra/otel/collector.yaml` receives OTLP on gRPC 4317 and HTTP 4318, applies a
`memory_limiter` (256 MiB, 64 MiB spike) and a `batch` processor (5 s / 512), then
exports traces to Tempo and metrics to Prometheus on `0.0.0.0:8889`.

`infra/otel/tempo.yaml` is single-binary Tempo: HTTP 3200, OTLP gRPC 4317,
`max_block_duration: 5m`, `block_retention: 6h`, local storage.

`infra/prometheus/prometheus.yml`: 10 s scrape, 15 s evaluation, external labels
`environment: local` / `cluster: aegis-local`. Jobs: `prometheus` (itself),
`aegis-api` (`api:8000/metrics`), `workload` (gateway, checkout, payment on
:8080). Every job sets a consistent `service` label because Aegis binds it into
its PromQL templates.

---

## 3. Observability of Aegis itself

### Correlation id — the thing that ties it all together

One identifier walks an investigator across structured logs, the audit trail, the
OTel trace and the LangSmith run.

```python
cid = request.headers.get("X-Correlation-ID") or correlation_id()
bind_correlation_id(cid)
...
response.headers["X-Correlation-ID"] = cid
```

The API middleware generates or echoes it. `core/logging.py` binds it into a
`ContextVar` so every log line in the request carries it. `audit_log` has a
dedicated index on `(correlation_id)`. `agent_runs` carries `langsmith_run_id`.

### Structured logs

`core/logging.py`, built on `structlog`. Two properties:

1. Every line carries `correlation_id` and, when known, `incident_id`, so one
   incident can be reconstructed across the API, the worker and agent processes.
2. **Secrets never reach a log sink.** The redaction processor runs *last*, after
   every other processor, so it also covers values injected by libraries. The
   patterns are deliberately broad — a false positive costs a masked log line, a
   false negative costs a leaked credential.

`LOG_FORMAT` selects JSON or console rendering; `LOG_LEVEL` sets the threshold.

### Tracing

`telemetry/otel.py`. `setup_tracing(settings)` is idempotent and
failure-tolerant, gated on `OTEL_TRACES_ENABLED`.

> The value Aegis actually needs from OTel is a shared correlation id so an
> operator can pivot between the infrastructure view and the AI view.

### Audit

`persistence/audit.py`. Append-only — there is no update or delete path in the
module, because a row that can be edited is not evidence of anything.

`actor_type` is CHECK-constrained to `('human','agent','system')`. Collapsing
those would make it impossible to answer "did a person authorise this?" — the
single most important question after an autonomous system touches production.

Audit writes are **non-blocking on failure but loud**: a failed write is logged
at ERROR and counted; it is never silently dropped, and it never aborts an
in-flight remediation.

Exposed at `GET /v1/audit` and `GET /v1/audit/incident/{incident_id}`, rendered at
`/audit` in the console with a `write_failures` surface.

### Agent-run and tool-call telemetry

| Table | What it records |
|---|---|
| `agent_runs` | one row per workflow node: role, status, model, provider, prompt version, task, summary, evidence ids, tokens, `cost_usd`, duration, `langsmith_run_id`, error |
| `tool_calls` | one row per invocation: server, tool, `access`, scope, environment, caller, arguments, `ok`, `degraded`, `degraded_reason`, evidence ids, duration |
| `evidence_gaps` | sources that could not be consulted, and what that affected |

`GET /v1/investigations/{incident_id}/runs`, `/tools` and `/gaps` back the
`/investigations/[incidentId]` console page, which renders each as an independent
query with its own failure state.

### LangSmith

`integrations/langsmith.py`. Agent tracing plus the evaluation harness's
datasets. Enabled by `LANGSMITH_TRACING` and `LANGSMITH_API_KEY`; unconfigured in
the reference environment, which costs a trace view and nothing else.

`health()` returns a tri-state `(bool | None, str)` — configured-and-healthy,
configured-and-failing, or not configured. The container marks the `langsmith`
capability with the reason, and `/health` reports it.

### Live streaming

`GET /v1/incidents/{incident_id}/stream` is a Server-Sent Events endpoint backed
by Redis pub/sub. Eight named event types: `snapshot`, `phase`, `evidence`,
`hypotheses`, `diagnosis`, `action_proposed`, `finished`, `update`.

The browser cannot set headers on an `EventSource`, so the token travels as an
`access_token` query parameter. Malformed frames are dropped rather than breaking
the stream. Losing Redis costs live updates; the incident still progresses and
the page still polls.

### Health endpoints

| Endpoint | Semantics |
|---|---|
| `GET /health/live` | process liveness, **dependency-free**. If this fails the process is genuinely wedged and restarting is correct. |
| `GET /health/ready` | Postgres only — the sole hard dependency. 503 when down. |
| `GET /health` | full dependency picture: postgres, neo4j, redis, prometheus, tempo, loki, plus each optional capability with a `hard_dependency` flag and an `affects` list |

`/health` is what backs the console's Integration Health surface, so the UI can
show "operating with reduced confidence" rather than implying an outage.

`core/resilience.breaker_states()` exposes circuit-breaker state, surfaced at
`/settings` as `CircuitBreakers`.

---

## 4. Known gap: Aegis exposes no Prometheus metrics

There is **no `/metrics` endpoint on the API**. Grepping `backend/src` for
`prometheus_client`, `make_asgi_app`, `Instrumentator`, `generate_latest` or
`/metrics` returns exactly one hit — `GET /v1/systems/services/{id}/metrics`,
which is a read of the *observed* workload's golden signals, not an exposition of
Aegis's own.

Consequences:

- the `aegis-api` scrape job in `infra/prometheus/prometheus.yml` targets
  `api:8000/metrics` and will always fail;
- `ESD.md` §44's "emitted metrics" criterion is not met for the control plane;
- there is no RED/USE dashboard for Aegis itself. Operational visibility today
  comes from structured logs, the audit log, `agent_runs`/`tool_calls` and OTel
  traces.

The instrumented reference workload *does* export metrics
(`http_requests_total`, `http_request_duration_seconds`,
`connection_pool_exhausted_total`), which is why the `workload` scrape job works
and the `aegis-api` one does not.

---

## 5. Resilience primitives

`core/resilience.py` enforces the rules every outbound call obeys:

- **no unbounded await** — `with_timeout` converts to a typed `TimeoutExceeded`;
- **retries only for errors explicitly marked `retryable`**, with full jitter;
- **circuit breakers** — a failing dependency trips a breaker instead of
  consuming the whole pool;
- **bulkheads** — concurrency into any one dependency is capped
  (`Bulkhead("neo4j", limit=8)`, embeddings at 4).

Breaker state is **process-local by design**:

> A shared breaker would need a network round trip to decide whether to make a
> network call, which adds the very failure mode it is meant to contain.

`guarded_call` composes timeout + retry + breaker + bulkhead and is what every
client and every tool invocation goes through.

---

## 6. Error taxonomy

`core/errors.py`. Every failure is one of these, and two fields drive platform
behaviour:

- `retryable` — whether `retry_async` may retry it. Anything that mutates
  non-idempotent external state is never retryable.
- `code` — a stable machine identifier surfaced to the API and the UI, so
  operators see a real cause instead of "internal error".

```
AegisError
├── ConfigError            ├── PolicyViolation
├── DomainError            ├── EvidenceError
├── NotFoundError          ├── LeaseConflict
├── ValidationError        ├── BudgetExhausted
├── AuthenticationError    └── ExternalServiceError
├── AuthorizationError         ├── SourceUnavailable
                                ├── CircuitOpen
                                └── TimeoutExceeded
```

`api/app.py` translates `AegisError` into a JSON body carrying `code`, `message`
and `context`, with the correlation id in the response header.

No bare `except:`. No silent `pass`.

---

## See also

- [evidence.md](evidence.md) — evidence gaps and trust tiers
- [runbooks.md](runbooks.md) — using this when something is wrong
- [data-model.md](data-model.md) — `audit_log`, `agent_runs`, `tool_calls`, `evidence_gaps`
- [aws-architecture.md](aws-architecture.md) — CloudWatch and retention in the deployed model
