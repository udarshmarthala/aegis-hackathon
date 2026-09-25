-- Code corpus, service->repository mapping and the columns incident memory
-- needs to answer "has this happened before?".
--
-- Additive, forward-only and idempotent. 005 already created
-- `retrieval_documents`, `incident_memories` and the `vector` extension;
-- nothing here redefines them.

CREATE EXTENSION IF NOT EXISTS vector;

-- --------------------------------------------------------------------------
-- retrieval_documents: dedup and citation columns 005 did not carry.
-- --------------------------------------------------------------------------

-- Ingestion re-runs on every doc sync. Without a content hash the corpus grows
-- by one duplicate row per sync and lexical scores drift, because the same
-- passage is counted repeatedly in the same ranking.
ALTER TABLE retrieval_documents
    ADD COLUMN IF NOT EXISTS content_hash   TEXT NOT NULL DEFAULT '';
ALTER TABLE retrieval_documents
    ADD COLUMN IF NOT EXISTS provenance_uri TEXT NOT NULL DEFAULT '';
ALTER TABLE retrieval_documents
    ADD COLUMN IF NOT EXISTS chunk_index    INTEGER NOT NULL DEFAULT 0;
ALTER TABLE retrieval_documents
    ADD COLUMN IF NOT EXISTS updated_at     TIMESTAMPTZ NOT NULL DEFAULT now();

-- Partial: rows ingested before this migration carry an empty hash and must
-- not collide with one another.
CREATE UNIQUE INDEX IF NOT EXISTS retrieval_documents_hash_key
    ON retrieval_documents (content_hash) WHERE content_hash <> '';
CREATE INDEX IF NOT EXISTS retrieval_documents_type_idx
    ON retrieval_documents (doc_type, created_at DESC);
-- Service scoping is applied as a filter before ranking, so it needs an index
-- of its own; a sequential scan here would dominate every hybrid query.
CREATE INDEX IF NOT EXISTS retrieval_documents_services_idx
    ON retrieval_documents USING GIN (services);

-- --------------------------------------------------------------------------
-- service -> repository mapping
-- --------------------------------------------------------------------------

-- The first narrowing stage of code localisation. Without it, the only route
-- from a failing service to source is to hand a model every repository, which
-- is precisely the unbounded behaviour CLAUDE.md section 4 forbids.
CREATE TABLE IF NOT EXISTS service_repositories (
    service_id   TEXT NOT NULL,
    repo         TEXT NOT NULL,          -- "owner/name"
    default_ref  TEXT NOT NULL DEFAULT 'main',
    path_prefix  TEXT NOT NULL DEFAULT '',
    language     TEXT NOT NULL DEFAULT '',
    -- Several services can share a monorepo; rank decides which mapping is
    -- consulted first when the per-incident repository budget is tight.
    rank         INTEGER NOT NULL DEFAULT 100,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (service_id, repo, path_prefix)
);
CREATE INDEX IF NOT EXISTS service_repositories_repo_idx
    ON service_repositories (repo);

-- --------------------------------------------------------------------------
-- code corpus
-- --------------------------------------------------------------------------

-- Chunks of repository content. One row per chunk, never one per repository:
-- the retriever reads ranked rows, so a large repo costs index pages rather
-- than process memory. start_line/end_line are stored because a citation that
-- cannot be resolved back to real lines is not evidence.
CREATE TABLE IF NOT EXISTS code_documents (
    id            TEXT PRIMARY KEY,
    repo          TEXT NOT NULL,
    ref           TEXT NOT NULL,         -- commit sha the content was read at
    path          TEXT NOT NULL,
    symbol        TEXT,                  -- function/class when the chunk is one
    kind          TEXT NOT NULL DEFAULT 'file'
                  CHECK (kind IN ('file', 'symbol', 'test', 'config')),
    language      TEXT NOT NULL DEFAULT '',
    start_line    INTEGER NOT NULL DEFAULT 1 CHECK (start_line >= 1),
    end_line      INTEGER NOT NULL DEFAULT 1 CHECK (end_line >= start_line),
    content       TEXT NOT NULL,
    content_hash  TEXT NOT NULL,
    service_id    TEXT,
    metadata      JSONB NOT NULL DEFAULT '{}'::jsonb,
    embedding     vector(1536),
    -- Generated rather than written by the ingester: a stored generated column
    -- cannot be forgotten on an upsert path, and a chunk with a stale tsv is
    -- invisible to lexical search while still looking ingested.
    tsv           tsvector GENERATED ALWAYS AS (
                      to_tsvector(
                          'english',
                          coalesce(symbol, '') || ' ' || coalesce(path, '')
                          || ' ' || coalesce(content, '')
                      )
                  ) STORED,
    indexed_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Re-ingesting an unchanged file must be a no-op. Keyed on content rather than
-- on ref, so a new commit that touched other files does not re-insert this one.
CREATE UNIQUE INDEX IF NOT EXISTS code_documents_dedup_key
    ON code_documents (repo, path, start_line, content_hash);
CREATE INDEX IF NOT EXISTS code_documents_tsv_idx
    ON code_documents USING GIN (tsv);
CREATE INDEX IF NOT EXISTS code_documents_vec_idx
    ON code_documents USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);
