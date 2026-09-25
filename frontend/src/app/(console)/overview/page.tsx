'use client';

import Link from 'next/link';
import { useQuery } from '@tanstack/react-query';
import { ArrowUpRight } from 'lucide-react';
import { api } from '@/lib/api';
import { SeverityBadge, StateChip } from '@/components/ui/primitives';
import { EmptyState, ErrorState, SkeletonRows } from '@/components/ui/states';
import { relativeTime } from '@/lib/utils';
import { HomeSidebar } from '@/components/shell/HomeSidebar';

/**
 * Home is an operator cockpit, not a chart dashboard (UX spec 10).
 * It answers "what needs me right now", in priority order.
 */
const LIVE = ['TRIAGING', 'INVESTIGATING', 'DIAGNOSING', 'DEBUGGING', 'VERIFYING'];

export default function HomePage() {
  const incidents = useQuery({
    queryKey: ['incidents', 'home'],
    queryFn: () => api.listIncidents({ limit: 8 }),
  });

  const open = incidents.data?.items.filter((i) => !i.resolved_at) ?? [];
  const awaiting = open.filter((i) => i.state === 'AWAITING_APPROVAL').length;
  const investigating = open.filter((i) => LIVE.includes(i.state)).length;
  const escalated = open.filter((i) => i.state === 'ESCALATED').length;

  const subtitle = incidents.isLoading
    ? 'Loading current state…'
    : open.length === 0
      ? 'No active incidents.'
      : [
          `${open.length} active incident${open.length === 1 ? '' : 's'}`,
          awaiting ? `${awaiting} awaiting approval` : null,
          investigating ? `${investigating} under investigation` : null,
          escalated ? `${escalated} escalated to a human` : null,
        ]
          .filter(Boolean)
          .join(' · ');

  return (
    <div className="mx-auto max-w-[1440px] px-6 py-7">
      <header className="mb-7">
        <h1 className="text-h2 font-medium tracking-tight">Operations</h1>
        <p className="mt-1 text-body text-ink-secondary">{subtitle}</p>
      </header>

      <div className="grid gap-5 lg:grid-cols-[1fr_300px]">
        <section aria-labelledby="active-incidents">
          <div className="mb-2.5 flex items-baseline justify-between">
            <h2 id="active-incidents" className="label-meta">Active incidents</h2>
            <Link
              href="/incidents"
              className="inline-flex items-center gap-1 text-meta text-ink-tertiary hover:text-ink-secondary"
            >
              All incidents <ArrowUpRight className="h-3 w-3" aria-hidden />
            </Link>
          </div>

          {incidents.isLoading ? (
            <SkeletonRows rows={4} />
          ) : incidents.isError ? (
            <ErrorState
              title="Cannot reach Aegis"
              detail={(incidents.error as Error).message}
              consequence="This is a connectivity problem, not an absence of incidents. Nothing shown here should be read as 'all clear'."
              onRetry={() => incidents.refetch()}
            />
          ) : open.length === 0 ? (
            <EmptyState
              title="No active incidents in this view."
              detail="Nothing is currently open. Resolved incidents remain searchable."
              hint="open the incidents list to review recent history"
            />
          ) : (
            <ul className="space-y-1.5">
              {open.map((incident) => (
                <li key={incident.id}>
                  <Link
                    href={`/incidents/${incident.id}`}
                    className="card block px-3.5 py-3 transition-colors duration-hover hover:bg-surface-2"
                  >
                    <div className="flex items-start gap-3">
                      <SeverityBadge severity={incident.severity} className="mt-0.5" />
                      <div className="min-w-0 flex-1">
                        <p className="truncate text-body font-medium">{incident.title}</p>
                        <p className="mt-0.5 truncate text-meta text-ink-tertiary">
                          {incident.affected_services.join(', ') || 'service not yet localised'}
                        </p>
                      </div>
                      <div className="shrink-0 space-y-1 text-right">
                        <StateChip state={incident.state} />
                        <p className="text-meta text-ink-tertiary">
                          {typeof incident.confidence === 'number'
                            ? `${Math.round(incident.confidence * 100)}% confidence`
                            : 'no conclusion yet'}
                          {' · '}
                          {relativeTime(incident.updated_at)}
                        </p>
                      </div>
                    </div>
                  </Link>
                </li>
              ))}
            </ul>
          )}
        </section>

        <HomeSidebar />
      </div>
    </div>
  );
}
