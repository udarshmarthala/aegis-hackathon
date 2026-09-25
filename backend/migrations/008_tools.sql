-- The tool boundary: what ``tool_calls`` needs beyond 004.
-- Forward-only and idempotent, like every migration here: the API and the
-- worker both run this on boot and neither may fail because the other went
-- first. 004 already created ``tool_calls`` with id, agent_run_id, incident_id,
-- server, tool, access, arguments, ok, result_summary, error, duration_ms and
-- created_at, so this file adds only what the invoker additionally records.

-- The scope that authorised the call. Recorded per row rather than looked up
-- from the tool name later, because the tool catalogue changes over releases
-- and an audit has to answer "what permission did this call use at the time".
ALTER TABLE tool_calls
    ADD COLUMN IF NOT EXISTS scope TEXT NOT NULL DEFAULT '';

-- The environment the call ran against and the identity that made it. Without
-- these, "who read production logs during this incident" is unanswerable.
ALTER TABLE tool_calls
    ADD COLUMN IF NOT EXISTS environment TEXT NOT NULL DEFAULT '';
ALTER TABLE tool_calls
    ADD COLUMN IF NOT EXISTS caller TEXT NOT NULL DEFAULT '';

-- Stitches a tool call to the structured logs, the OTel span and the audit row
-- for the same unit of work.
ALTER TABLE tool_calls
    ADD COLUMN IF NOT EXISTS correlation_id TEXT;

-- ``degraded`` is the difference between "the tool found nothing" and "the tool
-- could not look". Both have ok = TRUE; only one of them means the absence of a
-- finding is informative. Collapsing them is an operational bug (PRD 13), so
-- the distinction is stored, not derived from the summary text.
ALTER TABLE tool_calls
    ADD COLUMN IF NOT EXISTS degraded BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE tool_calls
    ADD COLUMN IF NOT EXISTS degraded_reason TEXT NOT NULL DEFAULT '';

-- Evidence produced by this call. The join from a citation back to the exact
-- tool invocation that produced it is what makes an evidence trail replayable.
ALTER TABLE tool_calls
    ADD COLUMN IF NOT EXISTS evidence_ids TEXT[] NOT NULL DEFAULT '{}';

-- "Tool calls for an incident, in order." 004's (incident_id, created_at) index
-- orders ties arbitrarily; ids are ULIDs, so adding id makes the sequence
-- stable across repeated reads - a timeline that reshuffles between refreshes
-- is not a timeline anyone can reason from.
CREATE INDEX IF NOT EXISTS tool_calls_incident_seq_idx
    ON tool_calls (incident_id, created_at, id);

-- Supports the safety query an auditor actually runs: "show me every write-class
-- tool call". Partial, because writes are a small fraction of all calls and a
-- full index on a column that is almost always 'read' earns nothing.
CREATE INDEX IF NOT EXISTS tool_calls_write_idx
    ON tool_calls (created_at DESC) WHERE access = 'write';

-- Supports "which tool calls were degraded during this incident", the query
-- behind the evidence-gap panel.
CREATE INDEX IF NOT EXISTS tool_calls_degraded_idx
    ON tool_calls (incident_id, created_at) WHERE degraded;
