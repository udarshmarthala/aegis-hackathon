'use client';

import Link from 'next/link';
import { useQuery } from '@tanstack/react-query';
import { consoleApi } from '@/lib/console-api';
import { EmptyState, ErrorState, SkeletonRows } from '@/components/ui/states';
import { humaniseMetric } from '@/components/evaluation/MetricGrid';
import { ScenarioCatalogue } from '@/components/evaluation/ScenarioCatalogue';
import { SectionCard } from '@/components/reliability/panels';
import { formatDuration, num, relativeTime } from '@/lib/utils';
import type { EvaluationRunRow } from '@/lib/console-types';

/**
 * AI evaluation.
 *
 * Never one aggregate score (UX spec 50): a run is listed with its scenario
 * counts, its harness failures and every metric it reported, because a single
 * headline number cannot distinguish a good model from a quiet harness.
 */
function runDuration(run: EvaluationRunRow): string {
  if (!run.finished_at) return 'still running';
  const started = new Date(run.started_at).getTime();
  const finished = new Date(run.finished_at).getTime();
  if (Number.isNaN(started) || Number.isNaN(finished)) return '—';
  return formatDuration(finished - started);
}

export default function EvaluationPage() {
  const runs = useQuery({
    queryKey: ['evaluation', 'runs'],
    queryFn: () => consoleApi.evaluationRuns(25),
    retry: false,
    staleTime: 60_000,
  });

  const catalogue = useQuery({
    queryKey: ['evaluation', 'catalogue'],
    queryFn: () => consoleApi.evaluationCatalogue(),
    retry: false,
    staleTime: 300_000,
  });

  const rows = runs.data?.items ?? [];

  return (
    <div className="mx-auto max-w-[1600px] px-6 py-7">
      <header className="mb-5">
        <h1 className="text-h2 font-semibold tracking-tight">AI evaluation</h1>
        <p className="mt-1 max-w-3xl text-body font-medium text-ink-secondary">
          Benchmark runs against ground-truth incident scenarios. Harness failures are counted
          separately from model failures everywhere on these pages.
        </p>
      </header>

      <div className="space-y-4">
        <SectionCard
          title="Runs"
          description="Most recent first. Open a run for per-category results, calibration and the unsafe-scenario list."
        >
          {runs.isLoading ? (
            <SkeletonRows rows={5} />
          ) : runs.isError ? (
            <ErrorState
              title="Cannot load evaluation runs"
              detail={(runs.error as Error).message}
              consequence="No benchmark history is being shown. This is a failure to read the evaluation store, not a claim that no run exists."
              onRetry={() => runs.refetch()}
            />
          ) : rows.length === 0 ? (
            <EmptyState
              title="No evaluation run has been recorded."
              detail="Runs appear once the benchmark harness completes a suite."
              hint="run eval/run.py --suite smoke"
            />
          ) : (
            <table className="w-full border-collapse text-body">
              <caption className="sr-only">Evaluation runs, most recent first</caption>
              <thead>
                <tr className="border-b border-hairline text-left">
                  {['Run', 'Status', 'Scenarios', 'Harness failures', 'Duration', 'Started',
                    'Metrics'].map((header) => (
                    <th key={header} scope="col" className="label-meta px-3 py-2 font-semibold">
                      {header}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {rows.map((run) => {
                  const metrics = Object.entries(run.metrics);
                  return (
                    <tr
                      key={run.id}
                      className="border-b border-hairline align-top last:border-0
                                 transition-colors duration-hover hover:bg-surface-2"
                    >
                      <th scope="row" className="max-w-[320px] px-3 py-2 text-left">
                        <Link href={`/evaluation/${run.id}`} className="block">
                          <span className="block truncate font-semibold text-ink-primary">
                            {run.suite}
                            {run.ablation ? (
                              <span className="ml-2 rounded border border-line px-1.5 py-0.5
                                               text-meta font-semibold uppercase tracking-wider text-ink-secondary">
                                {run.ablation}
                              </span>
                            ) : null}
                          </span>
                          <span className="block truncate font-mono text-meta font-medium text-ink-tertiary">
                            {run.id}
                          </span>
                        </Link>
                      </th>
                      <td className="px-3 py-2 font-semibold text-ink-secondary">{run.status}</td>
                      <td className="tnum px-3 py-2 font-medium text-ink-secondary">
                        {run.scenarios_scored} / {run.scenarios_total} scored
                      </td>
                      <td
                        className={
                          run.harness_failures > 0
                            ? 'tnum px-3 py-2 font-semibold text-status-warning'
                            : 'tnum px-3 py-2 font-medium text-ink-tertiary'
                        }
                      >
                        {run.harness_failures}
                      </td>
                      <td className="tnum px-3 py-2 font-medium text-ink-secondary">
                        {runDuration(run)}
                      </td>
                      <td className="px-3 py-2 font-medium text-ink-tertiary">
                        {relativeTime(run.started_at)}
                      </td>
                      <td className="max-w-[300px] px-3 py-2">
                        {metrics.length === 0 ? (
                          <span className="text-meta font-medium text-ink-tertiary">
                            None reported
                          </span>
                        ) : (
                          <dl className="space-y-0.5">
                            {metrics.map(([key, value]) => (
                              <div key={key} className="flex items-baseline justify-between gap-3">
                                <dt className="text-meta font-medium text-ink-tertiary">
                                  {humaniseMetric(key)}
                                </dt>
                                <dd className="tnum text-meta font-semibold text-ink-secondary">
                                  {value === null ? 'not measured' : num(value, 3)}
                                </dd>
                              </div>
                            ))}
                          </dl>
                        )}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          )}
        </SectionCard>

        <SectionCard
          title="Scenario catalogue"
          description="The ground truth Aegis is measured against. Every benchmark number on this page is a score on these scenarios."
        >
          {catalogue.isLoading ? (
            <SkeletonRows rows={4} />
          ) : catalogue.isError ? (
            <ErrorState
              title="Cannot load the scenario catalogue"
              detail={(catalogue.error as Error).message}
              consequence="What Aegis is measured against cannot be inspected. Run results above are read separately and are unaffected."
              onRetry={() => catalogue.refetch()}
            />
          ) : (
            <ScenarioCatalogue entries={catalogue.data?.items ?? []} />
          )}
        </SectionCard>
      </div>
    </div>
  );
}
