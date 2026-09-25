'use client';

import { Repeat } from 'lucide-react';
import { formatClock, relativeTime } from '@/lib/utils';
import type { RecurringPattern } from '@/lib/console-types';

/**
 * One known failure, as a structured knowledge card (UX spec 49).
 *
 * Not a chat transcript and not a log line: an operator should be able to read
 * the cause class, what it looks like from outside, who it hits and when it was
 * last seen without opening anything else.
 */
export function RecurringPatternCard({ pattern }: { pattern: RecurringPattern }) {
  return (
    <article className="card p-4">
      <header className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <p className="label-meta font-semibold">Known failure</p>
          <h3 className="mt-0.5 truncate text-body font-semibold text-ink-primary">
            {pattern.cause_category || 'Uncategorised cause'}
          </h3>
        </div>
        <span
          className="inline-flex shrink-0 items-center gap-1.5 rounded border border-status-warning/40
                     bg-status-warning/10 px-2 py-1 text-meta font-semibold text-status-warning"
        >
          <Repeat className="h-3 w-3" aria-hidden />
          {pattern.occurrences} verified occurrences
        </span>
      </header>

      <dl className="mt-3 space-y-2.5">
        <div>
          <dt className="label-meta font-semibold">Symptom</dt>
          <dd className="mt-0.5 text-body font-medium text-ink-secondary">
            {pattern.symptom || 'No symptom summary recorded with this pattern.'}
          </dd>
        </div>

        <div>
          <dt className="label-meta font-semibold">Affected services</dt>
          <dd className="mt-1">
            {pattern.services.length === 0 ? (
              <span className="text-meta font-medium text-ink-tertiary">
                No service attribution recorded.
              </span>
            ) : (
              <ul className="flex flex-wrap gap-1">
                {pattern.services.map((service) => (
                  <li
                    key={service}
                    className="rounded border border-hairline bg-surface-2 px-1.5 py-0.5
                               font-mono text-meta font-medium text-ink-secondary"
                  >
                    {service}
                  </li>
                ))}
              </ul>
            )}
          </dd>
        </div>

        <div className="grid grid-cols-2 gap-3">
          <div>
            <dt className="label-meta font-semibold">First seen</dt>
            <dd className="mt-0.5 text-meta font-semibold text-ink-primary">
              {pattern.first_seen ? relativeTime(pattern.first_seen) : 'Not recorded'}
            </dd>
            {pattern.first_seen ? (
              <dd className="tnum text-meta font-medium text-ink-tertiary">
                {formatClock(pattern.first_seen)}
              </dd>
            ) : null}
          </div>
          <div>
            <dt className="label-meta font-semibold">Last seen</dt>
            <dd className="mt-0.5 text-meta font-semibold text-ink-primary">
              {pattern.last_seen ? relativeTime(pattern.last_seen) : 'Not recorded'}
            </dd>
            {pattern.last_seen ? (
              <dd className="tnum text-meta font-medium text-ink-tertiary">
                {formatClock(pattern.last_seen)}
              </dd>
            ) : null}
          </div>
        </div>

        <div>
          <dt className="label-meta font-semibold">Fingerprint</dt>
          <dd className="mt-0.5 break-all font-mono text-meta font-medium text-ink-tertiary">
            {pattern.fingerprint}
          </dd>
        </div>
      </dl>
    </article>
  );
}
