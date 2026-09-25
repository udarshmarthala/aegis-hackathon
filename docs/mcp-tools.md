# The tool boundary

Everything an agent can reach outside its own reasoning is a `ToolSpec`, and
every answer it gets back is a `ToolResult`. There is no other door.

Source: `backend/src/aegis/mcp/` — `types.py`, `registry.py`, `invoker.py`,
`server.py`, `deps.py`, `tools/`.

---

## 1. The catalogue

**36 tools registered: 35 read, 1 write.** Verified by constructing the registry:

```
tool registry frozen  read_tools=35  tools=36  write_tools=1
```

| Tool | Access | Scope | Mutates |
|---|---|---|---|
| `query_metric_range` | read | `telemetry:metrics` | nothing |
| `compare_metric_windows` | read | `telemetry:metrics` | nothing |
| `service_error_rate` | read | `telemetry:metrics` | nothing |
| `service_latency` | read | `telemetry:metrics` | nothing |
| `search_traces` | read | `telemetry:traces` | nothing |
| `trace_detail` | read | `telemetry:traces` | nothing |
| `service_call_edges` | read | `telemetry:traces` | nothing |
| `error_logs` | read | `telemetry:logs` | nothing |
| `log_patterns` | read | `telemetry:logs` | nothing |
| `service_neighbourhood` | read | `topology:read` | nothing |
| `upstream_dependencies` | read | `topology:read` | nothing |
| `blast_radius` | read | `topology:read` | nothing |
| `causal_paths` | read | `topology:read` | nothing |
| `services_sharing_dependency` | read | `topology:read` | nothing |
| `expand_graph_context` | read | `topology:read` | nothing |
| `owning_team` | read | `topology:read` | nothing |
| `hybrid_search` | read | `knowledge:search` | nothing |
| `localize_code` | read | `code:read` | nothing |
| `read_file_at_ref` | read | `code:read` | nothing |
| `recent_commits` | read | `code:read` | nothing |
| `compare_refs` | read | `code:read` | nothing |
| `similar_incidents` | read | `memory:read` | nothing |
| `recurring_patterns` | read | `memory:read` | nothing |
| `list_services` | read | `runtime:read` | nothing |
| `get_service` | read | `runtime:read` | nothing |
| `list_instances` | read | `runtime:read` | nothing |
| `instance_logs` | read | `runtime:read` | nothing |
| `service_health` | read | `runtime:read` | nothing |
| `current_deployment` | read | `runtime:read` | nothing |
| `deployment_history` | read | `runtime:read` | nothing |
| `run_reproduction` | read | `sandbox:run` | **sandbox** |
| `test_patch` | read | `sandbox:run` | **sandbox** |
| `run_regression_suite` | read | `sandbox:run` | **sandbox** |
| `propose_action` | read | `remediation:propose` | nothing |
| `request_approval` | read | `remediation:approval` | **aegis_state** |
| **`execute_validated_action`** | **write** | `remediation:execute` | **environment** |

### Why sandbox tools are `access="read"`

