-- Agent observability and the durable workflow queue.

CREATE TABLE IF NOT EXISTS agent_runs (
    id             TEXT PRIMARY KEY,
    incident_id    TEXT REFERENCES incidents(id) ON DELETE CASCADE,
    agent_role     TEXT NOT NULL,
    parent_run_id  TEXT REFERENCES agent_runs(id) ON DELETE CASCADE,
    status         TEXT NOT NULL DEFAULT 'running',
    model          TEXT,
    provider       TEXT,
    prompt_version TEXT,
    task           TEXT NOT NULL DEFAULT '',
    result_summary TEXT NOT NULL DEFAULT '',
    evidence_ids   TEXT[] NOT NULL DEFAULT '{}',
    input_tokens   INTEGER NOT NULL DEFAULT 0,
    output_tokens  INTEGER NOT NULL DEFAULT 0,
    cost_usd       NUMERIC(12,6) NOT NULL DEFAULT 0,
    duration_ms    INTEGER,
    langsmith_run_id TEXT,
    error          TEXT,
    started_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at    TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS agent_runs_incident_idx ON agent_runs (incident_id, started_at);
CREATE INDEX IF NOT EXISTS agent_runs_role_idx ON agent_runs (agent_role, started_at DESC);

CREATE TABLE IF NOT EXISTS tool_calls (
    id            TEXT PRIMARY KEY,
    agent_run_id  TEXT REFERENCES agent_runs(id) ON DELETE CASCADE,
    incident_id   TEXT REFERENCES incidents(id) ON DELETE CASCADE,
    server        TEXT NOT NULL,
    tool          TEXT NOT NULL,
    -- read/write classification is recorded per call so an audit can prove no
    -- write tool was invoked outside the gate chain.
    access        TEXT NOT NULL DEFAULT 'read' CHECK (access IN ('read','write')),
    arguments     JSONB NOT NULL DEFAULT '{}'::jsonb,
    ok            BOOLEAN NOT NULL DEFAULT TRUE,
    result_summary TEXT NOT NULL DEFAULT '',
    error         TEXT,
    duration_ms   INTEGER,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS tool_calls_run_idx ON tool_calls (agent_run_id, created_at);
CREATE INDEX IF NOT EXISTS tool_calls_incident_idx ON tool_calls (incident_id, created_at);

-- Durable workflow queue. FOR UPDATE SKIP LOCKED gives competing workers
-- exactly-once pickup without introducing an external broker.
CREATE TABLE IF NOT EXISTS workflow_jobs (
    id             TEXT PRIMARY KEY,
    incident_id    TEXT NOT NULL REFERENCES incidents(id) ON DELETE CASCADE,
    kind           TEXT NOT NULL DEFAULT 'investigate',
    status         TEXT NOT NULL DEFAULT 'queued'
        CHECK (status IN ('queued','running','done','failed','cancelled')),
    attempts       INTEGER NOT NULL DEFAULT 0,
    max_attempts   INTEGER NOT NULL DEFAULT 3,
    locked_by      TEXT,
    locked_at      TIMESTAMPTZ,
    run_after      TIMESTAMPTZ NOT NULL DEFAULT now(),
    payload        JSONB NOT NULL DEFAULT '{}'::jsonb,
    last_error     TEXT,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS workflow_jobs_pickup_idx
    ON workflow_jobs (status, run_after) WHERE status = 'queued';
-- One live job per (incident, kind): a duplicate enqueue is a no-op.
CREATE UNIQUE INDEX IF NOT EXISTS workflow_jobs_one_active_idx
    ON workflow_jobs (incident_id, kind) WHERE status IN ('queued','running');
