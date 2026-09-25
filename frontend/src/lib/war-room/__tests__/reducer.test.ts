import { describe, expect, test } from 'vitest';

import {
  MAX_DISCARDED_VISIBLE, MAX_EVENTS, approvalOpen, initialModel, warRoomReducer,
  type WarRoomAction, type WarRoomModel,
} from '@/lib/war-room/reducer';
import type {
  EvidenceCard, HorizonEvent, HorizonPhase, HorizonState, SeqEvent, WarRoomState,
} from '@/lib/war-room/types';

/**
 * The reducer is the war room's only memory. What it guarantees is what the
 * audience sees: a replayed frame is not applied twice, an evicted card is
 * shown leaving rather than silently vanishing, a failed verification turns
 * the chain red, and an approval request raises the modal exactly once.
 */

function card(id: string, over: Partial<EvidenceCard> = {}): EvidenceCard {
  return {
    id, step: 1, tool: 'metrics', source: 'rule', origin: 'prometheus', claim: `claim ${id}`,
    supports: [], refutes: [], weight: 0.5, raw_ref: '', url: null, tokens_raw: 900,
    tokens_card: 40, pinned: false, ...over,
  };
}

function run(over: Partial<HorizonState> = {}): HorizonState {
  return {
    run_id: 'run-1', incident_id: 'INC-043', service: 'checkout', symptom: 'p99 up', step: 1,
    phase: 'INVESTIGATING', remediation_cycle: 0, goals: [], hypotheses: [], evidence: [],
    discarded: [], memory: [], notes: [], observe_tools_run: [], actions: [],
    excluded_actions: [], pending_action_id: null, brain_source: 'scripted',
    tokens: {
      context_tokens: 0, naive_tokens: 0, cache_read_tokens: 0, cache_hits: 0,
      fallbacks_used: 0, compacted_raw_tokens: 0, compacted_card_tokens: 0,
    },
    escalation_reason: null, started_at: null, updated_at: null, ...over,
  };
}

function snapshot(over: Partial<WarRoomState> = {}): WarRoomState {
  return {
    mode: 'scripted', run: run(), incident: null, brain: {}, integrations: {},
    memory_cards: [], last_seq: 0,
    stats: {
      compression_ratio: 0, cache_hits: 0, cache_read_tokens: 0, fallbacks_used: 0,
      steps: 0, context_tokens: 0, naive_tokens: 0,
    },
    ...over,
  };
}

function ev(
  seq: number,
  type: string,
  payload: Record<string, unknown> = {},
  over: Partial<HorizonEvent> = {},
): SeqEvent {
  return {
    seq,
    event: {
      ts: '2026-09-26T10:00:00Z', run_id: 'run-1', incident_id: 'INC-043', step: 1,
      phase: 'INVESTIGATING', event_type: type, tool: null, status: 'ok', duration_ms: 0,
      source: 'system', context_tokens: 0, naive_tokens: 0, message: '', payload, ...over,
    },
  };
}

function reduce(actions: WarRoomAction[], start: WarRoomModel = initialModel): WarRoomModel {
  return actions.reduce(warRoomReducer, start);
}

const horizon = (frame: SeqEvent): WarRoomAction => ({ type: 'horizon', frame });

describe('snapshot then horizon events', () => {
  test('a snapshot seeds the run and later events edit it in place', () => {
    const model = reduce([
      { type: 'snapshot', state: snapshot({ last_seq: 3, run: run({ evidence: [card('ev1')] }) }) },
      horizon(ev(4, 'evidence_added', { card: card('ev2') })),
      horizon(ev(5, 'hypotheses_updated', {
        hypotheses: [{ id: 'h1', statement: 'pool leak', supporting: ['ev2'], refuting: [], confidence: 0.7, history: [0.4, 0.7], suggested_action: null }],
      })),
    ]);
    expect(model.mode).toBe('scripted');
    expect(model.lastSeq).toBe(5);
    expect(model.evidence.map((v) => v.card.id)).toEqual(['ev1', 'ev2']);
    expect(model.run?.hypotheses[0]?.confidence).toBe(0.7);
    expect(model.events.map((e) => e.seq)).toEqual([5, 4]);
  });

  test('the brain badge comes from the latest brain_decision, with its model', () => {
    const model = reduce([
      { type: 'snapshot', state: snapshot() },
      horizon(ev(1, 'brain_decision', { model: 'claude-sonnet-4-6' }, { source: 'bedrock' })),
      horizon(ev(2, 'brain_decision', { model: 'gemini-flash' }, { source: 'gemini' })),
    ]);
    expect(model.brainBadge).toEqual({ source: 'gemini', model: 'gemini-flash', from: 'brain_decision' });
  });
});

