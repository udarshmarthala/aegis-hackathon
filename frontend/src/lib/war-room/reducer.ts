import type {
  ContextPoint, EvidenceCard, Goal, HealthPayload, HorizonEvent, HorizonHypothesis,
  HorizonPhase, HorizonState, IntegrationStatus, MemoryCard, PendingApprovalView,
  RawTreeQuery, SeqEvent, Source, WarRoomIncident, WarRoomState, WarRoomStats,
} from './types';
import { HORIZON_PHASES } from './types';

/**
 * The war-room model and the only code that changes it.
 *
 * Pure on purpose: the page receives the same facts from three places (the
 * initial reads, `snapshot` frames and `horizon` frames), in an order the
 * network chooses, possibly twice after a reconnect. Keeping the merge rules in
 * one side-effect-free function is what lets them be tested exhaustively rather
 * than trusted.
 *
 * Every collection is bounded. A demo that runs for an hour must not leak a
 * list into the tab until the projector stutters.
 */

export const MAX_EVENTS = 300;
export const MAX_CONTEXT_POINTS = 400;
export const MAX_RAWTREE_QUERIES = 40;
/** Discarded cards stay on screen, faded, so the eviction is visible - but only a few. */
export const MAX_DISCARDED_VISIBLE = 4;

export type ConnectionState = 'connecting' | 'live' | 'reconnecting' | 'offline';

export interface EvidenceView {
  card: EvidenceCard;
  /** Set when the card was evicted to storage; the UI fades it and says where it went. */
  discarded: boolean;
}

export interface BrainBadge {
  source: Source;
  model: string;
  /** Where the badge came from, so a stale fallback is not presented as a live decision. */
  from: 'brain_decision' | 'state';
}

export interface WarRoomModel {
  mode: string | null;
  run: HorizonState | null;
  incident: WarRoomIncident | null;
  brain: Record<string, unknown>;
  integrations: Record<string, IntegrationStatus>;
  memoryCards: MemoryCard[];
  stats: WarRoomStats | null;
  lastSeq: number;
  /** Latest first. */
  events: SeqEvent[];
  evidence: EvidenceView[];
  phase: HorizonPhase;
  /** Phases this run has entered, so the chain can show where it has been. */
  visited: HorizonPhase[];
  contextPoints: ContextPoint[];
  rawtreeQueries: RawTreeQuery[];
  health: HealthPayload | null;
  brainBadge: BrainBadge | null;
  approval: PendingApprovalView | null;
  /** Approval ids the operator has already decided or the backend resolved. */
  settledApprovals: string[];
  approvalDismissed: boolean;
  connection: ConnectionState;
}

export const initialModel: WarRoomModel = {
  mode: null,
  run: null,
  incident: null,
  brain: {},
  integrations: {},
  memoryCards: [],
  stats: null,
  lastSeq: 0,
  events: [],
  evidence: [],
  phase: 'IDLE',
  visited: [],
  contextPoints: [],
  rawtreeQueries: [],
  health: null,
  brainBadge: null,
  approval: null,
  settledApprovals: [],
  approvalDismissed: false,
  connection: 'connecting',
};

export type WarRoomAction =
  | {
      type: 'init';
      state: WarRoomState;
      events: SeqEvent[];
      points: ContextPoint[];
      health: HealthPayload | null;
      queries: RawTreeQuery[];
    }
  | { type: 'snapshot'; state: WarRoomState }
  | { type: 'horizon'; frame: SeqEvent }
  | { type: 'health'; health: HealthPayload }
  | { type: 'connection'; state: ConnectionState }
  | { type: 'approval-dismissed' }
  | { type: 'approval-reopened' }
  | { type: 'approval-settled'; approvalId: string };

/* ------------------------------------------------------------ narrowing -- */

type Json = Record<string, unknown>;

function isObject(value: unknown): value is Json {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function str(value: unknown): string | null {
  return typeof value === 'string' && value.length > 0 ? value : null;
}

function finite(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) ? value : null;
}

function strings(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((v): v is string => typeof v === 'string') : [];
}

export function isPhase(value: unknown): value is HorizonPhase {
  return typeof value === 'string' && (HORIZON_PHASES as readonly string[]).includes(value);
}

function looksLikeCard(value: unknown): value is EvidenceCard {
  return isObject(value) && typeof value.id === 'string' && typeof value.claim === 'string';
}

function looksLikeHypothesis(value: unknown): value is HorizonHypothesis {
  return isObject(value) && typeof value.id === 'string' && typeof value.statement === 'string';
}

