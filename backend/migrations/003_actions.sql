-- Actions, policy decisions, approvals, leases, verification, audit.
-- This file holds the safety-critical tables; every constraint here exists to
-- make an unsafe sequence impossible rather than merely unlikely.

CREATE TABLE IF NOT EXISTS remediation_actions (
    id                  TEXT PRIMARY KEY,
    incident_id         TEXT NOT NULL REFERENCES incidents(id) ON DELETE CASCADE,
    action_type         TEXT NOT NULL,
    state               TEXT NOT NULL DEFAULT 'PROPOSED',
    resource_type       TEXT NOT NULL,
    resource_id         TEXT NOT NULL,
    service_id          TEXT,
    environment         TEXT NOT NULL,
    reason              TEXT NOT NULL,
    supporting_evidence TEXT[] NOT NULL DEFAULT '{}',
    expected_effect     JSONB NOT NULL DEFAULT '{}'::jsonb,
    blast_radius        JSONB NOT NULL DEFAULT '{}'::jsonb,
    rollback_plan       JSONB,
    verification_plan   JSONB NOT NULL DEFAULT '{}'::jsonb,
    -- Idempotency key is globally unique: a retried proposal returns the
    -- original action instead of acting on production twice (ESD 19).
    idempotency_key     TEXT NOT NULL UNIQUE,
    proposed_by         TEXT NOT NULL DEFAULT 'remediation_planner',
    executed_at         TIMESTAMPTZ,
    completed_at        TIMESTAMPTZ,
    result              JSONB,
    error               TEXT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS actions_incident_idx ON remediation_actions (incident_id, created_at DESC);
CREATE INDEX IF NOT EXISTS actions_state_idx ON remediation_actions (state, created_at DESC);
-- Supports the autonomy rate limit without a table scan.
CREATE INDEX IF NOT EXISTS actions_executed_recent_idx
    ON remediation_actions (environment, executed_at DESC)
    WHERE executed_at IS NOT NULL;

-- Policy decisions are recorded separately from actions so that risk tier and
-- effect remain independently auditable (ESD 18).
CREATE TABLE IF NOT EXISTS policy_decisions (
    id              TEXT PRIMARY KEY,
    action_id       TEXT NOT NULL REFERENCES remediation_actions(id) ON DELETE CASCADE,
    incident_id     TEXT NOT NULL REFERENCES incidents(id) ON DELETE CASCADE,
    effect          TEXT NOT NULL CHECK (effect IN ('ALLOW','REQUIRE_HUMAN','BLOCK')),
    risk_tier       SMALLINT NOT NULL CHECK (risk_tier BETWEEN 0 AND 3),
    matched_rule    TEXT NOT NULL,
    reasons         TEXT[] NOT NULL DEFAULT '{}',
    gates           JSONB NOT NULL DEFAULT '[]'::jsonb,
    policy_version  TEXT NOT NULL,
    context_snapshot JSONB NOT NULL DEFAULT '{}'::jsonb,
    decided_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at      TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS policy_decisions_action_idx ON policy_decisions (action_id);

CREATE TABLE IF NOT EXISTS approvals (
    id             TEXT PRIMARY KEY,
    action_id      TEXT NOT NULL REFERENCES remediation_actions(id) ON DELETE CASCADE,
    incident_id    TEXT NOT NULL REFERENCES incidents(id) ON DELETE CASCADE,
    requested_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- A stale approval must never be executable (ESD 20); expiry is enforced
    -- both here and re-checked at execution time.
    expires_at     TIMESTAMPTZ NOT NULL,
    decision       TEXT CHECK (decision IN ('approved','rejected','more_evidence')),
    decided_by     TEXT REFERENCES users(id),
    decided_at     TIMESTAMPTZ,
    note           TEXT NOT NULL DEFAULT '',
    CHECK (decision IS NULL OR decided_at IS NOT NULL)
);
-- At most one open approval per action.
CREATE UNIQUE INDEX IF NOT EXISTS approvals_one_open_idx
    ON approvals (action_id) WHERE decision IS NULL;

-- The database is the concurrency arbiter (ESD 19). The partial unique index
-- means two workers cannot both hold a live lease on one resource.
CREATE TABLE IF NOT EXISTS resource_leases (
    id             TEXT PRIMARY KEY,
    resource_type  TEXT NOT NULL,
    resource_id    TEXT NOT NULL,
    holder         TEXT NOT NULL,
    incident_id    TEXT REFERENCES incidents(id) ON DELETE SET NULL,
    acquired_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at     TIMESTAMPTZ NOT NULL,
    released_at    TIMESTAMPTZ
);
CREATE UNIQUE INDEX IF NOT EXISTS resource_leases_active_uniq
    ON resource_leases (resource_type, resource_id)
    WHERE released_at IS NULL;
CREATE INDEX IF NOT EXISTS resource_leases_expiry_idx
    ON resource_leases (expires_at) WHERE released_at IS NULL;

CREATE TABLE IF NOT EXISTS verification_runs (
    id            TEXT PRIMARY KEY,
    incident_id   TEXT NOT NULL REFERENCES incidents(id) ON DELETE CASCADE,
    action_id     TEXT REFERENCES remediation_actions(id) ON DELETE CASCADE,
    kind          TEXT NOT NULL DEFAULT 'action',
    passed        BOOLEAN NOT NULL,
    checks        JSONB NOT NULL DEFAULT '[]'::jsonb,
    notes         TEXT NOT NULL DEFAULT '',
    started_at    TIMESTAMPTZ NOT NULL,
    completed_at  TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS verification_incident_idx ON verification_runs (incident_id);

-- Append-only audit. Every consequential event lands here with a correlation id
-- that ties it to logs, OTel traces and the LangSmith run (ESD 40).
CREATE TABLE IF NOT EXISTS audit_log (
    id             BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    incident_id    TEXT,
    actor          TEXT NOT NULL,
    actor_type     TEXT NOT NULL CHECK (actor_type IN ('human','agent','system')),
    event_type     TEXT NOT NULL,
    resource_type  TEXT,
    resource_id    TEXT,
    detail         JSONB NOT NULL DEFAULT '{}'::jsonb,
    correlation_id TEXT,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS audit_incident_idx ON audit_log (incident_id, created_at DESC);
CREATE INDEX IF NOT EXISTS audit_created_idx ON audit_log (created_at DESC);
CREATE INDEX IF NOT EXISTS audit_correlation_idx ON audit_log (correlation_id);

-- Operator-controlled kill switches. Absence of a row means "not engaged", but
-- an unreadable table is treated as engaged by KillSwitchState.fail_closed.
CREATE TABLE IF NOT EXISTS kill_switches (
    scope       TEXT NOT NULL CHECK (scope IN ('global','environment','action_type','service')),
    target      TEXT NOT NULL DEFAULT '',
    engaged     BOOLEAN NOT NULL DEFAULT TRUE,
    reason      TEXT NOT NULL DEFAULT '',
    engaged_by  TEXT,
    engaged_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (scope, target)
);
