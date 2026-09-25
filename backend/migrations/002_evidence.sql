-- Evidence, hypotheses and the reasoning record.

CREATE TABLE IF NOT EXISTS evidence_items (
    id                TEXT PRIMARY KEY,
    incident_id       TEXT NOT NULL REFERENCES incidents(id) ON DELETE CASCADE,
    source            TEXT NOT NULL,
    source_type       TEXT NOT NULL,
    evidence_type     TEXT NOT NULL,
    -- VALIDATED | UNVALIDATED | REFUTED | SOURCE_UNAVAILABLE.
    -- SOURCE_UNAVAILABLE is how "we could not look" stays distinguishable from
    -- "we looked and found nothing" all the way into the UI (PRD 13).
    status            TEXT NOT NULL DEFAULT 'UNVALIDATED',
    trust_class       TEXT NOT NULL DEFAULT 'TIER_D',
    resource_id       TEXT,
    summary           TEXT NOT NULL DEFAULT '',
    structured_value  JSONB NOT NULL DEFAULT '{}'::jsonb,
    content           TEXT,
    content_untrusted BOOLEAN NOT NULL DEFAULT FALSE,
    provenance_uri    TEXT NOT NULL DEFAULT '',
    content_hash      TEXT NOT NULL DEFAULT '',
    observed_at       TIMESTAMPTZ,
    retrieved_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS evidence_incident_idx
    ON evidence_items (incident_id, retrieved_at DESC);
CREATE INDEX IF NOT EXISTS evidence_incident_type_idx
    ON evidence_items (incident_id, evidence_type);
-- Cheap dedup: the same observation fetched twice yields one row.
CREATE UNIQUE INDEX IF NOT EXISTS evidence_dedup_idx
    ON evidence_items (incident_id, content_hash)
    WHERE content_hash <> '';

CREATE TABLE IF NOT EXISTS hypotheses (
    id                 TEXT PRIMARY KEY,
    incident_id        TEXT NOT NULL REFERENCES incidents(id) ON DELETE CASCADE,
    label              TEXT NOT NULL,
    statement          TEXT NOT NULL,
    state              TEXT NOT NULL DEFAULT 'PROPOSED',
    confidence         DOUBLE PRECISION NOT NULL DEFAULT 0
        CHECK (confidence >= 0 AND confidence <= 1),
    supporting         TEXT[] NOT NULL DEFAULT '{}',
    contradicting      TEXT[] NOT NULL DEFAULT '{}',
    missing            TEXT[] NOT NULL DEFAULT '{}',
    predictions        JSONB NOT NULL DEFAULT '[]'::jsonb,
    affected_services  TEXT[] NOT NULL DEFAULT '{}',
    rejected_reason    TEXT,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (incident_id, label)
);
CREATE INDEX IF NOT EXISTS hypotheses_incident_idx
    ON hypotheses (incident_id, confidence DESC);

-- Confidence over time, so the UI can show how belief moved (UX spec 23).
CREATE TABLE IF NOT EXISTS hypothesis_confidence_history (
    id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    hypothesis_id TEXT NOT NULL REFERENCES hypotheses(id) ON DELETE CASCADE,
    confidence    DOUBLE PRECISION NOT NULL,
    reason        TEXT NOT NULL DEFAULT '',
    recorded_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS hyp_conf_hist_idx
    ON hypothesis_confidence_history (hypothesis_id, recorded_at);

CREATE TABLE IF NOT EXISTS diagnoses (
    id                       TEXT PRIMARY KEY,
    incident_id              TEXT NOT NULL REFERENCES incidents(id) ON DELETE CASCADE,
    abstained                BOOLEAN NOT NULL,
    statement                TEXT NOT NULL,
    root_cause_category      TEXT,
    confidence               DOUBLE PRECISION NOT NULL DEFAULT 0,
    selected_hypothesis_id   TEXT,
    supporting_evidence      TEXT[] NOT NULL DEFAULT '{}',
    causal_path              TEXT[] NOT NULL DEFAULT '{}',
    affected_services        TEXT[] NOT NULL DEFAULT '{}',
    contributing_factors     TEXT[] NOT NULL DEFAULT '{}',
    rejected_alternatives    TEXT[] NOT NULL DEFAULT '{}',
    missing_evidence         TEXT[] NOT NULL DEFAULT '{}',
    uncertainty              TEXT NOT NULL DEFAULT '',
    confidence_model_version TEXT NOT NULL DEFAULT '1.0.0',
    created_at               TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS diagnoses_incident_idx ON diagnoses (incident_id, created_at DESC);

-- Sources that could not be consulted. A first-class record, not a log line.
CREATE TABLE IF NOT EXISTS evidence_gaps (
    id           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    incident_id  TEXT NOT NULL REFERENCES incidents(id) ON DELETE CASCADE,
    source       TEXT NOT NULL,
    source_type  TEXT NOT NULL,
    reason       TEXT NOT NULL,
    affects      TEXT[] NOT NULL DEFAULT '{}',
    attempted_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS evidence_gaps_incident_idx ON evidence_gaps (incident_id);
