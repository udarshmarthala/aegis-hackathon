# Retrieval

Lexical plus vector plus graph scope plus recency, fused by rank. Then a
hierarchical narrowing that turns "this service is broken" into "this function,
these lines".

Source: `backend/src/aegis/retrieval/` — `hybrid.py`, `embeddings.py`,
`documents.py`, `code.py`; and `backend/src/aegis/memory/`.

---

## 1. Why fusion rather than one ranker

In SRE retrieval the two signals fail in **opposite** directions:

- **Lexical** nails an exact error string, a metric name or a symbol, and misses
  every paraphrase.
- **Vector** finds "connection pool exhausted" from "too many clients already",
  and happily returns a plausible but literally wrong passage.

Reciprocal Rank Fusion combines the **ranks**, not the raw scores, so neither
ranker's score distribution has to be calibrated against the other — a
calibration that silently rots whenever the embedding model changes.

```python
RRF_K: Final = 60

WEIGHT_LEXICAL: Final = 1.0
WEIGHT_VECTOR: Final = 0.9
WEIGHT_GRAPH: Final = 0.7
WEIGHT_RECENCY: Final = 0.5

RECENCY_HALF_LIFE_DAYS: Final = 14.0
```

`reciprocal_rank_fusion` is a **pure function** with no I/O, so ranking behaviour
is unit-testable without a database or a provider
(`tests/unit/test_retrieval_fusion.py`).

Four signals: `lexical`, `vector`, `graph`, `recency`. Graph scope contributes a
rank, so services structurally near the incident are preferred without a hard
filter that would hide a relevant document from one hop further out.

### Bounds

| Constant | Value | Why |
|---|---|---|
| `MAX_QUERY_CHARS` | 1,000 | a longer query is a pasted log file; embedding it costs money and its tsquery matches nothing useful |
| `MAX_RESULT_LIMIT` | 50 | |
| `_POOL_MULTIPLIER` / `_MAX_POOL` | 4 / 200 | candidate pool before fusion |

---

## 2. Degradation is explicit

When embeddings are unavailable the search still runs lexically, but the result
carries `degraded=True` and a reason.

> A lexical-only answer presented as a full one is how "we could not look
> properly" gets mistaken for "there is nothing there".

`retrieval/embeddings.py` states the same rule from the other side:

> Returning zero vectors would make every cosine similarity identical and turn
> semantic search into a silent no-op that still looks like it ran.

Callers therefore check `EmbeddingClient.configured` first and degrade
explicitly, or catch `SourceUnavailable` and record an evidence gap.

`SearchResult` exposes `is_empty` separately from `degraded`, so "found nothing"
and "could not look" stay distinct all the way to the caller.

---

## 3. Embeddings