function looksLikeGoal(value: unknown): value is Goal {
  return isObject(value) && typeof value.id === 'string' && typeof value.title === 'string';
}

function looksLikeMemoryCard(value: unknown): value is MemoryCard {
  return isObject(value) && typeof value.id === 'string' && typeof value.incident_id === 'string';
}

/** A frame is only applied when it carries the fields the reducer relies on. */
export function isSeqEvent(value: unknown): value is SeqEvent {
  if (!isObject(value) || finite(value.seq) === null || !isObject(value.event)) return false;
  const event = value.event;
  return typeof event.event_type === 'string' && typeof event.run_id === 'string';
}

/* -------------------------------------------------------------- helpers -- */

function withVisited(visited: HorizonPhase[], phase: HorizonPhase): HorizonPhase[] {
  return visited.includes(phase) ? visited : [...visited, phase];
}

function upsertPoint(points: ContextPoint[], point: ContextPoint): ContextPoint[] {
  const without = points.filter((p) => p.step !== point.step);
  const next = [...without, point].sort((a, b) => a.step - b.step);
  return next.length > MAX_CONTEXT_POINTS ? next.slice(next.length - MAX_CONTEXT_POINTS) : next;
}

/** Keep every live card, and only the most recent few discarded ones. */
function boundEvidence(views: EvidenceView[]): EvidenceView[] {
  let discardedSeen = 0;
  const kept: EvidenceView[] = [];
  for (let i = views.length - 1; i >= 0; i -= 1) {
    const view = views[i];
    if (!view) continue;
    if (view.discarded) {
      discardedSeen += 1;
      if (discardedSeen > MAX_DISCARDED_VISIBLE) continue;
    }
    kept.unshift(view);
  }
  return kept;
}

/**
 * Rebuild the evidence list from a checkpointed run while keeping cards the
 * stream already showed being evicted. The snapshot knows only the in-context
 * set; without this merge a snapshot arriving a moment after an eviction would
 * make the card vanish instead of visibly leaving for RawTree.
 */
function evidenceFromRun(run: HorizonState, previous: EvidenceView[]): EvidenceView[] {
  const live = new Set(run.evidence.map((c) => c.id));
  const discardedIds = new Set(run.discarded ?? []);
  const carried = previous.filter(
    (v) => !live.has(v.card.id) && (v.discarded || discardedIds.has(v.card.id)),
  );
  const views: EvidenceView[] = [
    ...carried.map((v) => ({ card: v.card, discarded: true })),
    ...run.evidence.map((card) => ({ card, discarded: false })),
  ];
  return boundEvidence(views);
}

function normaliseApproval(seq: number, payload: Json): PendingApprovalView | null {
  const approvalId = str(payload.approval_id);
  if (!approvalId) return null;
  const tier = finite(payload.risk_tier);
  return {
    seq,
    approval_id: approvalId,
    action_id: str(payload.action_id) ?? '',
    action_type: str(payload.action_type) ?? 'unknown action',
    target: str(payload.target) ?? 'unknown target',
    reason: typeof payload.reason === 'string' ? payload.reason : '',
    confidence: finite(payload.confidence),
    evidence_ids: strings(payload.evidence_ids),
    risk_tier: tier,
  };
}

function normaliseQuery(event: HorizonEvent): RawTreeQuery {
  const p = event.payload ?? {};
  const rows = Array.isArray(p.rows) ? p.rows.length : (finite(p.rows) ?? finite(p.row_count) ?? 0);
  return {
    ts: event.ts,
    name: str(p.name) ?? event.tool ?? 'query',
    sql: typeof p.sql === 'string' ? p.sql : '',
    rows,
    source: str(p.source) ?? event.source,
    duration_ms: finite(p.duration_ms) ?? event.duration_ms ?? 0,
    error: str(p.error) ?? (event.status === 'error' ? event.message || 'query failed' : null),
  };
}

/** Evidence ids named by an `evidence_discarded` payload, whichever key carries them. */
function discardedIds(payload: Json): string[] {
  const ids = [
    ...strings(payload.ids),
    ...strings(payload.evidence_ids),
  ];
  for (const key of ['evidence_id', 'card_id', 'id']) {
    const value = str(payload[key]);
    if (value) ids.push(value);
  }
  if (isObject(payload.card)) {
    const value = str(payload.card.id);
    if (value) ids.push(value);
  }
  return ids;
}

