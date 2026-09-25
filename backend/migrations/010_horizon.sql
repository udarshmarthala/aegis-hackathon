-- Long-horizon agent: working-memory checkpoints, the war-room event log,
-- episodic observations and resolved-incident memory cards.
-- Forward-only and idempotent like every migration here: the API and the
-- worker both run it on boot and neither may fail because the other went first.
--
-- None of these tables references `incidents` by foreign key. The horizon loop
-- checkpoints before the incident row is guaranteed to be visible to another
-- process, and an event log that refuses a row because of an ordering race is
-- an event log with holes in it. Retention is by incident id instead.

-- Working memory. One row per (run, step), written after EVERY step: a worker
-- killed mid-incident resumes from the highest step of its run. The state is
-- bounded by the caps in `domain/horizon.py`, so the row is too.
CREATE TABLE IF NOT EXISTS horizon_checkpoints (
    run_id       TEXT NOT NULL,
    incident_id  TEXT NOT NULL,
    step         INTEGER NOT NULL CHECK (step >= 0),
    phase        TEXT NOT NULL,
    state        JSONB NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (run_id, step)
);
CREATE INDEX IF NOT EXISTS horizon_checkpoints_incident_idx
    ON horizon_checkpoints (incident_id, created_at DESC);
-- `latest_run()` reads the newest checkpoint across every incident.
CREATE INDEX IF NOT EXISTS horizon_checkpoints_created_idx
    ON horizon_checkpoints (created_at DESC);

-- The event stream. `seq` is the monotonic id the SSE stream carries as its
-- event id, so a reconnecting browser replays exactly what it missed.
CREATE TABLE IF NOT EXISTS horizon_events (
    seq             BIGSERIAL PRIMARY KEY,
    incident_id     TEXT NOT NULL,
    run_id          TEXT NOT NULL,
    step            INTEGER NOT NULL DEFAULT 0,
    phase           TEXT NOT NULL,
    event_type      TEXT NOT NULL,
    tool            TEXT,
    status          TEXT NOT NULL DEFAULT 'ok',
    source          TEXT NOT NULL DEFAULT 'system',
    duration_ms     INTEGER NOT NULL DEFAULT 0,
    context_tokens  INTEGER NOT NULL DEFAULT 0,
    naive_tokens    INTEGER NOT NULL DEFAULT 0,
    message         TEXT NOT NULL DEFAULT '' CHECK (char_length(message) <= 500),
    payload         JSONB NOT NULL DEFAULT '{}'::jsonb,
    ts              TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS horizon_events_incident_idx
    ON horizon_events (incident_id, seq);
CREATE INDEX IF NOT EXISTS horizon_events_type_idx
    ON horizon_events (incident_id, event_type, seq DESC);

-- Episodic memory: the raw output a card was compacted from. Never re-fed to
-- the model; referenced by evidence id. `raw` is truncated by the repository
-- before the INSERT and the truncation is recorded in `extra`.
CREATE TABLE IF NOT EXISTS horizon_observations (
    evidence_id  TEXT PRIMARY KEY,
    incident_id  TEXT NOT NULL,
    tool         TEXT NOT NULL,
    raw          TEXT NOT NULL DEFAULT '',
    card         JSONB,
    extra        JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS horizon_observations_incident_idx
    ON horizon_observations (incident_id, created_at DESC);

-- Semantic memory cards and their incident map. `card` is nullable because the
-- image and the card are written by two different steps, and whichever lands
-- first must not be refused; a row without a card is never listed.
CREATE TABLE IF NOT EXISTS horizon_memory_cards (
    id           TEXT PRIMARY KEY,
    incident_id  TEXT NOT NULL DEFAULT '',
    card         JSONB,
    image        BYTEA,
    image_mime   TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS horizon_memory_cards_updated_idx
    ON horizon_memory_cards (updated_at DESC);
