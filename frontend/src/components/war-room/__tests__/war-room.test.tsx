import { fireEvent, render, screen, within } from '@testing-library/react';
import { describe, expect, test, vi } from 'vitest';

import { ApprovalModal } from '@/components/war-room/ApprovalModal';
import { HealthCard } from '@/components/war-room/HealthCards';
import { PhaseChain } from '@/components/war-room/PhaseChain';
import { EvidenceCards } from '@/components/war-room/Reasoning';
import { EventStream } from '@/components/war-room/Outcome';
import { useDemoShortcuts, type ShortcutHandlers } from '@/components/war-room/useDemoShortcuts';
import type { EvidenceCard, HealthService } from '@/lib/war-room/types';

/**
 * What the audience must be able to trust on the war-room page: an unread
 * metric is not a zero, every card names the path that produced it, the demo
 * keys fire from the page but never from a text field, and the approval
 * request shows what is being authorised before it can be authorised.
 */

function svc(over: Partial<HealthService> = {}): HealthService {
  return {
    service: 'checkout', p99_ms: 120, error_rate: 0.004, pool_utilisation: 0.35,
    version: '1.4.1', status: 'healthy', source: 'prometheus',
    series: { p99_ms: [100, 120], error_rate: [0, 0.004], pool_utilisation: [0.3, 0.35] },
    ...over,
  };
}

describe('HealthCard', () => {
  test('an unavailable source renders "unavailable", never 0', () => {
    render(
      <HealthCard
        name="payment"
        svc={svc({
          service: 'payment', source: 'unavailable', status: 'unknown',
          p99_ms: null, error_rate: null, pool_utilisation: null,
          series: { p99_ms: [], error_rate: [], pool_utilisation: [] },
        })}
      />,
    );
    const card = screen.getByTestId('health-payment');
    for (const metric of ['p99_ms', 'error_rate', 'pool_utilisation']) {
      expect(within(card).getByTestId(`metric-${metric}`)).toHaveTextContent('unavailable');
    }
    expect(card.textContent).not.toMatch(/\b0(\.0)?\s?(ms|%)/);
  });

  test('a missing service is unavailable too, not healthy', () => {
    render(<HealthCard name="db-pool" svc={undefined} />);
    const card = screen.getByTestId('health-db-pool');
    expect(within(card).getByTestId('metric-pool_utilisation')).toHaveTextContent('unavailable');
    expect(card).not.toHaveTextContent('Healthy');
  });

  test('a real reading renders the values and the status in words', () => {
    render(<HealthCard name="checkout" svc={svc({ status: 'critical', pool_utilisation: 0.95 })} />);
    const card = screen.getByTestId('health-checkout');
    expect(within(card).getByTestId('metric-p99_ms')).toHaveTextContent('120 ms');
    expect(within(card).getByTestId('metric-pool_utilisation')).toHaveTextContent('95%');
    expect(card).toHaveTextContent('Critical');
    expect(card).toHaveTextContent('v1.4.1');
  });
});

function card(id: string, over: Partial<EvidenceCard> = {}): EvidenceCard {
  return {
    id, step: 3, tool: 'search_known_issues', source: 'gemini', origin: 'nimble',
    claim: 'upstream issue reports a connection leak', supports: [], refutes: [], weight: 0.6,
    raw_ref: '', url: null, tokens_raw: 4200, tokens_card: 52, pinned: false, ...over,
  };
}

describe('EvidenceCards', () => {
  test('every card carries a source label, and the compression is shown', () => {
    render(
      <EvidenceCards
        evidence={[
          { card: card('ev1', { source: 'rule', origin: 'prometheus' }), discarded: false },
          { card: card('ev2', { source: 'fixture', url: 'https://github.com/example/issue/1' }), discarded: false },
        ]}
      />,
    );
    const cards = screen.getAllByTestId('evidence-card');
    expect(cards).toHaveLength(2);
    for (const c of cards) expect(within(c).getAllByTestId('source-label').length).toBeGreaterThan(0);
    const sources = screen.getAllByTestId('source-label').map((el) => el.getAttribute('data-source'));
    expect(sources).toEqual(expect.arrayContaining(['rule', 'fixture', 'nimble']));
    expect(screen.getAllByText('4,200→52 tokens')).toHaveLength(2);
    expect(screen.getByRole('link', { name: /github\.com\/example/ })).toHaveAttribute('rel', 'noopener noreferrer');
  });

  test('a discarded card says where it went', () => {
    render(<EvidenceCards evidence={[{ card: card('ev9'), discarded: true }]} />);
    const c = screen.getByTestId('evidence-card');
    expect(c).toHaveAttribute('data-discarded', 'true');
    expect(c).toHaveTextContent('evicted → RawTree');
  });

  test('events carry their source label and status', () => {
    render(
      <EventStream
        events={[{
          seq: 4,
          event: {
            ts: '2026-09-26T10:00:00Z', run_id: 'r', incident_id: 'i', step: 2, phase: 'DIAGNOSING',
            event_type: 'brain_fallback', tool: null, status: 'degraded', duration_ms: 0,
            source: 'scripted', context_tokens: 0, naive_tokens: 0, message: 'bedrock timed out', payload: {},
          },
        }]}
      />,
    );
    expect(screen.getByTestId('source-label')).toHaveAttribute('data-source', 'scripted');
    expect(screen.getByText('degraded')).toBeInTheDocument();
  });
});

