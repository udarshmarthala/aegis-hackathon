'use client';

import Link from 'next/link';
import { useQuery } from '@tanstack/react-query';
import { FileWarning } from 'lucide-react';
import { consoleApi } from '@/lib/console-api';
import { SkeletonRows } from '@/components/ui/states';
import { AutonomyPanel } from '@/components/settings/AutonomyPanel';
import { CircuitBreakers } from '@/components/settings/CircuitBreakers';
import { IntegrationTable } from '@/components/settings/IntegrationTable';
import { QueryFailure, SectionCard } from '@/components/reliability/panels';

/**
 * Settings.
 *
 * Read-only by design: governance mutations live on /policies, where the kill
 * switch and the action boundary are edited together. This page answers "what
 * is Aegis configured to do, and what can it currently reach" - and says why
 * whenever the answer is "not that".
 */
export default function SettingsPage() {
  const integrations = useQuery({
    queryKey: ['integrations'],
    queryFn: () => consoleApi.integrations(),
    refetchInterval: 60_000,
  });

  const autonomy = useQuery({
    queryKey: ['integrations', 'autonomy'],
    queryFn: () => consoleApi.autonomy(),
    refetchInterval: 60_000,
  });

  const auditFailures = integrations.data?.audit_write_failures ?? 0;

  return (
    <div className="mx-auto max-w-[1400px] px-6 py-7">
      <header className="mb-5">
        <h1 className="text-h2 font-semibold tracking-tight">Settings</h1>
        <p className="mt-1 max-w-3xl text-body font-medium text-ink-secondary">
          Integration health, protective circuit breakers and the current autonomy posture. This
          page reports; governance changes are made on{' '}
          <Link href="/policies" className="font-semibold text-accent underline underline-offset-2">
            Policies
          </Link>
          .
        </p>
      </header>

      {auditFailures > 0 ? (
        <div
          className="card mb-4 flex items-start gap-3 border-status-critical/40 bg-status-critical/5 p-4"
          role="alert"
        >
          <FileWarning className="mt-0.5 h-4 w-4 shrink-0 text-status-critical" aria-hidden />
          <div>
            <p className="text-body font-semibold text-status-critical">
              {auditFailures} audit write{auditFailures === 1 ? '' : 's'} failed
            </p>
            <p className="mt-1 max-w-3xl text-meta font-medium text-ink-secondary">
              Actions may have executed without a durable audit record. The audit trail for this
              period is incomplete and cannot be reconstructed from the UI.
            </p>
          </div>
        </div>
      ) : null}

      <div className="space-y-4">
        <SectionCard
          title="Integrations"
          description="Configured means Aegis has credentials. Healthy means the last probe succeeded. Both can be false for entirely different reasons, and the reason is the useful part."
        >
          {integrations.isLoading ? (
            <SkeletonRows rows={6} />
          ) : integrations.isError ? (
            <QueryFailure
              error={integrations.error}
              title="Cannot load integrations"
              source="Integration registry"
              consequence="Integration health is unknown. This is not a report that every integration is down."
              onRetry={() => integrations.refetch()}
            />
          ) : (
            <IntegrationTable rows={integrations.data?.items ?? []} />
          )}
        </SectionCard>

        <SectionCard
          title="Circuit breakers"
          description="An open breaker means Aegis is deliberately not calling a dependency so it can recover. The integration is not missing — it is being protected."
        >
          {integrations.isLoading ? (
            <SkeletonRows rows={2} />
          ) : integrations.isError ? (
            <p className="text-body font-semibold text-ink-tertiary">
              Breaker state is unavailable because the integrations endpoint could not be read.
            </p>
          ) : (
            <CircuitBreakers breakers={integrations.data?.circuit_breakers ?? {}} />
          )}
        </SectionCard>

        <SectionCard
          title="Autonomy posture"
          description="What Aegis is currently permitted to do without a human, and the state of every kill switch."
        >
          {autonomy.isLoading ? (
            <SkeletonRows rows={4} />
          ) : autonomy.isError ? (
            <QueryFailure
              error={autonomy.error}
              title="Cannot load autonomy posture"
              source="Policy store"
              consequence="What Aegis is allowed to do autonomously is unknown from here. The enforcement path fails closed independently of this page."
              onRetry={() => autonomy.refetch()}
            />
          ) : autonomy.data ? (
            <AutonomyPanel posture={autonomy.data} />
          ) : null}
        </SectionCard>
      </div>
    </div>
  );
}
