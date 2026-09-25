'use client';

import { EmptyState } from '@/components/ui/states';
import { cn } from '@/lib/utils';
import type { IntegrationRow } from '@/lib/console-types';

/**
 * Integration health.
 *
 * Three states, not two: healthy, unhealthy, and not checked. `healthy: null`
 * means no probe has run, which is not the same as a failing probe, and a table
 * that renders both as a red dot sends an engineer to debug a dependency that
 * was never asked a question.
 *
 * The reason column is the point of the page. "GraphRAG unavailable:
 * NEO4J_PASSWORD is not set" is a fix; "unavailable" is a support ticket.
 */
function healthLabel(row: IntegrationRow): { label: string; tone: string } {
  if (!row.configured) return { label: 'Not configured', tone: 'border-line bg-surface-3 text-ink-secondary' };
  if (row.healthy === null) {
    return { label: 'Not checked', tone: 'border-line bg-surface-3 text-ink-secondary' };
  }
  return row.healthy
    ? { label: 'Healthy', tone: 'border-status-success/40 bg-status-success/10 text-status-success' }
    : { label: 'Unhealthy', tone: 'border-status-critical/40 bg-status-critical/10 text-status-critical' };
}

function reasonFor(row: IntegrationRow): string {
  if (!row.configured) {
    return row.reason || 'No reason was recorded for this integration being unconfigured.';
  }
  if (row.healthy === false) {
    return row.health_reason || 'The health probe failed without recording a reason.';
  }
  if (row.healthy === null) {
    return row.health_reason || 'No health probe has run for this integration yet.';
  }
  return row.health_reason || 'Last probe succeeded.';
}

export function IntegrationTable({ rows }: { rows: IntegrationRow[] }) {
  if (rows.length === 0) {
    return (
      <EmptyState
        title="No integration is registered."
        detail="Aegis reports the integrations it knows about. An empty registry means none are compiled in."
      />
    );
  }

  return (
    <table className="w-full border-collapse text-body">
      <caption className="sr-only">Integration configuration and health</caption>
      <thead>
        <tr className="border-b border-hairline text-left">
          {['Integration', 'Configured', 'Health', 'Reason', 'Required'].map((header) => (
            <th key={header} scope="col" className="label-meta px-3 py-2 font-semibold">
              {header}
            </th>
          ))}
        </tr>
      </thead>
      <tbody>
        {rows.map((row) => {
          const health = healthLabel(row);
          return (
            <tr key={row.name} className="border-b border-hairline align-top last:border-0">
              <th scope="row" className="px-3 py-2 text-left font-semibold text-ink-primary">
                {row.name}
              </th>
              <td className="px-3 py-2">
                <span
                  className={cn(
                    'rounded border px-1.5 py-0.5 text-meta font-semibold uppercase tracking-wider',
                    row.configured
                      ? 'border-line bg-surface-3 text-ink-secondary'
                      : 'border-status-warning/40 bg-status-warning/10 text-status-warning',
                  )}
                >
                  {row.configured ? 'Configured' : 'Not configured'}
                </span>
              </td>
              <td className="px-3 py-2">
                <span
                  className={cn(
                    'rounded border px-1.5 py-0.5 text-meta font-semibold uppercase tracking-wider',
                    health.tone,
                  )}
                >
                  {health.label}
                </span>
              </td>
              <td className="max-w-[560px] px-3 py-2 font-medium text-ink-secondary">
                {reasonFor(row)}
              </td>
              <td className="px-3 py-2 font-medium text-ink-tertiary">
                {row.required ? 'Required' : 'Optional'}
              </td>
            </tr>
          );
        })}
      </tbody>
    </table>
  );
}
