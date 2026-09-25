-- Aegis 2.0 core schema.
-- Postgres is the system of record: incidents, evidence, decisions, audit.
-- Conventions: ULID text ids, timestamptz everywhere, JSONB for evolving
-- metadata while every dimension we filter or join on stays a real column.

CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- ---------------------------------------------------------------- users ----
CREATE TABLE IF NOT EXISTS users (
    id              TEXT PRIMARY KEY,
    firebase_uid    TEXT UNIQUE NOT NULL,
    email           TEXT NOT NULL,
    display_name    TEXT,
    roles           TEXT[] NOT NULL DEFAULT ARRAY['viewer'],
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at    TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS users_email_idx ON users (lower(email));

-- ------------------------------------------------------------ incidents ----
CREATE TABLE IF NOT EXISTS incidents (
    id                 TEXT PRIMARY KEY,
    title              TEXT NOT NULL,
    severity           TEXT NOT NULL CHECK (severity IN ('P1','P2','P3','P4')),
    state              TEXT NOT NULL,
    environment        TEXT NOT NULL,
    workload           TEXT NOT NULL DEFAULT 'default',
    affected_services  TEXT[] NOT NULL DEFAULT '{}',
    suspected_origin   TEXT,
    confidence         DOUBLE PRECISION CHECK (confidence IS NULL OR (confidence >= 0 AND confidence <= 1)),
    summary            TEXT NOT NULL DEFAULT '',
    owner              TEXT,
    correlation_id     TEXT NOT NULL DEFAULT '',
    metadata           JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    resolved_at        TIMESTAMPTZ
);
-- The incident list is the hottest read path: filter by state+severity, newest first.
CREATE INDEX IF NOT EXISTS incidents_state_sev_created_idx
    ON incidents (state, severity, created_at DESC);
CREATE INDEX IF NOT EXISTS incidents_env_created_idx
    ON incidents (environment, created_at DESC);
CREATE INDEX IF NOT EXISTS incidents_open_idx
    ON incidents (created_at DESC) WHERE resolved_at IS NULL;

-- Alerts. (source, external_id) is UNIQUE, which is what makes ingestion
-- idempotent: Alertmanager retrying the same alert cannot create a second
-- incident (PRD FR-1).
CREATE TABLE IF NOT EXISTS incident_alerts (
    id             TEXT PRIMARY KEY,
    incident_id    TEXT NOT NULL REFERENCES incidents(id) ON DELETE CASCADE,
    source         TEXT NOT NULL,
    external_id    TEXT NOT NULL,
    title          TEXT NOT NULL,
    severity       TEXT NOT NULL,
    service_hint   TEXT,
    labels         JSONB NOT NULL DEFAULT '{}'::jsonb,
    annotations    JSONB NOT NULL DEFAULT '{}'::jsonb,
    raw_payload    JSONB NOT NULL DEFAULT '{}'::jsonb,
    started_at     TIMESTAMPTZ,
    received_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (source, external_id)
);
CREATE INDEX IF NOT EXISTS incident_alerts_incident_idx ON incident_alerts (incident_id);

CREATE TABLE IF NOT EXISTS incident_state_transitions (
    id             BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    incident_id    TEXT NOT NULL REFERENCES incidents(id) ON DELETE CASCADE,
    from_state     TEXT,
    to_state       TEXT NOT NULL,
    actor          TEXT NOT NULL,
    reason         TEXT NOT NULL DEFAULT '',
    correlation_id TEXT,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS incident_transitions_incident_idx
    ON incident_state_transitions (incident_id, created_at);
