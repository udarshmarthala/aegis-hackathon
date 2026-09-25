'use client';

import Link from 'next/link';
import { useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { api } from '@/lib/api';
import { SeverityBadge, StateChip } from '@/components/ui/primitives';
import { EmptyState, ErrorState, SkeletonRows } from '@/components/ui/states';
import { cn, formatDuration, relativeTime } from '@/lib/utils';
import type { Severity } from '@/lib/types';

/**
 * The incident list is a dense table, not a card grid. When an operator is
 * triaging many incidents at once, scannability beats decoration (UX spec 11).
 */
const SEVERITIES: Severity[] = ['P1', 'P2', 'P3', 'P4'];

export default function IncidentsPage() {
  const [severity, setSeverity] = useState<Severity[]>([]);
  const [openOnly, setOpenOnly] = useState(true);

  const query = useQuery({
    queryKey: ['incidents', severity, openOnly],
    queryFn: () =>
      api.listIncidents({
        severity: severity.length ? severity : undefined,
        limit: 100,
      }),
    refetchInterval: 20_000,
  });

  const rows = (query.data?.items ?? []).filter((i) => (openOnly ? !i.resolved_at : true));

  function toggleSeverity(value: Severity) {
    setSeverity((prev) =>
      prev.includes(value) ? prev.filter((s) => s !== value) : [...prev, value],
    );
  }

  return (
    <div className="mx-auto max-w-[1600px] px-6 py-7">
      <header className="mb-5 flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="text-h2 font-medium tracking-tight">Incidents</h1>
          <p className="mt-1 text-body text-ink-secondary">
            {query.data ? `${query.data.total_open} open` : 'Loading…'}
          </p>
        </div>

        <div className="flex items-center gap-1.5" role="group" aria-label="Filters">
          {SEVERITIES.map((value) => (
            <button
              key={value}
              type="button"
              onClick={() => toggleSeverity(value)}
              aria-pressed={severity.includes(value)}
              className={cn(
                'rounded-btn border px-2 py-1 text-meta transition-colors duration-hover',
                severity.includes(value)
                  ? 'border-edge bg-surface-3 text-ink-primary'
                  : 'border-hairline text-ink-tertiary hover:bg-surface-2',
              )}
            >
              {value}
            </button>
          ))}
          <button
            type="button"
            onClick={() => setOpenOnly((v) => !v)}
            aria-pressed={openOnly}
            className={cn(
              'ml-1.5 rounded-btn border px-2 py-1 text-meta transition-colors duration-hover',
              openOnly
                ? 'border-edge bg-surface-3 text-ink-primary'
                : 'border-hairline text-ink-tertiary hover:bg-surface-2',
            )}
          >
            Open only
          </button>
        </div>
      </header>

      {query.isLoading ? (
        <SkeletonRows rows={8} />
      ) : query.isError ? (
        <ErrorState
          title="Cannot load incidents"
          detail={(query.error as Error).message}
          consequence="Aegis is unreachable. An empty list here does not mean there are no incidents."
          onRetry={() => query.refetch()}
        />
      ) : rows.length === 0 ? (
        <EmptyState
          title="No incidents match this view."
          detail={openOnly ? 'No incidents are currently open.' : 'No incidents recorded.'}
          hint="clear the severity filters or turn off 'Open only'"
        />
      ) : (
        <div className="card overflow-hidden">
          <table className="w-full border-collapse text-body">
            <caption className="sr-only">Incidents, newest first</caption>
            <thead>
              <tr className="border-b border-hairline text-left">
                {['Sev', 'Incident', 'Services', 'State', 'Confidence', 'Age', 'Updated'].map(
                  (header) => (
                    <th
                      key={header}
                      scope="col"
                      className="px-3 py-2 label-meta font-normal"
                    >
                      {header}
                    </th>
                  ),
                )}
              </tr>
            </thead>
            <tbody>
              {rows.map((incident) => {
                const age = Date.now() - new Date(incident.created_at).getTime();
                return (
                  <tr
                    key={incident.id}
                    className="group border-b border-hairline last:border-0
                               transition-colors duration-hover hover:bg-surface-2"
                  >
                    <td className="px-3 py-2.5 align-top">
                      <SeverityBadge severity={incident.severity} />
                    </td>
                    <td className="max-w-[420px] px-3 py-2.5 align-top">
                      <Link href={`/incidents/${incident.id}`} className="block">
                        <span className="block truncate font-medium group-hover:text-ink-primary">
                          {incident.title}
                        </span>
                        <span className="block truncate font-mono text-meta text-ink-tertiary">
                          {incident.id}
                        </span>
                      </Link>
                    </td>
                    <td className="max-w-[220px] px-3 py-2.5 align-top text-ink-secondary">
                      <span className="block truncate">
                        {incident.affected_services.join(', ') || '—'}
                      </span>
                    </td>
                    <td className="px-3 py-2.5 align-top">
                      <StateChip state={incident.state} />
                    </td>
                    <td className="px-3 py-2.5 align-top tnum text-ink-secondary">
                      {typeof incident.confidence === 'number'
                        ? `${Math.round(incident.confidence * 100)}%`
                        : '—'}
                    </td>
                    <td className="px-3 py-2.5 align-top tnum text-ink-tertiary">
                      {formatDuration(age)}
                    </td>
                    <td className="px-3 py-2.5 align-top text-ink-tertiary">
                      {relativeTime(incident.updated_at)}
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