Embeddings come from Google AI Studio's native `generativelanguage` API, with
the same four-key ring the model router uses ([keyring](#), `core/keyring.py`).
There is no second vendor: Gemini quotas are enforced per key and per day, so
the unit that gets exhausted — and therefore the unit that must fail over — is
the key, not the provider.

| Behaviour | Detail |
|---|---|
| Model | `gemini-embedding-001` |
| Width | Requested explicitly as `outputDimensionality`. The model returns **3072** by default and the corpus columns are `vector(1536)`, so omitting the field would fail at insert time with a Postgres type error that names nothing useful |
| Key failover | 429 / 401 / 403 / 5xx park that key and advance; a 4xx about the request stops at the first key, because all four would reject it identically |
| Normalisation | None. Truncated Gemini vectors are not unit length, which does not matter: ranking uses cosine distance (`<=>`), and cosine ignores magnitude |

| Bound | Value |
|---|---|
| `MAX_BATCH_SIZE` | 64 |
| `MAX_INPUT_CHARS` | 8,000 (per input; oversized inputs are a non-retryable 400) |
| Bulkhead limit | 4 concurrent requests per provider |

`_check_dimension` verifies the returned vector width against
`LLM_EMBEDDING_DIM`. The schema is `vector(1536)` in all three corpora, so a
provider change that alters the dimension is caught at ingestion rather than
producing silently meaningless similarities.

---

## 4. Ingestion

`retrieval/documents.py` writes two tables: `retrieval_documents` (runbooks,
postmortems, design docs) and `code_documents` (repository chunks). Neither is
ever read directly by an agent — only through `retrieval.hybrid`.

Three invariants:

- **Re-ingesting unchanged content is a no-op.** Ingestion runs on a schedule;
  without content-hash dedup the corpus doubles every sync and lexical ranking
  degrades as duplicates split the score. Enforced by unique indexes on
  `content_hash`.
- **Code chunks carry real line numbers.** A citation that resolves to the wrong
  lines is worse than no citation, because an operator will act on it.
- **Embedding failures never fail ingestion.** The row lands with a `NULL`
  embedding and stays lexically searchable — degraded, not lost.

Chunking:

| Constant | Value |
|---|---|
| `DEFAULT_CHUNK_CHARS` | 2,000 (~500 tokens, so several fit in a context window alongside metrics and traces) |
| `DEFAULT_OVERLAP_CHARS` | 200 (a symbol straddling a boundary stays retrievable from either side) |
| `MAX_CHUNKS_PER_DOCUMENT` | 200 (a generated file or vendored blob cannot fill the corpus alone) |

`classify_path` tags a chunk as `test` from markers (`test_`, `_test.`,
`/tests/`, `.spec.`, `.test.`) — used to **classify**, never to exclude.

---

## 5. Code localisation

`retrieval/code.py`. The product claim is "here is the function that broke", and
the only way to make it honestly is to **narrow before reading, never after**:

```
incident
  -> affected services      (cap 12)
  -> repositories           (cap 8)
  -> commits in the window  (cap 50)
  -> files those commits touched (cap 40)
  -> symbols matching the symptom (cap 25)
  -> tests covering those symbols (cap 15)
```

Every stage is capped, and every stage records what it received and what it
emitted in a `StageTrace` with a `truncated` flag.

> Handing a model a whole repository and asking it to find the bug is both
> unaffordable and unfalsifiable: there is no way to audit which narrowing step
> was wrong. A recorded stage trail makes a localisation failure diagnosable.

That maps to `FailureClass.CODE_LOCALIZATION_FAILURE` in the benchmark.

Nothing loads a repository into memory. Content comes from `code_documents`
chunks that ingestion already bounded; commit metadata comes from an injected
`CommitSource` protocol, not from a clone. `MAX_SNIPPET_CHARS = 4_000`.

Symptom terms are extracted with a conservative regex
(`[A-Za-z_][A-Za-z0-9_.]{2,}`) minus a stopword set, capped at
`MAX_SYMPTOM_TERMS = 12`.

### Service-to-repository mapping

`service_repositories` maps a service to the repos and path prefixes that
implement it, with a `rank` to break ties when several services share a monorepo.
Without a mapping the stage emits nothing and records a degrade reason — it does
not guess.

---

## 6. Incident memory

`backend/src/aegis/memory/`. The one store in Aegis that feeds itself: what is
written here is retrieved during the next incident, cited as precedent, and
shapes which hypotheses an investigator even considers.

### The write path is the strictest in the codebase

`memory/store.py` refuses to write unless **all** hold:

- the diagnosis did **not** abstain — an abstention is a statement about
  evidence, not about cause; recalling one as "what happened last time"
  manufactures a root cause out of an admission of ignorance;
- the remediation was **verified** — an unverified remediation is a hypothesis,
  and recalled six weeks later it reads as a proven playbook;
- the memory is **approved** by a human before it becomes organisational
  knowledge.

> A contaminated memory store does not fail loudly. It quietly biases retrieval
> for every future incident, and the bias is undetectable from inside the
> investigation that suffers from it.

Hence a typed refusal at the boundary rather than a flag on the row.
`tests/unit/test_memory_contamination.py` covers each refusal.

### The read path keeps two mechanisms separate

`memory/recall.py`:

| Mechanism | Strength |
|---|---|
| **Recurrence signature** — exact `fingerprint` match | strong and cheap: same failure mode, same services, same cause category |
| **Hybrid similarity** — lexical + semantic over the memory corpus | weaker; catches a differently-worded symptom or a different blast radius |

They are deliberately **not merged**. Collapsing them into one number would let a
loose textual resemblance be presented with the authority of an exact
recurrence.

Everything returned is **Tier C**. The evidence store assigns that from
`SourceType.MEMORY`; nothing in the memory package chooses it.

---

## 7. Exposure

### MCP tools

| Tool | Scope |
|---|---|
| `hybrid_search` | `knowledge:search` |
| `similar_incidents` | `memory:read` |
| `recurring_patterns` | `memory:read` |
| `localize_code` | `code:read` |
| `read_file_at_ref` | `code:read` |
| `recent_commits` | `code:read` |
| `compare_refs` | `code:read` |

### API and console

`GET /v1/reliability/recurring` backs the `/recurring` page
(`RecurringPatternCard`, windows of 30/90/180/365 days, occurrence thresholds of
2/3/5). `/debug` drives the incident → service → repo → commit → files
narrowing interactively.

---

## 8. Known limitation: change analysis degrades when GitHub is unconfigured

`recent_commits`, `compare_refs` and `read_file_at_ref` all go through
`GitHubClient`. A missing `GITHUB_TOKEN` is not papered over:

> A missing token is not an error condition to paper over: reads raise
> `SourceUnavailable` (recorded as an evidence gap) and writes raise
> `AuthorizationError`. Neither ever returns a fabricated response.

In the reference environment `GITHUB_TOKEN` is empty, so:

- `Container.build_optional` marks the `github` capability
  `configured=False, reason="no github token is configured"`;
- the `analyze_changes` node records an evidence gap rather than commits;
- code localisation's commit stage emits nothing and records a degrade reason;
- `ConfidenceBreakdown.gap_ratio` rises and derived confidence falls.

The behaviour is correct. The capability is simply not exercised. The same
applies to `SLACK_BOT_TOKEN` / `SLACK_WEBHOOK_URL` for notification, and to the
embedding provider keys for the vector half of retrieval.

---

## See also

- [evidence.md](evidence.md) — trust tiers and the degraded/absent distinction
- [graph.md](graph.md) — the graph signal in the fusion
- [agents.md](agents.md) — `recall_memory` and `localize_code` nodes
- [data-model.md](data-model.md#6-memory-and-retrieval) — the corpora and their indexes
