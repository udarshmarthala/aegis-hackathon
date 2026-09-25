'use client';

import Link from 'next/link';
import { useQuery } from '@tanstack/react-query';
import { consoleApi } from '@/lib/console-api';
import type { DeploymentRow } from '@/lib/console-types';
import { EmptyState, ErrorState, SkeletonRows } from '@/components/ui/states';
import { StateChipText } from '@/components/deployments/PatchList';
import { cn, formatDuration, relativeTime } from '@/lib/utils';

/**
 * Deployments, as a change log.
 *
 * The verification verdict sits next to the state because "deployed" and
 * "verified" are different claims, and a deployment that finished while its
 * verification failed is exactly the row an operator is scanning for.
 */

function durationOf(row: DeploymentRow): string {
  if (!row.finished_at) return 'running';
  const ms = new Date(row.finished_at).getTime() - new Date(row.started_at).getTime();
  return Number.isNaN(ms) ? '—' : formatDuration(ms);
}

function verdictTone(verdict: string | null): string {
  if (verdict === null) return 'text-ink-tertiary';
  const upper = verdict.toUpperCase();
  if (upper.includes('PASS') || upper.includes('HEALTH')) return 'text-status-success';
  if (upper.includes('FAIL') || upper.includes('REGRESS')) return 'text-status-critical';
  return 'text-status-warning';
}

export function DeploymentTable() {
  const query = useQuery({
    queryKey: ['deployments'],
    queryFn: () => consoleApi.deployments({ limit: 100 }),
    refetchInterval: 30_000,
  });

  if (query.isLoading) return <SkeletonRows rows={6} />;
  if (query.isError) {
    return (
      <ErrorState
        title="Cannot load deployments"
        detail={(query.error as Error).message}
        consequence="Recent change history is unavailable, so a deployment cannot be ruled in or out as a cause."
        onRetry={() => query.refetch()}
      />
    );
  }

  const items = query.data?.items ?? [];
  if (items.length === 0) {
    return (
      <EmptyState
        title="No deployments recorded."
        detail="Aegis has not observed or performed a deployment in this window."
        hint="deployments appear here when Aegis executes a rollout or a rollback"
      />
    );
  }

  return (
    <div className="card overflow-hidden">
      <table className="w-full border-collapse text-body">
        <caption className="sr-only">Deployments, newest first</caption>
        <thead>
          <tr className="border-b border-hairline text-left">
            {['Service', 'Environment', 'Change', 'Strategy', 'State', 'Verification', 'Duration', 'Started', 'Incident'].map(
              (header) => (
                <th key={header} scope="col" className="label-meta px-3 py-2 font-semibold">
                  {header}
                </th>
              ),
            )}
          </tr>
        </thead>
        <tbody>
          {items.map((row) => (
            <tr key={row.id} className="border-b border-hairline last:border-0 hover:bg-surface-2">
              <td className="max-w-[240px] px-3 py-2.5 align-top">
                <Link
                  href={`/systems/${encodeURIComponent(row.service_id)}`}
                  className="block truncate font-semibold text-ink-primary"
                >
                  {row.service_id}
                </Link>
              </td>
              <td className="px-3 py-2.5 align-top font-medium text-ink-secondary">
                {row.environment}
              </td>
              <td className="px-3 py-2.5 align-top font-mono text-meta font-medium text-ink-secondary">
                {row.from_version ?? 'unknown'} <span className="text-ink-tertiary">→</span>{' '}
                <span className="text-ink-primary">{row.to_version ?? 'unknown'}</span>
              </td>
              <td className="px-3 py-2.5 align-top font-medium text-ink-secondary">
                {row.strategy}
              </td>
              <td className="px-3 py-2.5 align-top">
                <StateChipText state={row.state} />
                {row.error ? (
                  <p className="mt-1 max-w-[260px] break-words text-meta font-medium text-status-critical">
                    {row.error}
                  </p>
                ) : null}
              </td>
              <td className={cn('px-3 py-2.5 align-top font-semibold', verdictTone(row.verification_verdict))}>
                {row.verification_verdict ?? 'not verified'}
              </td>
              <td className="tnum px-3 py-2.5 align-top font-semibold text-ink-secondary">
                {durationOf(row)}
              </td>
              <td className="px-3 py-2.5 align-top font-medium text-ink-tertiary">
                {relativeTime(row.started_at)}
              </td>
              <td className="px-3 py-2.5 align-top">
                {row.incident_id ? (
                  <Link
                    href={`/incidents/${row.incident_id}`}
                    className="font-mono text-meta font-semibold text-accent
                               transition-opacity duration-hover hover:opacity-80"
                  >
                    {row.incident_id}
                  </Link>
                ) : (
                  <span className="text-meta font-medium text-ink-tertiary">none</span>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
