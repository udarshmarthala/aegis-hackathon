'use client';

import { EmptyState } from '@/components/ui/states';
import { relativeTime } from '@/lib/utils';
import type { ServiceLoadRow } from '@/lib/console-types';

/**
 * Where the incidents actually land.
 *
 * Ranked, with an inline proportion bar rather than a pie: the operational
 * question is "which service is eating the on-call rota", and rank plus
 * magnitude answers it faster than angles do.
 */
export function ServiceLoadTable({
  rows,
  windowDays,
}: {
  rows: ServiceLoadRow[];
  windowDays: number;
}) {
  if (rows.length === 0) {
    return (
      <EmptyState
        title="No service recorded an incident in this window."
        detail={`Nothing was attributed to a service in the last ${windowDays} days.`}
        hint="widen the window"
      />
    );
  }

  const worst = Math.max(...rows.map((row) => row.incidents), 1);

  return (
    <table className="w-full border-collapse text-body">
      <caption className="sr-only">
        Per-service incident load over the last {windowDays} days, most affected first
      </caption>
      <thead>
        <tr className="border-b border-hairline text-left">
          <th scope="col" className="label-meta px-3 py-2 font-semibold">
            Service
          </th>
          <th scope="col" className="label-meta px-3 py-2 font-semibold">
            Share of load
          </th>
          <th scope="col" className="label-meta px-3 py-2 text-right font-semibold">
            Incidents
          </th>
          <th scope="col" className="label-meta px-3 py-2 text-right font-semibold">
            P1
          </th>
          <th scope="col" className="label-meta px-3 py-2 text-right font-semibold">
            Last incident
          </th>
        </tr>
      </thead>
      <tbody>
        {rows.map((row) => (
          <tr
            key={row.service_id}
            className="border-b border-hairline last:border-0 transition-colors duration-hover hover:bg-surface-2"
          >
            <th scope="row" className="max-w-[280px] px-3 py-2 text-left">
              <span className="block truncate font-semibold text-ink-primary">
                {row.service_id}
              </span>
            </th>
            <td className="px-3 py-2">
              <div className="h-1.5 w-full overflow-hidden rounded-full bg-surface-4" aria-hidden>
                <div
                  className={row.p1 > 0 ? 'h-full bg-status-critical' : 'h-full bg-accent'}
                  style={{ width: `${Math.round((row.incidents / worst) * 100)}%` }}
                />
              </div>
            </td>
            <td className="tnum px-3 py-2 text-right font-semibold text-ink-primary">
              {row.incidents}
            </td>
            <td
              className={
                row.p1 > 0
                  ? 'tnum px-3 py-2 text-right font-semibold text-status-critical'
                  : 'tnum px-3 py-2 text-right font-medium text-ink-tertiary'
              }
            >
              {row.p1}
            </td>
            <td className="px-3 py-2 text-right font-medium text-ink-secondary">
              {relativeTime(row.last_incident)}
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}
