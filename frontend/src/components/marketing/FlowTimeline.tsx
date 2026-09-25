import { cn } from '@/lib/utils';

/**
 * A worked example of one investigation.
 *
 * Static and illustrative, and labelled as such — inventing a live-looking
 * incident on a marketing page would be exactly the kind of fabricated
 * confidence the product exists to avoid. The shape mirrors the real incident
 * timeline so the console feels familiar on first sight.
 */

type Tone = 'done' | 'active' | 'pending';

interface Step {
  title: string;
  detail: string;
  at: string;
  tone: Tone;
}

const STEPS: Step[] = [
  {
    title: 'Signal correlated',
    detail:
      'Elevated p99 latency on checkout, connection-pool saturation on the orders database and a deploy 4 minutes earlier all fall inside the same window.',
    at: '00:14',
    tone: 'done',
  },
  {
    title: 'Blast radius established',
    detail:
      'Topology shows three downstream services depend on checkout. Two are customer-facing, which raises the required approval tier.',
    at: '00:21',
    tone: 'done',
  },
  {
    title: 'Change localised',
    detail:
      'Commit 91f2d7 altered connection lifecycle in the checkout repository. Two symbols and the test covering them identified.',
    at: '00:32',
    tone: 'done',
  },
  {
    title: 'Hypothesis under test',
    detail:
      'The leak reproduces under burst traffic in a sandbox with no network and no production credentials. The candidate patch is running against the same reproduction.',
    at: '01:08',
    tone: 'active',
  },
  {
    title: 'Production action gated',
    detail:
      'Awaiting staging proof and deterministic risk classification. Rollback plan recorded; nothing reaches production until both pass.',
    at: '—',
    tone: 'pending',
  },
];

const DOT: Record<Tone, string> = {
  done: 'bg-status-success',
  active: 'bg-status-warning animate-pulse-soft',
  pending: 'bg-status-neutral',
};

export function FlowTimeline() {
  return (
    <div className="overflow-hidden rounded-card border border-line bg-surface-1">
      <div className="flex items-center justify-between border-b border-hairline px-4 py-3">
        <span className="text-meta font-semibold text-ink-secondary">
          INC-2471 · checkout latency degradation
        </span>
        <span className="text-meta font-bold uppercase tracking-wider text-ink-tertiary">
          Illustrative
        </span>
      </div>

      <ol className="px-4">
        {STEPS.map((step, index) => (
          <li
            key={step.title}
            className={cn(
              'grid grid-cols-[10px_1fr_auto] items-start gap-x-3.5 py-4',
              index < STEPS.length - 1 && 'border-b border-hairline',
            )}
          >
            <span
              aria-hidden
              className={cn('mt-[6px] h-2 w-2 rounded-full', DOT[step.tone])}
            />
            <div className="min-w-0">
              <p className="text-body font-semibold text-ink-primary">{step.title}</p>
              <p className="mt-1 text-meta font-medium leading-[1.7] text-ink-tertiary">
                {step.detail}
              </p>
            </div>
            <span className="tnum text-meta font-semibold text-ink-tertiary">{step.at}</span>
          </li>
        ))}
      </ol>
    </div>
  );
}
