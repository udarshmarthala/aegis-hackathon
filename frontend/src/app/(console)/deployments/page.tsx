'use client';

import { useState } from 'react';
import * as Tabs from '@radix-ui/react-tabs';
import { useQuery } from '@tanstack/react-query';
import { NetworkError } from '@/lib/api';
import { consoleApi } from '@/lib/console-api';
import { ErrorState, SourceUnavailableState } from '@/components/ui/states';
import { DeploymentTable } from '@/components/deployments/DeploymentTable';
import { PatchList } from '@/components/deployments/PatchList';
import { SandboxRunList } from '@/components/deployments/SandboxRunList';
import { cn } from '@/lib/utils';

/**
 * Change: what was deployed, what was proposed as a repair, and what was
 * actually executed in a sandbox.
 *
 * Three tabs rather than three pages because they answer one question together
 * - "what did Aegis change, and did it work" - and an operator moves between
 * them constantly while judging a repair.
 */

const TABS = [
  { value: 'deployments', label: 'Deployments', blurb: 'Rollouts and rollbacks Aegis performed or observed.' },
  { value: 'patches', label: 'Patches', blurb: 'Candidate code repairs, with the diff and its test outcome.' },
  { value: 'sandbox', label: 'Sandbox runs', blurb: 'Every command Aegis executed in an isolated container.' },
] as const;

type TabValue = (typeof TABS)[number]['value'];

export default function DeploymentsPage() {
  const [tab, setTab] = useState<TabValue>('deployments');

  // Environment posture is context for everything on this page: a read-only
  // adapter means nothing here was executed by Aegis.
  const environment = useQuery({
    queryKey: ['deployments', 'environments'],
    queryFn: () => consoleApi.environments(),
    refetchInterval: 60_000,
  });

  const active = TABS.find((entry) => entry.value === tab);

  return (
    <div className="mx-auto max-w-[1600px] px-6 py-7">
      <header className="mb-5">
        <h1 className="text-h2 font-semibold tracking-tight">Deployments</h1>
        <p className="mt-1 text-body font-medium text-ink-secondary">
          {active?.blurb}
        </p>
        {environment.data ? (
          <p className="mt-1.5 text-meta font-semibold text-ink-tertiary">
            {environment.data.current} · adapter {environment.data.adapter}
            {environment.data.is_production ? ' · production' : ''}
          </p>
        ) : environment.isError ? (
          <p className="mt-1.5 text-meta font-semibold text-status-warning">
            environment unknown · adapter unknown
          </p>
        ) : null}
      </header>

      {/*
        A failed environments read is reported louder than a successful one,
        because the dangerous direction here is silence: the "no runtime
        adapter" warning below is what tells an operator that nothing on this
        page was executed by Aegis, and gating it on `data` alone would delete
        that warning exactly when we can no longer confirm it.
      */}
      {environment.isError ? (
        <div className="mb-4">
          {environment.error instanceof NetworkError ? (
            <SourceUnavailableState
              source="Environment posture"
              reason={environment.error.message}
              consequence="Aegis cannot say which environment this is, nor whether a runtime adapter exists. Assume nothing below was executed from here until this resolves."
              onRetry={() => environment.refetch()}
            />
          ) : (
            <ErrorState
              title="Cannot read the environment posture"
              detail={(environment.error as Error).message}
              consequence="Whether a runtime adapter is available is unknown, so the records below cannot be attributed to Aegis or to a human."
              onRetry={() => environment.refetch()}
            />
          )}
        </div>
      ) : environment.data && !environment.data.runtime_available ? (
        <div className="mb-4">
          <SourceUnavailableState
            source="Runtime adapter"
            reason="No runtime adapter is available in this environment."
            consequence="Deployments listed below are historical records. Aegis cannot execute or verify a rollout from here."
            onRetry={() => environment.refetch()}
          />
        </div>
      ) : null}

      <Tabs.Root value={tab} onValueChange={(value) => setTab(value as TabValue)}>
        <Tabs.List
          aria-label="Change surfaces"
          className="mb-4 flex flex-wrap gap-1 border-b border-hairline"
        >
          {TABS.map((entry) => (
            <Tabs.Trigger
              key={entry.value}
              value={entry.value}
              className={cn(
                '-mb-px border-b-2 px-3 py-2 text-body font-semibold transition-colors duration-hover',
                tab === entry.value
                  ? 'border-accent text-ink-primary'
                  : 'border-transparent text-ink-tertiary hover:text-ink-secondary',
              )}
            >
              {entry.label}
            </Tabs.Trigger>
          ))}
        </Tabs.List>

        <Tabs.Content value="deployments" className="focus:outline-none">
          <DeploymentTable />
        </Tabs.Content>
        <Tabs.Content value="patches" className="focus:outline-none">
          <PatchList />
        </Tabs.Content>
        <Tabs.Content value="sandbox" className="focus:outline-none">
          <SandboxRunList />
        </Tabs.Content>
      </Tabs.Root>
    </div>
  );
}