function freshRunModel(model: WarRoomModel): WarRoomModel {
  // A new run id means a new incident on stage: per-run derived views start
  // clean, while the event log and health (which are not per-run) persist.
  return {
    ...model,
    evidence: [],
    visited: [],
    contextPoints: [],
    approval: null,
    approvalDismissed: false,
    brainBadge: null,
  };
}

/* ------------------------------------------------------------- snapshot -- */

function applySnapshot(model: WarRoomModel, state: WarRoomState): WarRoomModel {
  let next = model;
  const run = state.run ?? null;

  // A store whose sequence went backwards has been reset (or the API process
  // restarted onto a fresh in-memory store). Keeping the old high-water mark
  // would make every new event look like a duplicate and freeze the page.
  const lastSeq = finite(state.last_seq) ?? 0;
  if (lastSeq < model.lastSeq) {
    next = { ...next, lastSeq, events: [] };
  }

  if (run && model.run && run.run_id !== model.run.run_id) next = freshRunModel(next);

  const phase: HorizonPhase = run && isPhase(run.phase) ? run.phase : 'IDLE';

  let approval = next.approval;
  // The snapshot is authoritative about whether the run is still waiting, but
  // only once it is at least as new as the approval request itself.
  if (approval && phase !== 'AWAITING_APPROVAL' && lastSeq >= approval.seq) approval = null;

  next = {
    ...next,
    mode: typeof state.mode === 'string' ? state.mode : null,
    run,
    incident: state.incident ?? null,
    brain: isObject(state.brain) ? state.brain : {},
    integrations: isObject(state.integrations)
      ? (state.integrations as Record<string, IntegrationStatus>)
      : {},
    memoryCards: Array.isArray(state.memory_cards) ? state.memory_cards : [],
    stats: state.stats ?? null,
    lastSeq: Math.max(next.lastSeq, lastSeq),
    phase,
    visited: run ? withVisited(next.visited, phase) : [],
    evidence: run ? evidenceFromRun(run, next.evidence) : [],
    approval,
    brainBadge:
      next.brainBadge ??
      (run ? { source: run.brain_source, model: '', from: 'state' as const } : null),
  };

  if (run && run.tokens && run.tokens.context_tokens > 0) {
    next = {
      ...next,
      contextPoints: upsertPoint(next.contextPoints, {
        step: run.step,
        context_tokens: run.tokens.context_tokens,
        naive_tokens: run.tokens.naive_tokens,
      }),
    };
  }
  return next;
}

/* -------------------------------------------------------------- horizon -- */

function applyEventToRun(run: HorizonState, event: HorizonEvent): HorizonState {
  const p = event.payload ?? {};
  let next: HorizonState = { ...run, step: Math.max(run.step, event.step) };
  if (isPhase(event.phase)) next.phase = event.phase;

  switch (event.event_type) {
    case 'phase_changed': {
      const to = p.to ?? p.phase;
      if (isPhase(to)) next.phase = to;
      break;
    }
    case 'hypotheses_updated': {
      const list = Array.isArray(p.hypotheses) ? p.hypotheses.filter(looksLikeHypothesis) : null;
      if (list) next = { ...next, hypotheses: list };
      break;
    }
    case 'goal_updated': {
      if (Array.isArray(p.goals)) {
        next = { ...next, goals: p.goals.filter(looksLikeGoal) };
        break;
      }
      const goal = looksLikeGoal(p.goal) ? p.goal : null;
      if (goal) {
        const exists = next.goals.some((g) => g.id === goal.id);
        next = {
          ...next,
          goals: exists
            ? next.goals.map((g) => (g.id === goal.id ? { ...g, ...goal } : g))
            : [...next.goals, goal],
        };
        break;
      }
      const goalId = str(p.goal_id) ?? str(p.id);
      const status = str(p.status);
      if (goalId && status) {
        next = {
          ...next,
          goals: next.goals.map((g) =>
            g.id === goalId ? { ...g, status: status as Goal['status'] } : g,
          ),
        };
      }
      break;
    }
    case 'evidence_added':
    case 'evidence_recalled': {
      const card = looksLikeCard(p.card) ? p.card : looksLikeCard(p) ? (p as unknown as EvidenceCard) : null;
      if (card && !next.evidence.some((c) => c.id === card.id)) {
        next = {
          ...next,
          evidence: [...next.evidence, card],
          discarded: next.discarded.filter((id) => id !== card.id),
        };
      }
      break;
    }
    case 'evidence_discarded': {
      const ids = new Set(discardedIds(p));
      if (ids.size) {
        next = {
          ...next,
          evidence: next.evidence.filter((c) => !ids.has(c.id)),
          discarded: [...next.discarded.filter((id) => !ids.has(id)), ...ids],
        };
      }
      break;
    }
    case 'escalated': {
      const reason = str(p.reason) ?? (event.message || null);
      next = { ...next, escalation_reason: reason };
      break;
    }
    default:
      break;
  }

  if (event.context_tokens > 0) {
    next = {
      ...next,
      tokens: {
        ...next.tokens,
        context_tokens: event.context_tokens,
        naive_tokens: event.naive_tokens,
      },
    };
  }
  if (event.event_type === 'brain_decision' && str(event.source)) {
    next = { ...next, brain_source: event.source };
  }
  return next;
}