describe('dedupe by seq', () => {
  test('a frame at or below the high-water mark is ignored', () => {
    const once = reduce([
      { type: 'snapshot', state: snapshot() },
      horizon(ev(7, 'evidence_added', { card: card('ev1') })),
    ]);
    const twice = reduce([horizon(ev(7, 'evidence_added', { card: card('ev1') })), horizon(ev(6, 'note_written'))], once);
    expect(twice).toBe(once);
    expect(twice.events).toHaveLength(1);
  });

  test('the initial history and a replayed live frame do not double up', () => {
    const model = reduce([
      {
        type: 'init',
        state: snapshot({ last_seq: 2 }),
        events: [ev(1, 'step_started'), ev(2, 'step_completed')],
        points: [],
        health: null,
        queries: [],
      },
      horizon(ev(2, 'step_completed')),
      horizon(ev(3, 'step_started')),
    ]);
    expect(model.events.map((e) => e.seq)).toEqual([3, 2, 1]);
  });

  test('a store whose sequence went backwards resets the mark instead of freezing the page', () => {
    const before = reduce([
      { type: 'snapshot', state: snapshot({ last_seq: 50 }) },
      horizon(ev(51, 'step_started')),
    ]);
    const after = reduce([
      { type: 'snapshot', state: snapshot({ last_seq: 0 }) },
      horizon(ev(1, 'step_started')),
    ], before);
    expect(after.lastSeq).toBe(1);
    expect(after.events.map((e) => e.seq)).toEqual([1]);
  });

  test('the event list is bounded', () => {
    const actions: WarRoomAction[] = [{ type: 'snapshot', state: snapshot() }];
    for (let i = 1; i <= MAX_EVENTS + 50; i += 1) actions.push(horizon(ev(i, 'tool_called')));
    const model = reduce(actions);
    expect(model.events).toHaveLength(MAX_EVENTS);
    expect(model.events[0]?.seq).toBe(MAX_EVENTS + 50);
  });
});

describe('eviction', () => {
  test('a discarded card stays visible, marked, rather than vanishing', () => {
    const model = reduce([
      { type: 'snapshot', state: snapshot({ run: run({ evidence: [card('ev1'), card('ev2')] }) }) },
      horizon(ev(1, 'evidence_discarded', { evidence_id: 'ev1' })),
    ]);
    expect(model.evidence).toEqual([
      { card: expect.objectContaining({ id: 'ev1' }), discarded: true },
      { card: expect.objectContaining({ id: 'ev2' }), discarded: false },
    ]);
    expect(model.run?.evidence.map((c) => c.id)).toEqual(['ev2']);
    expect(model.run?.discarded).toContain('ev1');
  });

  test('a snapshot arriving after the eviction keeps the card faded, not gone', () => {
    const model = reduce([
      { type: 'snapshot', state: snapshot({ run: run({ evidence: [card('ev1'), card('ev2')] }) }) },
      horizon(ev(1, 'evidence_discarded', { ids: ['ev1'] })),
      { type: 'snapshot', state: snapshot({ last_seq: 1, run: run({ evidence: [card('ev2')], discarded: ['ev1'] }) }) },
    ]);
    expect(model.evidence.find((v) => v.card.id === 'ev1')?.discarded).toBe(true);
  });

  test('only a few discarded cards are kept on screen', () => {
    const cards = Array.from({ length: 8 }, (_, i) => card(`ev${i}`));
    const actions: WarRoomAction[] = [{ type: 'snapshot', state: snapshot({ run: run({ evidence: cards }) }) }];
    cards.forEach((c, i) => actions.push(horizon(ev(i + 1, 'evidence_discarded', { evidence_id: c.id }))));
    const model = reduce(actions);
    expect(model.evidence.filter((v) => v.discarded)).toHaveLength(MAX_DISCARDED_VISIBLE);
  });
});

