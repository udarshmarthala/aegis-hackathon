'use client';

import { useMemo, useState } from 'react';
import type { TimelineEvent, TimelineResponse } from '@/lib/types';
import { cn, formatClock } from '@/lib/utils';
import { EmptyState } from '@/components/ui/states';

/**
 * The timeline is a forensic record, not a decorative feed (UX spec 27).
 *
 * Markers are small and semantic; the eye should scan timestamps and outcomes,
 * not icons. Long investigations are capped in the DOM so a busy incident does
 * not degrade the page.
 */
const FILTERS = [
  { key: 'all', label: 'All' },
  { key: 'STATE', label: 'State' },
  { key: 'AI', label: 'AI' },
  { key: 'TOOL', label: 'Tools' },
] as const;

const TONE: Record<string, string> = {
  STATE: 'bg-status-info',
  AI: 'bg-accent',
  TOOL: 'bg-status-neutral',
};

const MAX_RENDERED = 300;

export function Timeline({ data }: { data: TimelineResponse }) {
  const [filter, setFilter] = useState<(typeof FILTERS)[number]['key']>('all');

  const events = useMemo(() => {
    const filtered =
      filter === 'all' ? data.events : data.events.filter((e) => e.kind === filter);
    // Newest first: during an incident the latest development matters most.
    return [...filtered].reverse().slice(0, MAX_RENDERED);
  }, [data.events, filter]);

  return (
    <div className="flex h-full flex-col">
      <div className="mb-2.5 flex items-center gap-1" role="group" aria-label="Timeline filters">
        {FILTERS.map((f) => (
          <button
            key={f.key}
            type="button"
            onClick={() => setFilter(f.key)}
            aria-pressed={filter === f.key}
            className={cn(
              'rounded-btn px-2 py-0.5 text-meta transition-colors duration-hover',
              filter === f.key
                ? 'bg-surface-3 text-ink-primary'
                : 'text-ink-tertiary hover:bg-surface-2',
            )}
          >
            {f.label}
          </button>
        ))}
      </div>

      {events.length === 0 ? (
        <EmptyState title="Nothing recorded for this filter yet." />
      ) : (
        <ol className="min-h-0 flex-1 space-y-0 overflow-y-auto pr-1">
          {events.map((event, i) => (
            <TimelineRow key={`${event.at}-${i}`} event={event} />
          ))}
        </ol>
      )}

      {data.events.length > MAX_RENDERED ? (
        <p className="pt-2 text-meta text-ink-tertiary">
          Showing the most recent {MAX_RENDERED} of {data.events.length} events.
        </p>
      ) : null}
    </div>
  );
}

function TimelineRow({ event }: { event: TimelineEvent }) {
  const failed = event.ok === false || event.status === 'failed';
  return (
    <li className="relative flex gap-3 border-l border-hairline pb-3 pl-4 last:pb-0">
      <span
        className={cn(
          'absolute -left-[3px] top-1.5 h-1.5 w-1.5 rounded-full',
          failed ? 'bg-status-critical' : TONE[event.kind] ?? 'bg-status-neutral',
        )}
        aria-hidden
      />
      <div className="min-w-0 flex-1">
        <div className="flex items-baseline gap-2">
          <time className="tnum shrink-0 font-mono text-meta text-ink-tertiary">
            {formatClock(event.at)}
          </time>
          <span className="label-meta shrink-0">{event.kind}</span>
          {event.access === 'write' ? (
            <span className="shrink-0 rounded border border-status-warning/40 px-1
                             text-[10px] uppercase text-status-warning">
              write
            </span>
          ) : null}
        </div>
        <p className="mt-0.5 truncate text-body text-ink-primary">{event.title}</p>
        {event.detail ? (
          <p className="mt-0.5 text-meta text-ink-secondary">{event.detail}</p>
        ) : null}
        <div className="mt-0.5 flex flex-wrap items-center gap-2">
          {event.actor ? (
            <span className="text-meta text-ink-tertiary">{event.actor}</span>
          ) : null}
          {typeof event.duration_ms === 'number' ? (
            <span className="tnum text-meta text-ink-tertiary">{event.duration_ms}ms</span>
          ) : null}
          {event.evidence_ids?.length ? (
            <span className="text-meta text-ink-tertiary">
              {event.evidence_ids.length} evidence item
              {event.evidence_ids.length === 1 ? '' : 's'}
            </span>
          ) : null}
          {failed ? (
            <span className="text-meta text-status-critical">failed</span>
          ) : null}
        </div>
      </div>
    </li>
  );
}
