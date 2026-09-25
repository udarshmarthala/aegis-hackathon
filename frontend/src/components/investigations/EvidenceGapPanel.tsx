'use client';

import { SearchX } from 'lucide-react';
import { formatClock, relativeTime } from '@/lib/utils';
import type { EvidenceGapRow } from '@/lib/console-types';

/**
 * Evidence gaps, as first-class facts.
 *
 * A gap is not an error and not an absence of findings: it records a source
 * Aegis meant to consult and could not. Printed next to the count of usable
 * evidence, it tells an operator exactly how much of the picture was missing
 * when the conclusion was drawn.
 */
export function EvidenceGapPanel({
  gaps,
  usableEvidenceCount,
}: {
  gaps: EvidenceGapRow[];
  usableEvidenceCount: number;
}) {
  return (
    <div className="space-y-3">
      <div className="grid gap-3 sm:grid-cols-2">
        <div className="card p-3.5">
          <p className="label-meta font-semibold">Usable evidence</p>
          <p className="tnum mt-1 text-h2 font-semibold text-ink-primary">{usableEvidenceCount}</p>
          <p className="mt-1 text-meta font-medium text-ink-tertiary">
            Validated items the diagnosis could be built on.
          </p>
        </div>
        <div className="card p-3.5">
          <p className="label-meta font-semibold">Evidence gaps</p>
          <p
            className={
              gaps.length > 0
                ? 'tnum mt-1 text-h2 font-semibold text-status-warning'
                : 'tnum mt-1 text-h2 font-semibold text-ink-primary'
            }
          >
            {gaps.length}
          </p>
          <p className="mt-1 text-meta font-medium text-ink-tertiary">
            {gaps.length === 0
              ? 'Every source Aegis tried to consult answered.'
              : 'Sources Aegis could not consult. Confidence is reduced accordingly.'}
          </p>
        </div>
      </div>

      {gaps.length === 0 ? null : (
        <ul className="space-y-2">
          {gaps.map((gap) => (
            <li key={gap.id} className="card border-status-warning/30 bg-status-warning/5 p-3.5">
              <div className="flex items-start gap-3">
                <SearchX className="mt-0.5 h-4 w-4 shrink-0 text-status-warning" aria-hidden />
                <div className="min-w-0 space-y-1">
                  <p className="text-body font-semibold text-status-warning">
                    {gap.source}
                    <span className="ml-2 text-meta font-semibold uppercase tracking-wider text-ink-tertiary">
                      {gap.source_type}
                    </span>
                  </p>
                  <p className="text-meta font-medium text-ink-secondary">{gap.summary}</p>
                  <p className="text-meta font-medium text-ink-tertiary">Reason: {gap.reason}</p>
                  <p className="tnum text-meta font-medium text-ink-tertiary">
                    Recorded {relativeTime(gap.retrieved_at)} · {formatClock(gap.retrieved_at)}
                  </p>
                </div>
              </div>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
