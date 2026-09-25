'use client';

import { ArrowRight, CornerDownLeft } from 'lucide-react';
import { cn } from '@/lib/utils';
import type { HorizonPhase } from '@/lib/war-room/types';

/**
 * Row 2, left: where the run is in its lifecycle.
 *
 * The phase is chosen by the orchestrator's guard, never by the model, so this
 * chain is a readout of deterministic state. The current phase is marked three
 * ways - a pulse, a ring and the word "now" - because a pulse alone is lost on
 * anyone with reduced motion enabled and colour alone on anyone who cannot
 * separate the hues. Entering REASSESSING (verification failed) turns its
 * arrow red: that is the moment the demo turns on.
 */

const MAIN: readonly HorizonPhase[] = [
  'IDLE', 'DETECTING', 'INVESTIGATING', 'DIAGNOSING', 'PLANNING',
  'AWAITING_APPROVAL', 'EXECUTING', 'VERIFYING', 'REASSESSING',
];
const OPTIONAL: ReadonlySet<HorizonPhase> = new Set(['AWAITING_APPROVAL', 'REASSESSING']);

const SHORT: Record<HorizonPhase, string> = {
  IDLE: 'Idle',
  DETECTING: 'Detect',
  INVESTIGATING: 'Investigate',
  DIAGNOSING: 'Diagnose',
  PLANNING: 'Plan',
  AWAITING_APPROVAL: 'Approval',
  EXECUTING: 'Execute',
  VERIFYING: 'Verify',
  REASSESSING: 'Reassess',
  RESOLVED: 'Resolved',
  ESCALATED: 'Escalated',
};

function Node({
  phase,
  current,
  visited,
}: {
  phase: HorizonPhase;
  current: boolean;
  visited: boolean;
}) {
  const optional = OPTIONAL.has(phase);
  const danger = phase === 'REASSESSING' || phase === 'ESCALATED';
  const good = phase === 'RESOLVED';
  return (
    <div
      data-testid={`phase-${phase}`}
      data-state={current ? 'current' : visited ? 'visited' : 'pending'}
      className={cn(
        'flex min-w-0 flex-col items-center rounded-btn border px-2 py-1 text-center',
        optional && !current && !visited && 'border-dashed',
        current
          ? cn(
              'border-2 bg-surface-3 motion-safe:animate-pulse-soft',
              danger
                ? 'border-status-critical text-status-critical'
                : good
                  ? 'border-status-success text-status-success'
                  : 'border-accent text-ink-primary',
            )
          : visited
            ? cn(
                'bg-surface-2',
                danger ? 'border-status-critical/60 text-status-critical' : 'border-edge text-ink-secondary',
              )
            : 'border-line text-ink-tertiary',
      )}
    >
      <span className="whitespace-nowrap text-[0.9rem] font-bold">{SHORT[phase]}</span>
      <span className="text-[0.64rem] font-semibold uppercase tracking-wider">
        {current ? 'now' : visited ? 'done' : optional ? 'if needed' : ' '}
      </span>
    </div>
  );
}

export function PhaseChain({
  phase,
  visited,
}: {
  phase: HorizonPhase;
  visited: readonly HorizonPhase[];
}) {
  const seen = new Set(visited);
  const reassessEntered = seen.has('REASSESSING');
  const state = (p: HorizonPhase) => ({ current: phase === p, visited: seen.has(p) && phase !== p });
  return (
    <div>
      <p className="sr-only" aria-live="polite">
        Current phase {phase.replace('_', ' ').toLowerCase()}
      </p>
      <ol className="flex flex-wrap items-center gap-y-1.5" aria-label="Incident phase chain">
        {MAIN.map((p, i) => (
          <li key={p} className="flex items-center" aria-current={phase === p ? 'step' : undefined}>
            {i > 0 ? (
              <ArrowRight
                aria-hidden
                data-testid={p === 'REASSESSING' ? 'reassess-arrow' : undefined}
                data-alert={p === 'REASSESSING' && reassessEntered ? 'true' : undefined}
                className={cn(
                  'mx-0.5 shrink-0',
                  p === 'REASSESSING' && reassessEntered
                    ? 'h-5 w-5 text-status-critical'
                    : 'h-4 w-4 text-ink-tertiary',
                )}
              />
            ) : null}
            <Node phase={p} {...state(p)} />
          </li>
        ))}
        <li
          className="flex items-center"
          aria-current={phase === 'RESOLVED' || phase === 'ESCALATED' ? 'step' : undefined}
        >
          <ArrowRight aria-hidden className="mx-0.5 h-4 w-4 shrink-0 text-ink-tertiary" />
          <div className="flex flex-col gap-1">
            <Node phase="RESOLVED" {...state('RESOLVED')} />
            <Node phase="ESCALATED" {...state('ESCALATED')} />
          </div>
        </li>
      </ol>
      {reassessEntered ? (
        <p className="mt-1.5 inline-flex items-center gap-1 text-[0.8rem] font-bold text-status-critical">
          <CornerDownLeft className="h-3.5 w-3.5" aria-hidden />
          Verification failed - reassessing, back to diagnosis
        </p>
      ) : null}
    </div>
  );
}
