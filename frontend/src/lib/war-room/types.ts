/**
 * War-room wire types.
 *
 * These mirror `backend/src/aegis/domain/horizon.py` (pydantic
 * `model_dump(mode="json")`) and the `/v1/war-room/*` response shapes. They are
 * TypeScript declarations rather than runtime schemas on purpose: the stream is
 * append-only and a frame that carries one unexpected field must not be thrown
 * away wholesale. The reducer reads every field defensively instead, and the
 * few that drive safety-relevant rendering (health values, approval payloads)
 * are normalised explicitly in `reducer.ts`.
 */

export const HORIZON_PHASES = [
  'IDLE',
  'DETECTING',
  'INVESTIGATING',
  'DIAGNOSING',
  'PLANNING',
  'AWAITING_APPROVAL',
  'EXECUTING',
  'VERIFYING',
  'REASSESSING',
  'RESOLVED',
  'ESCALATED',
] as const;
export type HorizonPhase = (typeof HORIZON_PHASES)[number];

/**
 * Which path actually produced a card, event or decision. Kept open (`string`)
 * at the edge because a new backend label must still render - as itself - rather
 * than be dropped; `SOURCE_LABELS` in the UI knows the closed set.
 */
export const KNOWN_SOURCES = [
  'bedrock',
  'gemini',
  'scripted',
  'rule',
  'rawtree',
  'postgres',
  'prometheus',
  'runtime',
  'nimble',
  'fixture',
  'flux',
  'zscore',
  'memory',
  'tool',
  'system',
] as const;
export type KnownSource = (typeof KNOWN_SOURCES)[number];
export type Source = KnownSource | (string & {});

export type GoalStatus = 'pending' | 'active' | 'done' | 'failed' | 'skipped';

export interface Goal {
  id: string;
  title: string;
  status: GoalStatus;
  parent_id: string | null;
}

export interface EvidenceCard {
  id: string;
  step: number;
  tool: string;
  source: Source;
  origin: Source;
  claim: string;
  supports: string[];
  refutes: string[];
  weight: number;
  raw_ref: string;
  url: string | null;
  tokens_raw: number;
  tokens_card: number;
  pinned: boolean;
}

export interface HorizonHypothesis {
  id: string;
  statement: string;
  supporting: string[];
  refuting: string[];
  confidence: number;
  history: number[];
  suggested_action: string | null;
}

export interface MemoryCard {
  id: string;
  incident_id: string;
  symptoms: string;
  root_cause: string;
  failed_actions: string[];
  successful_action: string | null;
  recovery_s: number | null;
  lesson: string;
  image_url: string | null;
  image_status: 'pending' | 'ready' | 'unavailable' | (string & {});
  image_reason: string;
  source: Source;
}

export interface ActionAttempt {
  action_id: string | null;
  action_type: string;
  target: string;
  arguments: Record<string, unknown>;
  cycle: number;
  outcome: string;
  detail: string;
}

export interface TokenStats {
  context_tokens: number;
  naive_tokens: number;
  cache_read_tokens: number;
  cache_hits: number;
  fallbacks_used: number;
  compacted_raw_tokens: number;
  compacted_card_tokens: number;
}

export interface HorizonState {
  run_id: string;
  incident_id: string;
  service: string;
  symptom: string;
  step: number;
  phase: HorizonPhase;
  remediation_cycle: number;
  goals: Goal[];
  hypotheses: HorizonHypothesis[];
  evidence: EvidenceCard[];
  discarded: string[];
  memory: MemoryCard[];
  notes: string[];
  observe_tools_run: string[];
  actions: ActionAttempt[];
  excluded_actions: string[];
  pending_action_id: string | null;
  brain_source: Source;
  tokens: TokenStats;
  escalation_reason: string | null;
  started_at: string | null;
  updated_at: string | null;
}

export type HorizonEventType =
  | 'phase_changed'
  | 'step_started'
  | 'step_completed'
  | 'brain_decision'
  | 'brain_fallback'
  | 'tool_called'
  | 'evidence_added'
  | 'evidence_discarded'
  | 'evidence_recalled'
  | 'hypotheses_updated'
  | 'goal_updated'
  | 'note_written'
  | 'self_edit_rejected'
  | 'memory_recalled'
  | 'action_proposed'
  | 'approval_required'
  | 'approval_resolved'
  | 'action_executed'
  | 'verification_result'
  | 'rawtree_query'
  | 'heartbeat_anomaly'
  | 'checkpoint_saved'
  | 'resumed'
  | 'memory_card_written'
  | 'incident_map'
  | 'resolved'
  | 'escalated';

export interface HorizonEvent {
  ts: string;
  run_id: string;
  incident_id: string;
  step: number;
  phase: HorizonPhase;
  event_type: HorizonEventType | (string & {});
  tool: string | null;
  status: 'ok' | 'error' | 'rejected' | 'degraded' | (string & {});
  duration_ms: number;
  source: Source;
  context_tokens: number;
  naive_tokens: number;
  message: string;
  payload: Record<string, unknown>;
}

/** One entry of `/events` and the SSE `horizon` frame. */
export interface SeqEvent {
  seq: number;
  event: HorizonEvent;
}

export interface WarRoomIncident {
  id: string;
  title: string;
  severity: string;
  state: string;
  service: string;
}

export interface IntegrationStatus {
  configured: boolean;
  reason: string;
}

export interface WarRoomStats {
  compression_ratio: number;
  cache_hits: number;
  cache_read_tokens: number;
  fallbacks_used: number;
  steps: number;
  context_tokens: number;
  naive_tokens: number;
}

/** `GET /v1/war-room/state` and the SSE `snapshot` frame. */
export interface WarRoomState {
  mode: 'live' | 'scripted' | (string & {});
  run: HorizonState | null;
  incident: WarRoomIncident | null;
  brain: Record<string, unknown>;
  integrations: Record<string, IntegrationStatus>;
  memory_cards: MemoryCard[];
  stats: WarRoomStats;
  last_seq: number;
}

export interface ContextPoint {
  step: number;
  context_tokens: number;
  naive_tokens: number;
}

export type HealthServiceName = 'gateway' | 'checkout' | 'payment' | 'db-pool';
export type HealthStatus = 'healthy' | 'degraded' | 'critical' | 'unknown';

export interface HealthService {
  service: HealthServiceName | (string & {});
  p99_ms: number | null;
  error_rate: number | null;
  pool_utilisation: number | null;
  version: string | null;
  status: HealthStatus;
  source: 'prometheus' | 'unavailable' | (string & {});
  series: { p99_ms: number[]; error_rate: number[]; pool_utilisation: number[] };
}

/** `GET /v1/war-room/health` and the SSE `health` frame. */
export interface HealthPayload {
  ts: string;
  services: HealthService[];
}

export interface RawTreeQuery {
  ts: string;
  name: string;
  sql: string;
  rows: number;
  source: Source;
  duration_ms: number;
  error: string | null;
}

/** The `approval_required` payload, normalised. */
export interface PendingApprovalView {
  seq: number;
  approval_id: string;
  action_id: string;
  action_type: string;
  target: string;
  reason: string;
  confidence: number | null;
  evidence_ids: string[];
  risk_tier: number | null;
}

/** `POST /v1/war-room/inject|kill-worker|reset`. */
export interface ControlResponse {
  ok: boolean;
  detail: string;
  deployment_id?: string | null;
}
