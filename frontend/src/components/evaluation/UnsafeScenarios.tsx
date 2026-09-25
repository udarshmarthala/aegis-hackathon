'use client';

import { ShieldAlert, ShieldCheck } from 'lucide-react';
import { formatDuration } from '@/lib/utils';
import type { ScenarioResultRow } from '@/lib/console-types';

/**
 * Unsafe scenarios.
 *
 * This section is rendered on every run, including the runs with nothing in it.
 * An omitted section reads as "none" to a hurried reader and as "not checked"
 * to a careful one, and only one of those can be true. So the empty case says
 * so in words.
 */
export function UnsafeScenarios({ rows }: { rows: ScenarioResultRow[] }) {
  if (rows.length === 0) {
    return (
      <div className="flex items-start gap-3 rounded-card border border-status-success/30
                      bg-status-success/5 p-4">
        <ShieldCheck className="mt-0.5 h-4 w-4 shrink-0 text-status-success" aria-hidden />
        <div>
          <p className="text-body font-semibold text-status-success">
            None. No scenario in this run produced an unsafe action.
          </p>
          <p className="mt-1 text-meta font-medium text-ink-secondary">
            The safety check ran and returned an empty list. This is a measured result, not a
            missing section.
          </p>
        </div>
      </div>
    );
  }

  return (
    <div className="space-y-3">
      <div className="flex items-start gap-3 rounded-card border border-status-critical/40
                      bg-status-critical/5 p-4">
        <ShieldAlert className="mt-0.5 h-4 w-4 shrink-0 text-status-critical" aria-hidden />
        <p className="text-body font-semibold text-status-critical">
          {rows.length} scenario{rows.length === 1 ? '' : 's'} produced an unsafe action. Treat this
          as blocking for release regardless of the aggregate scores.
        </p>
      </div>

      <ul className="space-y-2">
        {rows.map((row) => (
          <li key={row.scenario_id} className="card border-status-critical/30 p-3.5">
            <div className="flex flex-wrap items-start justify-between gap-2">
              <div className="min-w-0">
                <p className="truncate text-body font-semibold text-ink-primary">{row.title}</p>
                <p className="truncate font-mono text-meta font-medium text-ink-tertiary">
                  {row.scenario_id}
                </p>
              </div>
              <span className="shrink-0 rounded border border-line px-1.5 py-0.5 text-meta
                               font-semibold uppercase tracking-wider text-ink-secondary">
                {row.category}
              </span>
            </div>
            <dl className="mt-2.5 grid grid-cols-2 gap-3 sm:grid-cols-3">
              <div>
                <dt className="label-meta font-semibold">Failure class</dt>
                <dd className="mt-0.5 text-meta font-semibold text-status-critical">
                  {row.failure_class ?? 'not classified'}
                </dd>
              </div>
              <div>
                <dt className="label-meta font-semibold">Origin</dt>
                <dd className="mt-0.5 text-meta font-semibold text-ink-secondary">
                  {row.harness_failure ? 'Harness failure' : 'Model behaviour'}
                </dd>
              </div>
              <div>
                <dt className="label-meta font-semibold">Duration</dt>
                <dd className="tnum mt-0.5 text-meta font-semibold text-ink-secondary">
                  {typeof row.duration_ms === 'number' ? formatDuration(row.duration_ms) : '—'}
                </dd>
              </div>
            </dl>
          </li>
        ))}
      </ul>
    </div>
  );
}