/** A skeleton run, for when the stream starts a run before any snapshot described it. */
function runFromEvent(event: HorizonEvent): HorizonState {
  return {
    run_id: event.run_id,
    incident_id: event.incident_id,
    service: '',
    symptom: '',
    step: event.step,
    phase: isPhase(event.phase) ? event.phase : 'DETECTING',
    remediation_cycle: 0,
    goals: [],
    hypotheses: [],
    evidence: [],
    discarded: [],
    memory: [],
    notes: [],
    observe_tools_run: [],
    actions: [],
    excluded_actions: [],
    pending_action_id: null,
    brain_source: 'scripted',
    tokens: {
      context_tokens: 0, naive_tokens: 0, cache_read_tokens: 0, cache_hits: 0,
      fallbacks_used: 0, compacted_raw_tokens: 0, compacted_card_tokens: 0,
    },
    escalation_reason: null,
    started_at: event.ts,
    updated_at: event.ts,
  };
}

function applyHorizon(model: WarRoomModel, frame: SeqEvent): WarRoomModel {
  // Dedupe: a reconnect replays from Last-Event-ID and the initial /events read
  // overlaps the first live frames. Sequence numbers are monotonic per store,
  // so anything at or below the high-water mark has already been applied.
  if (frame.seq <= model.lastSeq) return model;

  const event = frame.event;
  let next: WarRoomModel = model;

  if (model.run && event.run_id && event.run_id !== model.run.run_id) {
    next = freshRunModel(next);
    next = { ...next, run: runFromEvent(event) };
  }

  const baseRun = next.run ?? runFromEvent(event);
  const run = applyEventToRun(baseRun, event);

  // Evidence view: new cards appear, evicted cards stay visible but faded.
  let evidence = next.evidence;
  if (event.event_type === 'evidence_added' || event.event_type === 'evidence_recalled') {
    const added = run.evidence.filter((c) => !evidence.some((v) => v.card.id === c.id && !v.discarded));
    evidence = [
      ...evidence.filter((v) => !added.some((c) => c.id === v.card.id)),
      ...added.map((card) => ({ card, discarded: false })),
    ];
  } else if (event.event_type === 'evidence_discarded') {
    const ids = new Set(discardedIds(event.payload ?? {}));
    evidence = evidence.map((v) => (ids.has(v.card.id) ? { ...v, discarded: true } : v));
  }

  let approval = next.approval;
  let approvalDismissed = next.approvalDismissed;
  let settled = next.settledApprovals;
  if (event.event_type === 'approval_required') {
    const view = normaliseApproval(frame.seq, event.payload ?? {});
    if (view && !settled.includes(view.approval_id)) {
      approval = view;
      approvalDismissed = false;
    }
  } else if (event.event_type === 'approval_resolved') {
    const id = str(event.payload?.approval_id);
    if (id) settled = [...settled.filter((s) => s !== id), id].slice(-20);
    if (approval && (!id || id === approval.approval_id)) approval = null;
  }

  let memoryCards = next.memoryCards;
  if (event.event_type === 'memory_card_written' || event.event_type === 'memory_recalled') {
    const incoming = [
      ...(looksLikeMemoryCard(event.payload?.card) ? [event.payload.card] : []),
      ...(Array.isArray(event.payload?.cards) ? event.payload.cards.filter(looksLikeMemoryCard) : []),
    ];
    for (const card of incoming) {
      memoryCards = [...memoryCards.filter((m) => m.id !== card.id), card];
    }
    memoryCards = memoryCards.slice(-6);
  } else if (event.event_type === 'incident_map') {
    const p = event.payload ?? {};
    const cardId = str(p.card_id) ?? str(p.id);
    if (cardId) {
      memoryCards = memoryCards.map((m) =>
        m.id === cardId
          ? {
              ...m,
              image_status: str(p.status) ?? (event.status === 'ok' ? 'ready' : 'unavailable'),
              image_reason: typeof p.reason === 'string' ? p.reason : m.image_reason,
              image_url: str(p.image_url) ?? m.image_url,
            }
          : m,
      );
    }
  }

  let rawtreeQueries = next.rawtreeQueries;
  if (event.event_type === 'rawtree_query') {
    rawtreeQueries = [normaliseQuery(event), ...rawtreeQueries].slice(0, MAX_RAWTREE_QUERIES);
  }

  let brainBadge = next.brainBadge;
  if (event.event_type === 'brain_decision') {
    brainBadge = {
      source: event.source,
      model: str(event.payload?.model) ?? '',
      from: 'brain_decision',
    };
  }

  let contextPoints = next.contextPoints;
  if (event.context_tokens > 0) {
    contextPoints = upsertPoint(contextPoints, {
      step: event.step,
      context_tokens: event.context_tokens,
      naive_tokens: event.naive_tokens,
    });
  }

  const events = [frame, ...next.events].slice(0, MAX_EVENTS);

  return {
    ...next,
    run,
    phase: run.phase,
    visited: withVisited(next.visited, run.phase),
    lastSeq: frame.seq,
    events,
    evidence: boundEvidence(evidence),
    approval,
    approvalDismissed,
    settledApprovals: settled,
    memoryCards,
    rawtreeQueries,
    brainBadge,
    contextPoints,
  };
}

