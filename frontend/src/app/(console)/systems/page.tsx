'use client';

import Link from 'next/link';
import { useMemo, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { consoleApi } from '@/lib/console-api';
import { isAvailable, type ServiceRow } from '@/lib/console-types';
import { EmptyState, ErrorState, SkeletonRows, SourceUnavailableState } from '@/components/ui/states';
import { HEALTH_ORDER, HealthChip, Readiness, healthRank, type Health } from '@/components/systems/health';
import { cn, num, pct } from '@/lib/utils';

/**
 * Live Systems (UX spec 44).
 *
 * A dense table of what is actually running. The endpoint distinguishes "no
 * services" from "Aegis cannot see your environment", and so does this page:
 * an operator who reads an empty table as "nothing is deployed" when in truth
 * no runtime adapter is configured will draw exactly the wrong conclusion.
 */

type SortKey = 'health' | 'name' | 'error_rate' | 'latency' | 'ready';

const COLUMNS: Array<{ key: SortKey | null; label: string; align?: 'right' }> = [
  { key: 'name', label: 'Service' },
  { key: null, label: 'Environment' },
  { key: 'health', label: 'Health' },
  { key: null, label: 'Version' },
  { key: 'ready', label: 'Ready', align: 'right' },
  { key: 'error_rate', label: 'Error rate', align: 'right' },
  { key: 'latency', label: 'p99', align: 'right' },
  { key: null, label: 'Owner' },
];

function compare(a: ServiceRow, b: ServiceRow, key: SortKey): number {
  switch (key) {
    case 'name':
      return a.name.localeCompare(b.name);
    case 'error_rate':
      return (b.error_rate ?? -1) - (a.error_rate ?? -1);
    case 'latency':
      return (b.latency_p99_ms ?? -1) - (a.latency_p99_ms ?? -1);
    case 'ready':
      return a.ready_instances - a.desired_instances - (b.ready_instances - b.desired_instances);
    case 'health':
    default:
      return healthRank(a.health) - healthRank(b.health) || a.name.localeCompare(b.name);
  }
}

export default function SystemsPage() {
  const [healthFilter, setHealthFilter] = useState<Health[]>([]);
  const [environment, setEnvironment] = useState('');
  const [sort, setSort] = useState<SortKey>('health');

  const query = useQuery({
    queryKey: ['systems', 'services'],
    queryFn: () => consoleApi.services(),
    refetchInterval: 15_000,
  });

  const payload = query.data;
  const available = payload ? isAvailable(payload) : false;
  const all = useMemo<ServiceRow[]>(
    () => (payload && isAvailable(payload) ? payload.items : []),
    [payload],
  );

  const environments = useMemo(
    () => Array.from(new Set(all.map((s) => s.environment))).sort(),
    [all],
  );

  const rows = useMemo(() => {
    const filtered = all.filter(
      (s) =>
        (healthFilter.length === 0 || healthFilter.includes(s.health)) &&
        (environment === '' || s.environment === environment),
    );
    return [...filtered].sort((a, b) => compare(a, b, sort));
  }, [all, healthFilter, environment, sort]);

  const attention = all.filter((s) => s.health === 'critical' || s.health === 'degraded').length;

  function toggleHealth(value: Health) {
    setHealthFilter((prev) =>
      prev.includes(value) ? prev.filter((h) => h !== value) : [...prev, value],
    );
  }

  return (
    <div className="mx-auto max-w-[1600px] px-6 py-7">
      <header className="mb-5 flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-h2 font-semibold tracking-tight">Live Systems</h1>
          <p className="mt-1 text-body font-medium text-ink-secondary" aria-live="polite">
            {query.isLoading
              ? 'Reading the runtime adapter…'
              : available
                ? `${all.length} services observed · ${attention} needing attention`
                : 'Runtime state unavailable'}
          </p>
        </div>

        {available && all.length > 0 ? (
          <div className="flex flex-wrap items-center gap-1.5" role="group" aria-label="Filters">
            {HEALTH_ORDER.map((value) => (
              <button
                key={value}
                type="button"
                onClick={() => toggleHealth(value)}
                aria-pressed={healthFilter.includes(value)}
                className={cn(
                  'rounded-btn border px-2 py-1 text-meta font-semibold capitalize transition-colors duration-hover',
                  healthFilter.includes(value)
                    ? 'border-edge bg-surface-3 text-ink-primary'
                    : 'border-hairline text-ink-tertiary hover:bg-surface-2',
                )}
              >
                {value}
              </button>
            ))}
            <label className="ml-2 flex items-center gap-1.5">
              <span className="label-meta font-semibold">Environment</span>
              <select
                value={environment}
                onChange={(event) => setEnvironment(event.target.value)}
                className="rounded-btn border border-hairline bg-surface-2 px-2 py-1
                           text-meta font-semibold text-ink-primary"
              >
                <option value="">all</option>
                {environments.map((env) => (
                  <option key={env} value={env}>
                    {env}
                  </option>
                ))}
              </select>
            </label>
          </div>
        ) : null}
      </header>

      {query.isLoading ? (
        <SkeletonRows rows={8} />
      ) : query.isError ? (
        <ErrorState
          title="Cannot load live systems"
          detail={(query.error as Error).message}
          consequence="Aegis is unreachable. This is not a statement about what is running."
          onRetry={() => query.refetch()}
        />
      ) : payload && !isAvailable(payload) ? (
        <SourceUnavailableState
          source="Runtime adapter"
          reason={payload.reason}
          consequence="Aegis cannot see the running environment. This is not an empty environment — no service state can be reported until an adapter is reachable."
          onRetry={() => query.refetch()}
        />
      ) : all.length === 0 ? (
        <EmptyState
          title="The runtime adapter reports no services."
          detail="Aegis reached the environment successfully and found nothing deployed."
          hint="deploy the reference workload, or point the adapter at the right namespace"
        />
      ) : rows.length === 0 ? (
        <EmptyState
          title="No services match this view."
          detail={`${all.length} services are running; none match the current filters.`}
          hint="clear the health filters or switch the environment back to all"
        />
      ) : (
        <div className="card overflow-hidden">
          <table className="w-full border-collapse text-body">
            <caption className="sr-only">
              Services in the observed environment, sorted by {sort}
            </caption>
            <thead>
              <tr className="border-b border-hairline text-left">
                {COLUMNS.map((column) => {
                  const active = column.key !== null && sort === column.key;
                  // Worst-first columns really are descending; saying otherwise
                  // to a screen reader is a small lie with a real cost.
                  const descending = column.key === 'error_rate' || column.key === 'latency';
                  return (
                    <th
                      key={column.label}
                      scope="col"
                      aria-sort={active ? (descending ? 'descending' : 'ascending') : 'none'}
                      className={cn(
                        'label-meta px-3 py-2 font-semibold',
                        column.align === 'right' && 'text-right',
                      )}
                    >
                      {column.key ? (
                        <button
                          type="button"
                          onClick={() => setSort(column.key as SortKey)}
                          className={cn(
                            'uppercase tracking-wider transition-colors duration-hover hover:text-ink-primary',
                            active && 'text-ink-primary',
                          )}
                        >
                          {column.label}
                        </button>
                      ) : (
                        column.label
                      )}
                    </th>
                  );
                })}
              </tr>
            </thead>
            <tbody>
              {rows.map((service) => (
                <tr
                  key={service.service_id}
                  className="group border-b border-hairline transition-colors duration-hover
                             last:border-0 hover:bg-surface-2"
                >
                  <td className="max-w-[320px] px-3 py-2.5 align-top">
                    <Link
                      href={`/systems/${encodeURIComponent(service.service_id)}`}
                      className="block"
                    >
                      <span className="block truncate font-semibold text-ink-primary">
                        {service.name}
                      </span>
                      <span className="block truncate font-mono text-meta font-medium text-ink-tertiary">
                        {service.workload}
                      </span>
                    </Link>
                  </td>
                  <td className="px-3 py-2.5 align-top font-medium text-ink-secondary">
                    {service.environment}
                  </td>
                  <td className="px-3 py-2.5 align-top">
                    <HealthChip health={service.health} />
                  </td>
                  <td className="px-3 py-2.5 align-top font-mono text-meta font-medium text-ink-secondary">
                    {service.version ?? '—'}
                  </td>
                  <td className="px-3 py-2.5 text-right align-top">
                    <Readiness ready={service.ready_instances} desired={service.desired_instances} />
                  </td>
                  <td
                    className={cn(
                      'tnum px-3 py-2.5 text-right align-top font-semibold',
                      service.error_rate !== null && service.error_rate > 0.01
                        ? 'text-status-critical'
                        : 'text-ink-secondary',
                    )}
                  >
                    {pct(service.error_rate, 2)}
                  </td>
                  <td className="tnum px-3 py-2.5 text-right align-top font-semibold text-ink-secondary">
                    {service.latency_p99_ms === null ? '—' : `${num(service.latency_p99_ms, 0)}ms`}
                  </td>
                  <td className="px-3 py-2.5 align-top font-medium text-ink-secondary">
                    {service.owner_team ?? 'unassigned'}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
