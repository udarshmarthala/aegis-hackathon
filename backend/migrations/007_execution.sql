-- Execution: action arguments, sandbox runs and staged deployments.
-- Forward-only and idempotent, like every migration here: the worker and the
-- API both run this on boot and neither may fail because the other went first.

-- Action arguments are scalars only (see domain.ActionProposal). Stored
-- separately from expected_effect so that "what the action does" and "how we
-- will know it worked" stay independently auditable.
ALTER TABLE remediation_actions
    ADD COLUMN IF NOT EXISTS arguments JSONB NOT NULL DEFAULT '{}'::jsonb;

-- Every sandboxed code execution, recorded whether or not it succeeded. A
-- remediation that was tested and failed is as important to the audit trail as
-- one that passed.
CREATE TABLE IF NOT EXISTS sandbox_runs (
    id              TEXT PRIMARY KEY,
    incident_id     TEXT REFERENCES incidents(id) ON DELETE CASCADE,
    action_id       TEXT REFERENCES remediation_actions(id) ON DELETE SET NULL,
    purpose         TEXT NOT NULL CHECK (purpose IN
                        ('reproduce','test_patch','regression','build','static_check')),
    image           TEXT NOT NULL,
    repo            TEXT,
    base_ref        TEXT,
    patch_sha256    TEXT,
    command         TEXT NOT NULL,
    exit_code       INTEGER,
    timed_out       BOOLEAN NOT NULL DEFAULT FALSE,
    killed          BOOLEAN NOT NULL DEFAULT FALSE,
    duration_ms     INTEGER,
    -- Captured output is bounded at write time; an unbounded log from a runaway
    -- test would otherwise be able to fill the incident database.
    stdout_excerpt  TEXT NOT NULL DEFAULT '',
    stderr_excerpt  TEXT NOT NULL DEFAULT '',
    artifacts       JSONB NOT NULL DEFAULT '[]'::jsonb,
    resource_limits JSONB NOT NULL DEFAULT '{}'::jsonb,
    network         TEXT NOT NULL DEFAULT 'none',
    started_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at     TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS sandbox_runs_incident_idx
    ON sandbox_runs (incident_id, started_at DESC);
CREATE INDEX IF NOT EXISTS sandbox_runs_action_idx ON sandbox_runs (action_id);

-- A candidate remediation as a concrete, reviewable change. Kept distinct from
-- the action that applies it: a patch can be proposed, tested and rejected
-- without any action ever being executed.
CREATE TABLE IF NOT EXISTS remediation_patches (
    id              TEXT PRIMARY KEY,
    incident_id     TEXT NOT NULL REFERENCES incidents(id) ON DELETE CASCADE,
    repo            TEXT NOT NULL,
    base_ref        TEXT NOT NULL,
    summary         TEXT NOT NULL,
    rationale       TEXT NOT NULL DEFAULT '',
    diff            TEXT NOT NULL,
    diff_sha256     TEXT NOT NULL,
    files_changed   TEXT[] NOT NULL DEFAULT '{}',
    lines_added     INTEGER NOT NULL DEFAULT 0,
    lines_removed   INTEGER NOT NULL DEFAULT 0,
    supporting_evidence TEXT[] NOT NULL DEFAULT '{}',
    reproduction_run_id TEXT REFERENCES sandbox_runs(id) ON DELETE SET NULL,
    test_run_id     TEXT REFERENCES sandbox_runs(id) ON DELETE SET NULL,
    state           TEXT NOT NULL DEFAULT 'PROPOSED' CHECK (state IN
                        ('PROPOSED','REPRODUCED','TESTED','REJECTED','PROMOTED')),
    pull_request_url TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS patches_incident_diff_idx
    ON remediation_patches (incident_id, diff_sha256);
CREATE INDEX IF NOT EXISTS patches_incident_idx
    ON remediation_patches (incident_id, created_at DESC);

-- Staged and production deployments Aegis performed or observed, so that
-- "we deployed to staging and it was healthy" is a record rather than a claim.
CREATE TABLE IF NOT EXISTS deployment_attempts (
    id              TEXT PRIMARY KEY,
    incident_id     TEXT REFERENCES incidents(id) ON DELETE CASCADE,
    action_id       TEXT REFERENCES remediation_actions(id) ON DELETE SET NULL,
    patch_id        TEXT REFERENCES remediation_patches(id) ON DELETE SET NULL,
    environment     TEXT NOT NULL,
    service_id      TEXT NOT NULL,
    from_version    TEXT,
    to_version      TEXT,
    strategy        TEXT NOT NULL DEFAULT 'rolling',
    state           TEXT NOT NULL DEFAULT 'PENDING' CHECK (state IN
                        ('PENDING','IN_PROGRESS','DEPLOYED','VERIFIED',
                         'FAILED','ROLLED_BACK')),
    verification_id TEXT REFERENCES verification_runs(id) ON DELETE SET NULL,
    detail          JSONB NOT NULL DEFAULT '{}'::jsonb,
    error           TEXT,
    started_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at     TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS deployment_attempts_incident_idx
    ON deployment_attempts (incident_id, started_at DESC);
CREATE INDEX IF NOT EXISTS deployment_attempts_service_idx
    ON deployment_attempts (service_id, started_at DESC);

-- Verification claims. The CLAIM/EVIDENCE/TEST/RESULT primitive persisted, so a
-- verdict can be re-read and disputed line by line rather than trusted whole.
CREATE TABLE IF NOT EXISTS verification_claims (
    id              TEXT PRIMARY KEY,
    verification_id TEXT NOT NULL REFERENCES verification_runs(id) ON DELETE CASCADE,
    incident_id     TEXT NOT NULL REFERENCES incidents(id) ON DELETE CASCADE,
    claim           TEXT NOT NULL,
    test_kind       TEXT NOT NULL,
    test_spec       JSONB NOT NULL DEFAULT '{}'::jsonb,
    outcome         TEXT NOT NULL CHECK (outcome IN
                        ('PASS','FAIL','INCONCLUSIVE','UNAVAILABLE')),
    before_value    DOUBLE PRECISION,
    after_value     DOUBLE PRECISION,
    threshold       DOUBLE PRECISION,
    evidence_ids    TEXT[] NOT NULL DEFAULT '{}',
    detail          TEXT NOT NULL DEFAULT '',
    observed_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS verification_claims_run_idx
    ON verification_claims (verification_id);

-- verification_runs predates the richer verdict vocabulary; widen it in place.
ALTER TABLE verification_runs
    ADD COLUMN IF NOT EXISTS verdict TEXT NOT NULL DEFAULT 'INCONCLUSIVE';
ALTER TABLE verification_runs
    ADD COLUMN IF NOT EXISTS baseline_window JSONB NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE verification_runs
    ADD COLUMN IF NOT EXISTS observation_window JSONB NOT NULL DEFAULT '{}'::jsonb;