/* -------------------------------------------------------------- reducer -- */

export function warRoomReducer(model: WarRoomModel, action: WarRoomAction): WarRoomModel {
  switch (action.type) {
    case 'init': {
      // The initial reads describe the same moment from four endpoints. Apply
      // the snapshot first, then replay the history on top of it with the
      // high-water mark lowered to the oldest event, so history fills the log
      // and derived views without being rejected as duplicates.
      let next: WarRoomModel = {
        ...model,
        health: action.health ?? model.health,
        rawtreeQueries: action.queries.slice(0, MAX_RAWTREE_QUERIES),
      };
      for (const point of action.points) next = { ...next, contextPoints: upsertPoint(next.contextPoints, point) };
      next = applySnapshot(next, action.state);
      const history = action.events
        .filter(isSeqEvent)
        .sort((a, b) => a.seq - b.seq);
      const snapshotRun = next.run;
      const snapshotSeq = next.lastSeq;
      next = { ...next, lastSeq: 0 };
      for (const frame of history) next = applyHorizon(next, frame);
      // The checkpoint is newer than, or as new as, the history: keep its view
      // of the run so replayed events cannot roll hypotheses or goals back.
      if (snapshotRun && (!next.run || next.run.run_id === snapshotRun.run_id)) {
        next = {
          ...next,
          run: snapshotRun,
          phase: isPhase(snapshotRun.phase) ? snapshotRun.phase : next.phase,
          visited: isPhase(snapshotRun.phase)
            ? withVisited(next.visited, snapshotRun.phase)
            : next.visited,
          evidence: evidenceFromRun(snapshotRun, next.evidence),
        };
      }
      // A pending approval in history is only still pending if the run is.
      if (next.approval && next.run && next.run.phase !== 'AWAITING_APPROVAL') {
        next = { ...next, approval: null };
      }
      return { ...next, lastSeq: Math.max(next.lastSeq, snapshotSeq) };
    }
    case 'snapshot':
      return applySnapshot(model, action.state);
    case 'horizon':
      return isSeqEvent(action.frame) ? applyHorizon(model, action.frame) : model;
    case 'health':
      return { ...model, health: action.health };
    case 'connection':
      return { ...model, connection: action.state };
    case 'approval-dismissed':
      return { ...model, approvalDismissed: true };
    case 'approval-reopened':
      return model.approval ? { ...model, approvalDismissed: false } : model;
    case 'approval-settled':
      return {
        ...model,
        approval: model.approval?.approval_id === action.approvalId ? null : model.approval,
        settledApprovals: [...model.settledApprovals, action.approvalId].slice(-20),
      };
    default:
      return model;
  }
}

/** Whether the approval modal should be open. */
export function approvalOpen(model: WarRoomModel): boolean {
  return model.approval !== null && !model.approvalDismissed;
}
