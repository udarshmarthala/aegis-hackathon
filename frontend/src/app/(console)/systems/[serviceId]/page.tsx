'use client';

import Link from 'next/link';
import { useParams } from 'next/navigation';
import { useQuery } from '@tanstack/react-query';
import { ChevronLeft, Network } from 'lucide-react';
import { consoleApi } from '@/lib/console-api';
import { isAvailable } from '@/lib/console-types';
import { ErrorState, Skeleton, SourceUnavailableState } from '@/components/ui/states';
import { HealthChip, Readiness } from '@/components/systems/health';
import { InstancesPanel } from '@/components/systems/InstancesPanel';
import { ServiceMetricsPanel } from '@/components/systems/ServiceMetricsPanel';
import { Field } from '@/components/approvals/detail-primitives';
import { num, pct } from '@/lib/utils';

/**
 * Service detail (UX spec 45): the long-term operational identity of one
 * service - what is running, how it is behaving, and where it sits in the
 * topology.
 *
 * The header is driven by the same services query the list page uses, so the
 * two can never disagree, and so an unreachable runtime adapter is reported
 * once, in the same words, on both.
 */
export default function ServiceDetailPage() {
  const params = useParams<{ serviceId: string }>();
  const serviceId = decodeURIComponent(params.serviceId);

  const services = useQuery({
    queryKey: ['systems', 'services'],
    queryFn: () => consoleApi.services(),
    refetchInterval: 15_000,
  });

  const payload = services.data;
  const service =
    payload && isAvailable(payload)
      ? payload.items.find((item) => item.service_id === serviceId)
      : undefined;

  return (
    <div className="mx-auto max-w-[1600px] px-6 py-7">
      <Link
        href="/systems"
        className="mb-4 inline-flex items-center gap-1 text-meta font-semibold text-ink-tertiary
                   transition-colors duration-hover hover:text-ink-primary"
      >
        <ChevronLeft className="h-3.5 w-3.5" aria-hidden />
        Live Systems
      </Link>

      <header className="mb-5">
        <div className="flex flex-wrap items-start justify-between gap-4">
          <div className="min-w-0">
            <h1 className="truncate text-h2 font-semibold tracking-tight">
              {service?.name ?? serviceId}
            </h1>
            <p className="mt-1 font-mono text-meta font-medium text-ink-tertiary">{serviceId}</p>
          </div>
          <Link
            href={`/graph?service=${encodeURIComponent(serviceId)}`}
            className="inline-flex items-center gap-2 rounded-btn border border-line px-3 py-1.5
                       text-body font-semibold text-ink-secondary transition-colors duration-hover
                       hover:bg-surface-3 hover:text-ink-primary"
          >
            <Network className="h-4 w-4" aria-hidden />
            Open in service graph
          </Link>
        </div>

        {services.isLoading ? (
          <Skeleton className="mt-4 h-[74px] w-full" />
        ) : services.isError ? (
          <div className="mt-4">
            <ErrorState
              title="Cannot load service state"
              detail={(services.error as Error).message}
              consequence="Health, version and readiness are unknown for this service."
              onRetry={() => services.refetch()}
            />
          </div>
        ) : payload && !isAvailable(payload) ? (
          <div className="mt-4">
            <SourceUnavailableState
              source="Runtime adapter"
              reason={payload.reason}
              consequence="Aegis cannot see this service running. Health below is unknown, not healthy."
              onRetry={() => services.refetch()}
            />
          </div>
        ) : !service ? (
          <div className="mt-4">
            <SourceUnavailableState
              source="Service record"
              reason={`The runtime adapter is reachable but reports no service with id ${serviceId}.`}
              consequence="It may have been removed, renamed, or it lives in another environment. Telemetry below is still queried by id."
              onRetry={() => services.refetch()}
            />
          </div>
        ) : (
          <dl className="card mt-4 grid gap-4 p-4 sm:grid-cols-3 xl:grid-cols-6">
            <Field label="Health" value={<HealthChip health={service.health} />} />
            <Field label="Environment" value={service.environment} />
            <Field
              label="Version"
              value={<span className="font-mono">{service.version ?? '—'}</span>}
            />
            <Field
              label="Ready"
              value={
                <Readiness ready={service.ready_instances} desired={service.desired_instances} />
              }
            />
            <Field
              label="Error rate"
              value={<span className="tnum">{pct(service.error_rate, 2)}</span>}
            />
            <Field
              label="p99 latency"
              value={
                <span className="tnum">
                  {service.latency_p99_ms === null ? '—' : `${num(service.latency_p99_ms, 0)}ms`}
                </span>
              }
            />
            <Field label="Owner" value={service.owner_team ?? 'unassigned'} />
            <Field label="Workload" value={service.workload} />
          </dl>
        )}
      </header>

      <div className="space-y-6">
        <ServiceMetricsPanel serviceId={serviceId} />
        <InstancesPanel serviceId={serviceId} />
      </div>
    </div>
  );
}
