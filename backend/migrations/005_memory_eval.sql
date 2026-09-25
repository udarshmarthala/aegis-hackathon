-- Memory, hybrid-retrieval corpus and the evaluation benchmark.

CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS incident_memories (
    id                TEXT PRIMARY KEY,
    incident_id       TEXT REFERENCES incidents(id) ON DELETE SET NULL,
    title             TEXT NOT NULL,
    symptoms          TEXT NOT NULL DEFAULT '',
    root_cause        TEXT NOT NULL DEFAULT '',
    evidence_pattern  JSONB NOT NULL DEFAULT '{}'::jsonb,
    affected_services TEXT[] NOT NULL DEFAULT '{}',
    successful_fix    TEXT NOT NULL DEFAULT '',
    failed_attempts   TEXT[] NOT NULL DEFAULT '{}',
    verification      TEXT NOT NULL DEFAULT '',
    prevention        TEXT NOT NULL DEFAULT '',
    fingerprint       TEXT NOT NULL DEFAULT '',
    occurrences       INTEGER NOT NULL DEFAULT 1,
    -- Only validated memories become authoritative organisational knowledge
    -- (PRD FR-16). Unapproved memories stay retrievable but clearly marked.
    approved          BOOLEAN NOT NULL DEFAULT FALSE,
    approved_by       TEXT REFERENCES users(id),
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS memories_fingerprint_idx ON incident_memories (fingerprint);

CREATE TABLE IF NOT EXISTS runbooks (
    id          TEXT PRIMARY KEY,
    title       TEXT NOT NULL,
    body        TEXT NOT NULL,
    services    TEXT[] NOT NULL DEFAULT '{}',
    tags        TEXT[] NOT NULL DEFAULT '{}',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Unified corpus for hybrid retrieval: runbooks, postmortems, memories, docs.
-- tsv gives lexical BM25-ish search, embedding gives semantic search; the
-- retrieval layer fuses both with graph results.
CREATE TABLE IF NOT EXISTS retrieval_documents (
    id           TEXT PRIMARY KEY,
    doc_type     TEXT NOT NULL,
    ref_id       TEXT,
    title        TEXT NOT NULL,
    body         TEXT NOT NULL,
    services     TEXT[] NOT NULL DEFAULT '{}',
    metadata     JSONB NOT NULL DEFAULT '{}'::jsonb,
    embedding    vector(1536),
    tsv          tsvector GENERATED ALWAYS AS
                 (to_tsvector('english', coalesce(title,'') || ' ' || coalesce(body,''))) STORED,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS retrieval_tsv_idx ON retrieval_documents USING GIN (tsv);
CREATE INDEX IF NOT EXISTS retrieval_vec_idx ON retrieval_documents
    USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);

CREATE TABLE IF NOT EXISTS benchmark_cases (
    id           TEXT PRIMARY KEY,
    scenario_id  TEXT NOT NULL UNIQUE,
    workload     TEXT NOT NULL,
    category     TEXT NOT NULL,
    severity     TEXT NOT NULL,
    -- Ground truth lives here and is never exposed on any agent-facing path.
    ground_truth JSONB NOT NULL,
    fault_spec   JSONB NOT NULL DEFAULT '{}'::jsonb,
    enabled      BOOLEAN NOT NULL DEFAULT TRUE,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS evaluation_runs (
    id             TEXT PRIMARY KEY,
    suite          TEXT NOT NULL,
    agent_version  TEXT NOT NULL,
    prompt_version TEXT NOT NULL DEFAULT '',
    policy_version TEXT NOT NULL DEFAULT '',
    model          TEXT NOT NULL DEFAULT '',
    baseline       TEXT NOT NULL DEFAULT 'aegis',
    status         TEXT NOT NULL DEFAULT 'running',
    summary        JSONB NOT NULL DEFAULT '{}'::jsonb,
    started_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at    TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS benchmark_results (
    id                TEXT PRIMARY KEY,
    evaluation_run_id TEXT NOT NULL REFERENCES evaluation_runs(id) ON DELETE CASCADE,
    scenario_id       TEXT NOT NULL,
    incident_id       TEXT,
    passed            BOOLEAN NOT NULL,
    failure_class     TEXT,
    scores            JSONB NOT NULL DEFAULT '{}'::jsonb,
    predicted         JSONB NOT NULL DEFAULT '{}'::jsonb,
    duration_ms       INTEGER,
    tokens            INTEGER NOT NULL DEFAULT 0,
    cost_usd          NUMERIC(12,6) NOT NULL DEFAULT 0,
    langsmith_run_id  TEXT,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS benchmark_results_run_idx ON benchmark_results (evaluation_run_id);
CREATE INDEX IF NOT EXISTS benchmark_results_scenario_idx ON benchmark_results (scenario_id);