CREATE INDEX IF NOT EXISTS code_documents_service_idx
    ON code_documents (service_id);
CREATE INDEX IF NOT EXISTS code_documents_repo_path_idx
    ON code_documents (repo, path);
-- Symbol lookup is case-insensitive because symptom terms arrive from log and
-- alert text, which does not preserve the declaration's casing.
CREATE INDEX IF NOT EXISTS code_documents_symbol_idx
    ON code_documents (lower(symbol)) WHERE symbol IS NOT NULL;
CREATE INDEX IF NOT EXISTS code_documents_kind_idx
    ON code_documents (kind);

-- --------------------------------------------------------------------------
-- incident_memories: recurrence signature and the rest of the record
-- --------------------------------------------------------------------------

-- 005 has `fingerprint`; these are the inputs that produced it. Storing them
-- means a signature-scheme change can be re-derived rather than rebuilt.
ALTER TABLE incident_memories
    ADD COLUMN IF NOT EXISTS signature_version    TEXT NOT NULL DEFAULT 'v1';
ALTER TABLE incident_memories
    ADD COLUMN IF NOT EXISTS symptom_normalised   TEXT NOT NULL DEFAULT '';
ALTER TABLE incident_memories
    ADD COLUMN IF NOT EXISTS cause_category       TEXT NOT NULL DEFAULT '';
ALTER TABLE incident_memories
    ADD COLUMN IF NOT EXISTS contributing_factors TEXT[] NOT NULL DEFAULT '{}';
ALTER TABLE incident_memories
    ADD COLUMN IF NOT EXISTS timeline             JSONB NOT NULL DEFAULT '[]'::jsonb;
ALTER TABLE incident_memories
    ADD COLUMN IF NOT EXISTS related_commits      TEXT[] NOT NULL DEFAULT '{}';
ALTER TABLE incident_memories
    ADD COLUMN IF NOT EXISTS related_deployments  TEXT[] NOT NULL DEFAULT '{}';
ALTER TABLE incident_memories
    ADD COLUMN IF NOT EXISTS follow_ups           TEXT[] NOT NULL DEFAULT '{}';
-- Written only from a VerificationResult. A memory whose fix was never verified
-- is a hypothesis, and hypotheses must not be recalled later as fact.
ALTER TABLE incident_memories
    ADD COLUMN IF NOT EXISTS verification_passed  BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE incident_memories
    ADD COLUMN IF NOT EXISTS diagnosis_confidence DOUBLE PRECISION NOT NULL DEFAULT 0;
ALTER TABLE incident_memories
    ADD COLUMN IF NOT EXISTS evidence_ids         TEXT[] NOT NULL DEFAULT '{}';
ALTER TABLE incident_memories
    ADD COLUMN IF NOT EXISTS first_seen_at        TIMESTAMPTZ NOT NULL DEFAULT now();
ALTER TABLE incident_memories
    ADD COLUMN IF NOT EXISTS last_seen_at         TIMESTAMPTZ NOT NULL DEFAULT now();
ALTER TABLE incident_memories
    ADD COLUMN IF NOT EXISTS embedding            vector(1536);
ALTER TABLE incident_memories
    ADD COLUMN IF NOT EXISTS tsv                  tsvector GENERATED ALWAYS AS (
        to_tsvector(
            'english',
            coalesce(title, '') || ' ' || coalesce(symptoms, '')
            || ' ' || coalesce(root_cause, '')
        )
    ) STORED;

-- Occurrence counting is an upsert on the signature, so the signature has to be
-- unique. Partial because rows predating the scheme carry an empty string.
CREATE UNIQUE INDEX IF NOT EXISTS incident_memories_fingerprint_key
    ON incident_memories (fingerprint) WHERE fingerprint <> '';
CREATE INDEX IF NOT EXISTS incident_memories_tsv_idx
    ON incident_memories USING GIN (tsv);
CREATE INDEX IF NOT EXISTS incident_memories_vec_idx
    ON incident_memories USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);
CREATE INDEX IF NOT EXISTS incident_memories_services_idx
    ON incident_memories USING GIN (affected_services);
-- Drives the "Recurring Failures" surface, which is always time-windowed.
CREATE INDEX IF NOT EXISTS incident_memories_recurrence_idx
    ON incident_memories (last_seen_at DESC, occurrences DESC);