function ShortcutHarness({ handlers }: { handlers: ShortcutHandlers }) {
  useDemoShortcuts(handlers);
  return (
    <div>
      <input aria-label="note" />
      <button type="button">focusable</button>
    </div>
  );
}

describe('demo shortcuts', () => {
  test('keys trigger their actions from the page', () => {
    const handlers = { inject: vi.fn(), kill: vi.fn(), reset: vi.fn(), approve: vi.fn(), deny: vi.fn(), help: vi.fn() };
    render(<ShortcutHarness handlers={handlers} />);
    for (const key of ['i', 'k', 'r', 'a', 'd', '?']) fireEvent.keyDown(document.body, { key });
    for (const fn of Object.values(handlers)) expect(fn).toHaveBeenCalledTimes(1);
  });

  test('keys typed into an input are ignored', () => {
    const handlers = { approve: vi.fn(), reset: vi.fn() };
    render(<ShortcutHarness handlers={handlers} />);
    const input = screen.getByLabelText('note');
    fireEvent.keyDown(input, { key: 'a' });
    fireEvent.keyDown(input, { key: 'r' });
    expect(handlers.approve).not.toHaveBeenCalled();
    expect(handlers.reset).not.toHaveBeenCalled();
  });
});

describe('ApprovalModal', () => {
  const approval = {
    seq: 9, approval_id: 'apr-1', action_id: 'act-1', action_type: 'rollback_deployment',
    target: 'checkout', reason: 'restart did not verify', confidence: 0.82,
    evidence_ids: ['ev3', 'ev7'], risk_tier: 2,
  };

  test('shows action, target, reason, derived confidence, cited evidence and risk tier', () => {
    const onApprove = vi.fn();
    const onDeny = vi.fn();
    render(
      <ApprovalModal approval={approval} open pending={false} onApprove={onApprove} onDeny={onDeny} onDismiss={vi.fn()} />,
    );
    const dialog = screen.getByRole('dialog');
    expect(dialog).toHaveTextContent('rollback_deployment');
    expect(dialog).toHaveTextContent('checkout');
    expect(dialog).toHaveTextContent('restart did not verify');
    expect(dialog).toHaveTextContent('82%');
    expect(dialog).toHaveTextContent('ev3');
    expect(dialog).toHaveTextContent('ev7');
    expect(dialog).toHaveTextContent('Tier 2');
    fireEvent.click(within(dialog).getByRole('button', { name: /Approve rollback_deployment/ }));
    fireEvent.click(within(dialog).getByRole('button', { name: /Deny/ }));
    expect(onApprove).toHaveBeenCalledTimes(1);
    expect(onDeny).toHaveBeenCalledTimes(1);
  });

  test('an approval with no cited evidence says so loudly', () => {
    render(
      <ApprovalModal approval={{ ...approval, evidence_ids: [] }} open pending={false} onApprove={vi.fn()} onDeny={vi.fn()} onDismiss={vi.fn()} />,
    );
    expect(screen.getByText('No evidence cited.')).toBeInTheDocument();
  });
});

describe('PhaseChain', () => {
  test('the current phase is marked in words, and REASSESSING turns its arrow red once entered', () => {
    const { rerender } = render(<PhaseChain phase="VERIFYING" visited={['DETECTING', 'VERIFYING']} />);
    expect(screen.getByTestId('phase-VERIFYING')).toHaveAttribute('data-state', 'current');
    expect(screen.getByTestId('phase-VERIFYING')).toHaveTextContent('now');
    expect(screen.getByTestId('reassess-arrow')).not.toHaveAttribute('data-alert');

    rerender(<PhaseChain phase="DIAGNOSING" visited={['DETECTING', 'VERIFYING', 'REASSESSING', 'DIAGNOSING']} />);
    expect(screen.getByTestId('reassess-arrow')).toHaveAttribute('data-alert', 'true');
    expect(screen.getByText(/Verification failed/)).toBeInTheDocument();
  });
});