describe('phase changes', () => {
  function phaseEvent(seq: number, phase: HorizonPhase): SeqEvent {
    return ev(seq, 'phase_changed', { to: phase }, { phase });
  }

  test('the current phase follows the stream and visited phases accumulate', () => {
    const model = reduce([
      { type: 'snapshot', state: snapshot({ run: run({ phase: 'DETECTING' }) }) },
      horizon(phaseEvent(1, 'INVESTIGATING')),
      horizon(phaseEvent(2, 'DIAGNOSING')),
    ]);
    expect(model.phase).toBe('DIAGNOSING');
    expect(model.visited).toEqual(['DETECTING', 'INVESTIGATING', 'DIAGNOSING']);
  });

  test('entering REASSESSING is remembered after the run moves on', () => {
    const model = reduce([
      { type: 'snapshot', state: snapshot({ run: run({ phase: 'VERIFYING' }) }) },
      horizon(phaseEvent(1, 'REASSESSING')),
      horizon(phaseEvent(2, 'DIAGNOSING')),
    ]);
    expect(model.phase).toBe('DIAGNOSING');
    expect(model.visited).toContain('REASSESSING');
  });

  test('a new run id starts the per-run views clean', () => {
    const model = reduce([
      { type: 'snapshot', state: snapshot({ run: run({ phase: 'VERIFYING', evidence: [card('ev1')] }) }) },
      horizon(phaseEvent(1, 'REASSESSING')),
      horizon(ev(2, 'phase_changed', { to: 'DETECTING' }, { run_id: 'run-2', phase: 'DETECTING' })),
    ]);
    expect(model.run?.run_id).toBe('run-2');
    expect(model.visited).toEqual(['DETECTING']);
    expect(model.evidence).toEqual([]);
  });
});

describe('approval_required', () => {
  const payload = {
    approval_id: 'apr-1', action_id: 'act-1', action_type: 'rollback_deployment',
    target: 'checkout', reason: 'restart did not verify', confidence: 0.82,
    evidence_ids: ['ev3', 'ev7'], risk_tier: 2,
  };

  test('opens the modal with the normalised payload', () => {
    const model = reduce([
      { type: 'snapshot', state: snapshot() },
      horizon(ev(9, 'approval_required', payload, { phase: 'AWAITING_APPROVAL' })),
    ]);
    expect(approvalOpen(model)).toBe(true);
    expect(model.approval).toEqual({ seq: 9, ...payload });
  });

  test('a payload without an approval id opens nothing - there is nothing to decide', () => {
    const model = reduce([
      { type: 'snapshot', state: snapshot() },
      horizon(ev(9, 'approval_required', { ...payload, approval_id: undefined })),
    ]);
    expect(approvalOpen(model)).toBe(false);
  });

  test('approval_resolved closes it, and a dismissed modal can be reopened', () => {
    const open = reduce([
      { type: 'snapshot', state: snapshot() },
      horizon(ev(9, 'approval_required', payload, { phase: 'AWAITING_APPROVAL' })),
    ]);
    const dismissed = warRoomReducer(open, { type: 'approval-dismissed' });
    expect(approvalOpen(dismissed)).toBe(false);
    expect(approvalOpen(warRoomReducer(dismissed, { type: 'approval-reopened' }))).toBe(true);
    const resolved = warRoomReducer(open, horizon(ev(10, 'approval_resolved', { approval_id: 'apr-1' })));
    expect(resolved.approval).toBeNull();
  });

  test('a stale snapshot does not close a newer request; a newer one does', () => {
    const open = reduce([
      { type: 'snapshot', state: snapshot({ last_seq: 5 }) },
      horizon(ev(9, 'approval_required', payload, { phase: 'AWAITING_APPROVAL' })),
    ]);
    const stale = warRoomReducer(open, { type: 'snapshot', state: snapshot({ last_seq: 8 }) });
    expect(stale.approval).not.toBeNull();
    const fresh = warRoomReducer(open, {
      type: 'snapshot', state: snapshot({ last_seq: 12, run: run({ phase: 'EXECUTING' }) }),
    });
    expect(fresh.approval).toBeNull();
  });

  test('a request the operator already decided is not raised again on replay', () => {
    const decided = reduce([
      { type: 'snapshot', state: snapshot() },
      horizon(ev(9, 'approval_required', payload)),
      { type: 'approval-settled', approvalId: 'apr-1' },
      horizon(ev(11, 'approval_required', payload)),
    ]);
    expect(decided.approval).toBeNull();
  });
});

describe('context series', () => {
  test('points come from events carrying token counts, one per step', () => {
    const model = reduce([
      { type: 'snapshot', state: snapshot() },
      horizon(ev(1, 'step_completed', {}, { step: 1, context_tokens: 2100, naive_tokens: 3000 })),
      horizon(ev(2, 'step_completed', {}, { step: 1, context_tokens: 2200, naive_tokens: 3100 })),
      horizon(ev(3, 'step_completed', {}, { step: 2, context_tokens: 2300, naive_tokens: 9000 })),
    ]);
    expect(model.contextPoints).toEqual([
      { step: 1, context_tokens: 2200, naive_tokens: 3100 },
      { step: 2, context_tokens: 2300, naive_tokens: 9000 },
    ]);
  });
});
