'use client';

import Link from 'next/link';
import { useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { ArrowUpRight } from 'lucide-react';
import { NetworkError, api } from '@/lib/api';
import { consoleApi } from '@/lib/console-api';
import type { PatchRow, SandboxRunRow } from '@/lib/console-types';
import { SeverityBadge, StateChip } from '@/components/ui/primitives';
import {
  EmptyState, ErrorState, Skeleton, SkeletonRows, SourceUnavailableState,
} from '@/components/ui/states';
import { PatchList } from '@/components/deployments/PatchList';
import { SandboxRunList } from '@/components/deployments/SandboxRunList';
import { cn } from '@/lib/utils';

/**
 * The debug workbench (UX spec 32-35).
 *
 * The value of this page is the narrowing: an incident becomes a service, a
 * service becomes a repository, a repository becomes a commit, a commit becomes
 * a set of files, and the files become an executed reproduction. Each step is
 * derived from a record Aegis actually wrote - where there is no record, the
 * step says so rather than inventing a plausible one.
 */

function unique(values: Array<string | null | undefined>): string[] {
  return Array.from(new Set(values.filter((value): value is string => Boolean(value))));
}

function isRepro(run: SandboxRunRow): boolean {
  return /repro/i.test(run.purpose);
}

function isTest(run: SandboxRunRow): boolean {
  return /test|verif/i.test(run.purpose);
}

export default function DebugPage() {
  const [chosen, setChosen] = useState<string | null>(null);

  const incidents = useQuery({
    queryKey: ['incidents', 'debug-picker'],
    queryFn: () => api.listIncidents({ limit: 100 }),
    refetchInterval: 30_000,
  });

  const incidentItems = incidents.data?.items ?? [];
  const incidentId = chosen ?? incidentItems[0]?.id ?? '';
  const incident = incidentItems.find((item) => item.id === incidentId);

  // These two queries share their keys with the ones inside `SandboxRunList`
  // and `PatchList` below, so React Query serves both from one cache entry and
  // one fetch rather than four. The page reads them for the narrowing summary;
  // the lists render the rows.
  const runs = useQuery({
    queryKey: ['sandbox-runs', incidentId],
    queryFn: () => consoleApi.sandboxRuns({ incident_id: incidentId }),
    enabled: incidentId !== '',
    refetchInterval: 30_000,
  });

  const patches = useQuery({
    queryKey: ['patches', incidentId],
    queryFn: () => consoleApi.patches({ incident_id: incidentId }),
    enabled: incidentId !== '',
    refetchInterval: 30_000,
  });

  const runItems: SandboxRunRow[] = runs.data?.items ?? [];
  const patchItems: PatchRow[] = patches.data?.items ?? [];
  const loadingWork = runs.isLoading || patches.isLoading;

  // A failed read of either query makes every count on this page a guess. The
  // claim "nothing has been executed" is only honest when both reads succeeded
  // - otherwise an unreachable backend would be reported as an idle agent.
  const workFailed = runs.isError || patches.isError;
  const workError = runs.error ?? patches.error;
  const workUnknown = loadingWork || workFailed;
  const retryWork = () => {
    void runs.refetch();
    void patches.refetch();
  };
  const nothingExecuted =
    incidentId !== '' &&
    !loadingWork &&
    !workFailed &&
    runItems.length === 0 &&
    patchItems.length === 0;

  const repos = unique([...patchItems.map((p) => p.repo), ...runItems.map((r) => r.repo)]);
  const refs = unique([...patchItems.map((p) => p.base_ref), ...runItems.map((r) => r.base_ref)]);
  const files = unique(patchItems.flatMap((p) => p.files_changed));
  const reproRuns = runItems.filter(isRepro);
  const testRuns = runItems.filter(isTest);

  return (
    <div className="mx-auto max-w-[1600px] px-6 py-7">
      <header className="mb-5 flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-h2 font-semibold tracking-tight">Debug Workbench</h1>
          <p className="mt-1 text-body font-medium text-ink-secondary">
            What Aegis ran, what it changed, and how far it narrowed the fault.
          </p>
        </div>

        <label className="flex items-center gap-2">
          <span className="label-meta font-semibold">Incident</span>
          <select
            value={incidentId}
            onChange={(event) => setChosen(event.target.value)}
            disabled={incidents.isLoading || incidentItems.length === 0}
            className="max-w-[420px] rounded-btn border border-hairline bg-surface-2 px-2 py-1.5
                       text-body font-semibold text-ink-primary disabled:opacity-50"
          >
            {incidentItems.length === 0 ? <option value="">no incidents</option> : null}
            {incidentItems.map((item) => (
              <option key={item.id} value={item.id}>
                {item.severity} · {item.title}
              </option>
            ))}
          </select>
        </label>
      </header>

      {incidents.isLoading ? (
        <SkeletonRows rows={6} />
      ) : incidents.isError ? (
        <ErrorState
          title="Cannot load incidents"
          detail={(incidents.error as Error).message}
          consequence="No incident can be selected, so no debugging context can be assembled."
          onRetry={() => incidents.refetch()}
        />
      ) : incidentItems.length === 0 ? (
        <EmptyState
          title="There are no incidents to debug."
          detail="The workbench works on a selected incident and Aegis has not recorded one."
          hint="open the incident list once an alert has been ingested"
        />
      ) : (
        <div className="space-y-6">
          {/* ------------------------------------------- debug context header */}
          <section aria-labelledby="context" className="card p-4">
            <h2 id="context" className="sr-only">
              Debug context
            </h2>
            <div className="flex flex-wrap items-center gap-2">
              {incident ? <SeverityBadge severity={incident.severity} /> : null}
              <h3 className="min-w-0 break-words text-h3 font-bold tracking-tight text-ink-primary">
                {incident?.title ?? incidentId}
              </h3>
              {incident ? <StateChip state={incident.state} /> : null}
              <Link
                href={`/incidents/${incidentId}`}
                className="ml-auto inline-flex items-center gap-1.5 rounded-btn border border-line px-2.5 py-1.5
                           text-meta font-semibold text-ink-primary transition-colors duration-hover
                           hover:bg-surface-3"
              >
                Open incident
                <ArrowUpRight className="h-3.5 w-3.5" aria-hidden />
              </Link>
            </div>

            <dl className="mt-3 grid gap-3 sm:grid-cols-3 xl:grid-cols-5">
              <Stat
                label="Suspected origin"
                value={incident?.suspected_origin ?? 'not identified'}
                tone={incident?.suspected_origin ? 'normal' : 'muted'}
              />
              <Stat
                label="Reproduction"
                value={
                  loadingWork
                    ? '…'
                    : workFailed
                      ? 'unknown'
                      : reproRuns.length === 0
                        ? 'not attempted'
                        : reproRuns.some((run) => run.exit_code === 0)
                          ? 'reproduced'
                          : 'not reproduced'
                }
                tone={
                  workUnknown || reproRuns.length === 0
                    ? 'muted'
                    : reproRuns.some((run) => run.exit_code === 0)
                      ? 'good'
                      : 'bad'
                }
              />
              <Stat
                label="Test runs"
                value={loadingWork ? '…' : workFailed ? 'unknown' : `${testRuns.length}`}
                tone={!workUnknown && testRuns.length > 0 ? 'normal' : 'muted'}
              />
              <Stat
                label="Candidate patches"
                value={loadingWork ? '…' : workFailed ? 'unknown' : `${patchItems.length}`}
                tone={!workUnknown && patchItems.length > 0 ? 'normal' : 'muted'}
              />
              <Stat
                label="Sandbox executions"
                value={loadingWork ? '…' : workFailed ? 'unknown' : `${runItems.length}`}
                tone={!workUnknown && runItems.length > 0 ? 'normal' : 'muted'}
              />
            </dl>
          </section>

          {workFailed ? (
            workError instanceof NetworkError ? (
              <SourceUnavailableState
                source="Debug records"
                reason={workError.message}
                consequence="Sandbox runs and candidate patches cannot be read for this incident, so this page cannot say what Aegis ran or changed. It is not saying that nothing ran."
                onRetry={retryWork}
              />
            ) : (
              <ErrorState
                title="Cannot load the debugging record"
                detail={(workError as Error).message}
                consequence="Sandbox runs and candidate patches cannot be read for this incident. Treat the counts above as unknown, not as zero."
                onRetry={retryWork}
              />
            )
          ) : nothingExecuted ? (
            <EmptyState
              title="Nothing has been executed for this incident."
              detail="No sandbox run and no candidate patch exists, so there is no debugging session to show. Aegis has not reached the debugging phase here."
              hint="select another incident, or follow this one on its incident page"
              action={
                <Link
                  href={`/incidents/${incidentId}`}
                  className="inline-flex items-center gap-1.5 rounded-btn border border-line px-2.5 py-1.5
                             text-meta font-semibold text-ink-primary transition-colors duration-hover
                             hover:bg-surface-3"
                >
                  Open incident
                  <ArrowUpRight className="h-3.5 w-3.5" aria-hidden />
                </Link>
              }
            />
          ) : (
            <div className="grid gap-6 xl:grid-cols-[minmax(0,300px)_minmax(0,1fr)]">
              {/* ----------------------------------------------- narrowing */}
              <section aria-labelledby="narrowing" className="min-w-0">
                <h2 id="narrowing" className="mb-2 text-h3 font-semibold tracking-tight">
                  Narrowing
                </h2>
                {loadingWork ? (
                  <Skeleton className="h-[320px] w-full" />
                ) : (
                  <ol className="card divide-y divide-hairline">
                    <Stage
                      step="Incident"
                      values={[incidentId]}
                      source="incident record"
                    />
                    <Stage
                      step="Services"
                      values={incident?.affected_services ?? []}
                      source="incident record"
                      absent="No affected service was recorded on this incident."
                    />
                    <Stage
                      step="Repositories"
                      values={repos}
                      source="patches and sandbox runs"
                      absent="No repository has been checked out for this incident."
                    />
                    <Stage
                      step="Commits"
                      values={refs}
                      source="patch base refs"
                      absent="No base commit was pinned."
                    />
                    <Stage
                      step="Files"
                      values={files}
                      source="patch file lists"
                      absent="No file has been identified as changed."
                      note="Symbol-level narrowing is not exposed by the API; files are as far as the record goes."
                    />
                    <Stage
                      step="Tests and reproductions"
                      values={unique([...reproRuns, ...testRuns].map((run) => run.purpose))}
                      source="sandbox runs"
                      absent="Nothing has been executed to confirm the fault."
                    />
                  </ol>
                )}
              </section>

              {/* --------------------------------------------- execution */}
              <div className="min-w-0 space-y-6">
                <section aria-labelledby="sandbox">
                  <h2 id="sandbox" className="mb-2 text-h3 font-semibold tracking-tight">
                    Sandbox runs
                  </h2>
                  <SandboxRunList incidentId={incidentId} />
                </section>

                <section aria-labelledby="patches">
                  <h2 id="patches" className="mb-2 text-h3 font-semibold tracking-tight">
                    Candidate repairs
                  </h2>
                  <PatchList incidentId={incidentId} />
                </section>
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function Stat({
  label,
  value,
  tone,
}: {
  label: string;
  value: string;
  tone: 'normal' | 'good' | 'bad' | 'muted';
}) {
  return (
    <div className="min-w-0">
      <dt className="label-meta font-semibold">{label}</dt>
      <dd
        className={cn(
          'mt-1 break-words text-body font-bold',
          tone === 'good'
            ? 'text-status-success'
            : tone === 'bad'
              ? 'text-status-critical'
              : tone === 'muted'
                ? 'text-ink-tertiary'
                : 'text-ink-primary',
        )}
      >
        {value}
      </dd>
    </div>
  );
}

function Stage({
  step,
  values,
  source,
  absent,
  note,
}: {
  step: string;
  values: string[];
  source: string;
  absent?: string;
  note?: string;
}) {
  return (
    <li className="p-3">
      <div className="flex items-baseline justify-between gap-2">
        <p className="text-body font-bold text-ink-primary">{step}</p>
        <span className="tnum text-meta font-bold text-ink-tertiary">{values.length}</span>
      </div>
      {values.length === 0 ? (
        <p className="mt-1 text-meta font-semibold text-ink-tertiary">
          {absent ?? 'Nothing recorded at this step.'}
        </p>
      ) : (
        <ul className="mt-1.5 space-y-0.5">
          {values.map((value) => (
            <li
              key={value}
              className="break-all font-mono text-meta font-medium text-ink-secondary"
            >
              {value}
            </li>
          ))}
        </ul>
      )}
      <p className="mt-1.5 text-meta font-medium text-ink-tertiary">from {source}</p>
      {note ? <p className="mt-1 text-meta font-medium text-ink-tertiary">{note}</p> : null}
    </li>
  );
}
