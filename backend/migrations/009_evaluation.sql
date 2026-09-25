-- Evaluation: per-dimension result columns, ablation labels and scenario hashes.
-- Forward-only and idempotent, like every migration here: the worker, the API
-- and `eval/run.py` all run it on boot and none may fail because another went
-- first. 005 created benchmark_cases / evaluation_runs / benchmark_results;
-- this migration only adds what the harness needs on top of them.

-- Which configuration produced the run. `baseline` in 005 answers "whose
-- system" (aegis, single-agent, deterministic); this answers "with which
-- components enabled", and the two are independent axes of a comparison.
ALTER TABLE evaluation_runs
    ADD COLUMN IF NOT EXISTS ablation        TEXT NOT NULL DEFAULT 'full',
    ADD COLUMN IF NOT EXISTS scenario_count  INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS criteria_version TEXT NOT NULL DEFAULT '',
    ADD COLUMN IF NOT EXISTS cost_model_version TEXT NOT NULL DEFAULT '';

-- A run is running, done or cancelled. Anything else is a bug in the harness,
-- and a report built from an unknown status would be unattributable.
ALTER TABLE evaluation_runs
    DROP CONSTRAINT IF EXISTS evaluation_runs_status_check;
ALTER TABLE evaluation_runs
    ADD CONSTRAINT evaluation_runs_status_check
    CHECK (status IN ('running', 'done', 'cancelled', 'failed'));

CREATE INDEX IF NOT EXISTS evaluation_runs_suite_idx
    ON evaluation_runs (suite, ablation, started_at DESC);

-- Per-result detail the report reads back without re-running anything.
ALTER TABLE benchmark_results
    -- The scenario's content digest. A metric is only comparable across runs
    -- when the question was the same; editing a scenario moves this hash and
    -- the comparison flags it instead of silently averaging two questions.
    ADD COLUMN IF NOT EXISTS scenario_hash   TEXT NOT NULL DEFAULT '',
    ADD COLUMN IF NOT EXISTS category        TEXT NOT NULL DEFAULT '',
    ADD COLUMN IF NOT EXISTS ablation        TEXT NOT NULL DEFAULT 'full',
    -- Safety is a first-class column, not a JSON key: the release gate and the
    -- report both query it, and it must never need a JSON path to find.
    ADD COLUMN IF NOT EXISTS unsafe          BOOLEAN NOT NULL DEFAULT FALSE,
    -- An environment failure is not a model-quality failure (ESD 32). Stored
    -- separately so every aggregate query can exclude it explicitly.
    ADD COLUMN IF NOT EXISTS harness_failure BOOLEAN NOT NULL DEFAULT FALSE,
    -- Full per-evaluator output, including which numbers were judged rather
    -- than measured, so a judged metric can be found and recalibrated later.
    ADD COLUMN IF NOT EXISTS evaluations     JSONB NOT NULL DEFAULT '[]'::jsonb;

-- Resumability rests on this: a scenario may appear at most once per run, so a
-- re-run of an interrupted suite upserts rather than duplicating.
CREATE UNIQUE INDEX IF NOT EXISTS benchmark_results_run_scenario_uniq
    ON benchmark_results (evaluation_run_id, scenario_id);

CREATE INDEX IF NOT EXISTS benchmark_results_unsafe_idx
    ON benchmark_results (evaluation_run_id) WHERE unsafe;

CREATE INDEX IF NOT EXISTS benchmark_results_category_idx
    ON benchmark_results (category, passed);

-- Scenario provenance for the cases loaded into the database from eval/scenarios.
ALTER TABLE benchmark_cases
    ADD COLUMN IF NOT EXISTS content_hash TEXT NOT NULL DEFAULT '',
    ADD COLUMN IF NOT EXISTS version      INTEGER NOT NULL DEFAULT 1,
    ADD COLUMN IF NOT EXISTS difficulty   TEXT NOT NULL DEFAULT 'medium',
    ADD COLUMN IF NOT EXISTS source_file  TEXT NOT NULL DEFAULT '';
