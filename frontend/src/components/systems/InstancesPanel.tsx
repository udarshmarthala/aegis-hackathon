'use client';

import { useQuery } from '@tanstack/react-query';
import { consoleApi } from '@/lib/console-api';
import { isAvailable } from '@/lib/console-types';
import { EmptyState, ErrorState, SkeletonRows, SourceUnavailableState } from '@/components/ui/states';
import { HealthDot } from '@/components/systems/health';
import { cn, relativeTime } from '@/lib/utils';

/**
 * The instances behind a service.
 *
 * Restart counts get their own emphasis: a service that reports healthy while
 * quietly restarting every ninety seconds is the failure mode this table exists
 * to expose.
 */
export function InstancesPanel({ serviceId }: { serviceId: string }) {
  const query = useQuery({
    queryKey: ['systems', 'instances', serviceId],
    queryFn: () => consoleApi.instances(serviceId),
    refetchInterval: 15_000,
  });

  const payload = query.data;

  return (
    <section aria-labelledby="instances" className="space-y-3">
      <h2 id="instances" className="text-h3 font-semibold tracking-tight">
        Instances
      </h2>

      {query.isLoading ? (
        <SkeletonRows rows={3} />
      ) : query.isError ? (
        <ErrorState
          title="Cannot load instances"
          detail={(query.error as Error).message}
          consequence="Instance state is unknown. Do not read this as the service having no instances."
          onRetry={() => query.refetch()}
        />
      ) : payload && !isAvailable(payload) ? (
        <SourceUnavailableState
          source="Runtime adapter"
          reason={payload.reason}
          consequence="Aegis cannot enumerate instances for this service, so restart and readiness state are unknown."
          onRetry={() => query.refetch()}
        />
      ) : !payload || payload.items.length === 0 ? (
        <EmptyState
          title="This service has no running instances."
          detail="The runtime adapter answered and reported zero instances — the service is deployed but nothing is up."
          hint="check the scheduler, the image reference and recent deployment failures"
        />
      ) : (
        <div className="card overflow-hidden">
          <table className="w-full border-collapse text-body">
            <caption className="sr-only">Instances of {serviceId}</caption>
            <thead>
              <tr className="border-b border-hairline text-left">
                {['Instance', 'State', 'Health', 'Image', 'Version', 'Restarts', 'Started'].map(
                  (header) => (
                    <th key={header} scope="col" className="label-meta px-3 py-2 font-semibold">
                      {header}
                    </th>
                  ),
                )}
              </tr>
            </thead>
            <tbody>
              {payload.items.map((instance) => (
                <tr key={instance.instance_id} className="border-b border-hairline last:border-0">
                  <td className="max-w-[260px] px-3 py-2.5 align-top">
                    <span className="block truncate font-semibold text-ink-primary">
                      {instance.name}
                    </span>
                    <span className="block truncate font-mono text-meta font-medium text-ink-tertiary">
                      {instance.instance_id}
                    </span>
                  </td>
                  <td className="px-3 py-2.5 align-top font-semibold text-ink-secondary">
                    {instance.state}
                  </td>
                  <td className="px-3 py-2.5 align-top">
                    <HealthDot health={instance.health} />
                  </td>
                  <td className="max-w-[280px] px-3 py-2.5 align-top">
                    <span className="block truncate font-mono text-meta font-medium text-ink-secondary">
                      {instance.image ?? '—'}
                    </span>
                  </td>
                  <td className="px-3 py-2.5 align-top font-mono text-meta font-medium text-ink-secondary">
                    {instance.version ?? '—'}
                  </td>
                  <td
                    className={cn(
                      'tnum px-3 py-2.5 align-top font-bold',
                      instance.restart_count > 0 ? 'text-status-warning' : 'text-ink-secondary',
                    )}
                  >
                    {instance.restart_count}
                  </td>
                  <td className="px-3 py-2.5 align-top font-medium text-ink-tertiary">
                    {relativeTime(instance.started_at)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}
