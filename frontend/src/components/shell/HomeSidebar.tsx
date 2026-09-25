'use client';

import Link from 'next/link';
import { useQuery } from '@tanstack/react-query';
import { ShieldQuestion } from 'lucide-react';
import { api } from '@/lib/api';
import { SkeletonRows, WorkingIndicator } from '@/components/ui/states';
import { cn } from '@/lib/utils';

/**
 * An unread policy rendered as an explicit unknown.
 *
 * Falling back to "Observe only" would assert the safest-sounding posture from
 * a read that never landed — a reassuring claim with nothing behind it.
 */
function PolicyUnknown({ what }: { what: string }) {
  return (
    <span className="inline-flex items-center gap-1.5 text-body text-status-warning">
      <ShieldQuestion className="h-3.5 w-3.5 shrink-0" aria-hidden />
      <span aria-hidden>Unknown</span>
      <span className="sr-only">{what} unknown — the policy endpoint could not be read.</span>
    </span>
  );
}

/**
 * Integration health and autonomy posture.
 *
 * Both are on the home page by design: an operator should never have to open a
 * settings page to learn that metric evidence is unavailable or that Aegis is
 * currently allowed to act (UX spec 10, 40).
 */
export function HomeSidebar() {
  const health = useQuery({
    queryKey: ['health'],
    queryFn: api.health,
    refetchInterval: 30_000,
    retry: 1,
  });
  const policy = useQuery({ queryKey: ['policy'], queryFn: api.policy, retry: 1 });

  const killed = policy.data?.kill_switches.any_engaged ?? false;
  // A failed read and a settled read that returned nothing are the same fact
  // here: the posture below is not known and must not be stated.
  const policyUnread = policy.isError || (!policy.isLoading && !policy.data);

  return (
    <aside className="space-y-5">
      <section aria-labelledby="system-health">
        <h2 id="system-health" className="mb-2.5 label-meta">System health</h2>
        <div className="card divide-y divide-hairline">
          {health.isLoading ? (
            <div className="p-3"><SkeletonRows rows={3} /></div>
          ) : health.isError ? (
            <p className="p-3 text-meta text-status-critical">
              Health endpoint unreachable — integration status is unknown.
            </p>
          ) : (
            Object.entries(health.data?.components ?? {}).map(([name, component]) => {
              const ok = component.status === 'healthy' || component.status === 'configured';
              return (
                <div key={name} className="flex items-center justify-between px-3 py-2">
                  <span className="text-body text-ink-secondary">{name}</span>
                  <span className="flex items-center gap-1.5" title={component.affects.join(', ')}>
                    <span
                      className={cn(
                        'h-1.5 w-1.5 rounded-full',
                        ok
                          ? 'bg-status-success'
                          : component.hard_dependency
                            ? 'bg-status-critical'
                            : 'bg-status-warning',
                      )}
                      aria-hidden
                    />
                    <span className={cn('text-meta', ok ? 'text-ink-tertiary' : 'text-status-warning')}>
                      {component.status}
                    </span>
                    {/* What a degraded component costs is the operative fact,
                        so it cannot live only in a mouse-only tooltip. */}
                    {component.affects.length ? (
                      <span className="sr-only">Affects {component.affects.join(', ')}.</span>
                    ) : null}
                  </span>
                </div>
              );
            })
          )}
        </div>
        {health.data?.degraded_components.length ? (
          <p className="mt-2 text-meta text-ink-tertiary">
            Aegis continues with reduced confidence where a source is unavailable.
            Missing evidence is recorded, never assumed absent.
          </p>
        ) : null}
      </section>

      <section aria-labelledby="autonomy-panel">
        <h2 id="autonomy-panel" className="mb-2.5 label-meta">Autonomy</h2>
        <div className="card space-y-2 p-3.5">
          {policy.isLoading ? (
            <WorkingIndicator label="Reading autonomy policy" />
          ) : (
            <>
              <div className="flex items-center justify-between">
                <span className="text-body text-ink-secondary">Status</span>
                {policyUnread ? (
                  <PolicyUnknown what="Autonomy posture" />
                ) : (
                  <span
                    className={cn('text-body', killed ? 'text-status-critical' : 'text-ink-primary')}
                  >
                    {killed
                      ? 'Halted'
                      : policy.data?.autonomy.enabled
                        ? policy.data.autonomy.mode
                        : 'Observe only'}
                  </span>
                )}
              </div>
              <div className="flex items-center justify-between">
                <span className="text-body text-ink-secondary">Allowed tiers</span>
                {policyUnread ? (
                  <PolicyUnknown what="Allowed tiers" />
                ) : (
                  <span className="tnum text-body">
                    {policy.data?.autonomy.allowed_tiers.join(', ') || 'none'}
                  </span>
                )}
              </div>
              <div className="flex items-center justify-between">
                <span className="text-body text-ink-secondary">Policy version</span>
                {policyUnread ? (
                  <PolicyUnknown what="Policy version" />
                ) : (
                  <span className="font-mono text-meta text-ink-tertiary">
                    {policy.data?.policy_version ?? '—'}
                  </span>
                )}
              </div>
            </>
          )}
          <Link href="/policies" className="block pt-1 text-meta text-accent hover:underline">
            Open governance controls
          </Link>
        </div>
      </section>
    </aside>
  );
}