They execute code, so they declare `mutates="sandbox"` — but the write class
exists to force the gate chain in front of **environment** mutation. A
reproduction run's entire purpose is to gather the evidence that would justify a
`ValidatedAction`; demanding one first would be circular. What keeps the
classification honest is the containment boundary underneath: no network, no
inherited credentials, bounded CPU/memory/wall-clock, read-only root filesystem.
See [execution.md](execution.md#7-the-sandbox).

`propose_action` is likewise a read: proposing is an agent's job. It creates no
external effect.

### The twelve scopes

```python
SCOPES: Final[frozenset[str]] = frozenset({
    "telemetry:metrics", "telemetry:traces", "telemetry:logs",
    "topology:read", "knowledge:search", "code:read", "memory:read",
    "runtime:read", "sandbox:run",
    "remediation:propose", "remediation:approval", "remediation:execute",
})
```

---

## 2. Registration-time contract checks

`mcp/registry.py` builds the registry once at import and then freezes it.
Everything that could be wrong with a tool declaration fails **at startup**, not
at 3am during an incident:

- a duplicate name;
- an unknown scope;
- an output model with an untyped free-text field (must be `UntrustedText`);
- a `write`-class tool that does not demand a `ValidatedAction`.

Two invariants are asserted rather than assumed:

- `read_tools()` and `write_tools()` are **disjoint** and together account for
  every registered tool. A name in both sets would mean the write path could be
  reached through a read permission.
- `permitted_for` **fails closed**. An unknown environment, an unknown scope or a
  caller lacking the scope yields nothing — never "everything", never a
  best-effort subset chosen by the caller's name.

---

## 3. The invoker is the enforcement point

`mcp/invoker.py`. Every tool call in Aegis — from the LangGraph orchestrator,
from the API, from an external MCP client — goes through `ToolInvoker.invoke`.
Nothing else calls a handler directly, and a handler is useless on its own
because it has no way to obtain the validated arguments object or, for a write,
the `ValidatedAction` it requires.

What it guarantees, in order:

1. **Schema.** Arguments validated against the tool's Pydantic model in **strict
   mode**. Nothing is coerced; a wrong type is a rejection.
2. **Permission.** The caller must hold the tool's scope, and the tool must be
   declared for the caller's environment. Unknown scope or environment ⇒ deny.
3. **Budget.** Every accepted call charges `budget.charge_tool()`. An exhausted
   budget refuses before any work starts. The invoker can **spend** a budget; it
   has no method that raises one.
4. **Bounded execution.** `guarded_call` applies the tool's timeout, capped
   further by the caller's deadline and remaining wall-clock budget. No unbounded
   await exists under this boundary.
5. **Retry only where it is safe.** `attempts > 1` requires both `retryable` and
   `idempotent`. A write tool is structurally barred from being retryable, so no
   write is ever repeated automatically.
6. **The gate chain is not optional.** A `write`-class tool is refused unless the
   caller supplies an `execution.ValidatedAction` — a type only
   `ActionGate.validate` can construct. The write handler is never entered
   without one, and the action is re-checked for liveness immediately before the
   handler runs. `tests/unit/test_mcp_invoker.py` asserts both properties.
7. **Audit and persistence.** Every invocation — success, denial, failure or
   timeout — writes a `tool_calls` row. Every write-class attempt also writes an
   `audit_log` event (`TOOL_WRITE_INVOKED`).
8. **No tracebacks.** Every exception becomes a typed `ToolResult(ok=False)`. An
   agent never sees a stack trace: it is both an implementation leak and a large
   blob of instruction-shaped text.

Bounds on what is recorded: `MAX_RESULT_SUMMARY_CHARS = 2_000`,
`MAX_ARGUMENT_CHARS = 8_000`.

---

## 4. Result semantics

`ToolResult` carries `ok`, `value`, `degraded` and `degraded_reason`.

| `ok` | `degraded` | `value` | Meaning |
|---|---|---|---|
| `True` | `False` | empty | **We looked and found nothing.** A finding. |
| `True` | `True` | empty | **We could not look.** An evidence gap. |
| `False` | — | — | The call itself failed. |

No caller can confuse the first two by accident. The `tool_calls` table mirrors
the distinction with separate `ok` and `degraded` columns, indexed:

```sql
CREATE INDEX tool_calls_write_idx
    ON tool_calls (created_at DESC) WHERE access = 'write';
CREATE INDEX tool_calls_degraded_idx
    ON tool_calls (incident_id, created_at) WHERE degraded;
```

Nothing on a `ToolResult` feeds back into a permission decision. A budget ledger
can be charged but not raised; a caller identity can be read but not amended.

---

## 5. No raw query languages

The tool layer never accepts a query language from a caller.

| Source | What the caller supplies | What is rendered |
|---|---|---|
| Prometheus | a closed metric name + service + window | PromQL from a template |
| Tempo | service, operation, window | TraceQL via `escape_traceql` |
| Loki | a `LogSelector` (service, level, substring) | LogQL rendered in `telemetry/loki.py` |
| Neo4j | a validated `NodeLabel` / `RelType` + params | Cypher with a clamped depth |
| Sandbox | a suite name from a closed template set + one validated path | argv |

A service name lifted out of an alert payload is attacker-influenceable, and a
query language is a query language: `{service="x"} |= ""} |= "secret"` is an
injection in exactly the way SQL is. `graph/ontology.py` makes labels and
relationship types a closed enum because Cypher cannot parameterise them —
`label_token` / `rel_token` are the only supported way to turn a string into one,
and a caller passing `"Service) DETACH DELETE (n"` gets a `ValidationError`,
never a clause.

---

## 6. The stdio MCP server

`mcp/server.py` exposes the same registry, the same invoker and the same
enforcement over the Model Context Protocol on stdio. An external MCP client is
not a privileged caller.

Two deliberate restrictions:

- **Write tools are never advertised and never reachable.** A write tool requires
  a `ValidatedAction`, which only the gate chain can mint and which cannot cross
  a JSON transport. The catalogue lists reads only, and the invoker would refuse
  a write even if a client guessed the name.
- **Scopes come from the operator, not from the client.** The identity a session
  runs as is constructed by whoever starts the server. Nothing a client sends — a
  tool argument, a header, a prompt — can widen it.

### Current state: unavailable

The `mcp` Python package is an **optional dependency and is not installed** in
the reference environment:

```
$ python -c "import importlib.util; print(importlib.util.find_spec('mcp'))"
None
```

`mcp/server.py` still imports cleanly and `server_status()` reports the server
unavailable with the reason. That is deliberate — an optional transport must
never be able to stop the control plane from booting.

**The internal tool boundary is fully functional regardless.** The registry, the
invoker, all 36 tools, permission checks, budgets and `tool_calls` auditing work
exactly the same; only the external stdio transport is off. `.mcp.json` in the
repo root configures the `graphify` MCP server for development, not this one.

---

## 7. Where a tool result becomes evidence

`mcp/tools/support.py` provides the two helpers every tool uses:

- `record_evidence(...)` — stores one observation via `EvidenceStore` and returns
  its id, with trust assigned from the source type;
- `degraded(...)` — the answer when a source could not be consulted.

`bounded`, `window` and `offset_window` clamp caller-supplied sizes and time
ranges into what the source can actually serve (`MIN_WINDOW_S` /
`MAX_WINDOW_S`).

---

## See also

- [evidence.md](evidence.md) — `UntrustedText`, trust tiers, the degraded/absent distinction
- [execution.md](execution.md) — what `execute_validated_action` requires
- [agents.md](agents.md) — a node with no invoker records an evidence gap
- [data-model.md](data-model.md) — the `tool_calls` table
