'use client';

import Link from 'next/link';
import { useQuery } from '@tanstack/react-query';
import { consoleApi } from '@/lib/console-api';
import { SeverityBadge, StateChip } from '@/components/ui/primitives';
import { EmptyState, SkeletonRows } from '@/components/ui/states';
import { asIncidentState, asSeverity } from '@/components/investigations/domain';
import { QueryFailure } from '@/components/reliability/panels';
import { cn, formatDuration, relativeTime } from '@/lib/utils';

/**
 * Investigations.
 *
 * The list answers one question before you open anything: which investigations
 * had agent runs fail. A failed run does not invalidate a diagnosis, but it
 * does mean part of the work did not happen, and that belongs on the index.
 */
export default function InvestigationsPage() {
  const query = useQuery({
    queryKey: ['investigations'],
    queryFn: () => consoleApi.investigations(100),
    refetchInterval: 30_000,
  });

  const rows = query.data?.items ?? [];

  return (
    <div className="mx-auto max-w-[1600px] px-6 py-7">
      <header className="mb-5">
        <h1 className="text-h2 font-semibold tracking-tight">Investigations</h1>
        <p className="mt-1 max-w-3xl text-body font-medium text-ink-secondary">
          Every agent run, tool call and evidence gap behind each incident. Aegis records what was
          done and what came back — never model reasoning.
        </p>
      </header>

      {query.isLoading ? (
        <SkeletonRows rows={8} />
      ) : query.isError ? (
        <QueryFailure
          error={query.error}
          title="Cannot load investigations"
          source="Investigation record"
          consequence="Aegis is unreachable. An empty list here does not mean no investigation ran."
          onRetry={() => query.refetch()}
        />
      ) : rows.length === 0 ? (
        <EmptyState
          title="No investigation has been recorded."
          detail="Investigations appear as soon as the orchestrator dispatches its first agent for an incident."
        />
      ) : (
        <div className="card overflow-hidden">
          <table className="w-full border-collapse text-body">
            <caption className="sr-only">Investigations, most recent first</caption>
            <thead>
              <tr className="border-b border-hairline text-left">
                {['Sev', 'Incident', 'State', 'Environment', 'Confidence', 'Agent runs',
                  'Agent time', 'Last activity'].map((header) => (
                  <th key={header} scope="col" className="label-meta px-3 py-2 font-semibold">
                    {header}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => {
                const severity = asSeverity(row.severity);
                const state = asIncidentState(row.state);
                return (
                  <tr
                    key={row.incident_id}
                    className="group border-b border-hairline last:border-0
                               transition-colors duration-hover hover:bg-surface-2"
                  >
                    <td className="px-3 py-2.5 align-top">
                      {severity ? (
                        <SeverityBadge severity={severity} />
                      ) : (
                        <span className="text-meta font-semibold text-ink-tertiary">
                          {row.severity}
                        </span>
                      )}
                    </td>
                    <td className="max-w-[420px] px-3 py-2.5 align-top">
                      <Link href={`/investigations/${row.incident_id}`} className="block">
                        <span className="block truncate font-semibold text-ink-primary">
                          {row.title}
                        </span>
                        <span className="block truncate font-mono text-meta font-medium text-ink-tertiary">
                          {row.incident_id}
                        </span>
                      </Link>
                    </td>
                    <td className="px-3 py-2.5 align-top">
                      {state ? (
                        <StateChip state={state} />
                      ) : (
                        <span className="font-medium text-ink-secondary">{row.state}</span>
                      )}
                    </td>
                    <td className="px-3 py-2.5 align-top font-medium text-ink-secondary">
                      {row.environment}
                    </td>
                    <td className="tnum px-3 py-2.5 align-top font-semibold text-ink-secondary">
                      {typeof row.confidence === 'number'
                        ? `${Math.round(row.confidence * 100)}%`
                        : 'Unknown'}
                    </td>
                    <td className="px-3 py-2.5 align-top">
                      <span className="tnum font-semibold text-ink-primary">{row.agent_runs}</span>
                      <span
                        className={cn(
                          'ml-2 text-meta font-semibold',
                          row.failed_runs > 0 ? 'text-status-critical' : 'text-ink-tertiary',
                        )}
                      >
                        {row.failed_runs > 0 ? `${row.failed_runs} failed` : 'none failed'}
                      </span>
                    </td>
                    <td className="tnum px-3 py-2.5 align-top font-medium text-ink-secondary">
                      {formatDuration(row.total_duration_ms)}
                    </td>
                    <td className="px-3 py-2.5 align-top font-medium text-ink-tertiary">
                      {relativeTime(row.last_activity ?? row.created_at)}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
