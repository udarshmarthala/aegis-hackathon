'use client';

import Link from 'next/link';
import { useParams } from 'next/navigation';
import { useQuery } from '@tanstack/react-query';
import { ArrowLeft } from 'lucide-react';
import { consoleApi } from '@/lib/console-api';
import { ErrorState, SkeletonRows } from '@/components/ui/states';
import { CalibrationChart } from '@/components/evaluation/CalibrationChart';
import { CategoryAggregates } from '@/components/evaluation/CategoryAggregates';
import { MetricGrid } from '@/components/evaluation/MetricGrid';
import { ScenarioResultsTable } from '@/components/evaluation/ScenarioResultsTable';
import { UnsafeScenarios } from '@/components/evaluation/UnsafeScenarios';
import { SectionCard, StatTile } from '@/components/reliability/panels';
import { formatDuration, relativeTime } from '@/lib/utils';

/**
 * One evaluation run.
 *
 * Four independent reads. Every section states its own failure, and the unsafe
 * section is rendered whatever happens - if it cannot be read, that is said out
 * loud, because a missing safety list must never be mistaken for an empty one.
 */
export default function EvaluationRunPage() {
  const params = useParams<{ runId: string }>();
  const runId = params.runId;

  const run = useQuery({
    queryKey: ['evaluation', 'run', runId],
    queryFn: () => consoleApi.evaluationRun(runId),
    retry: false,
  });

  const scenarios = useQuery({
    queryKey: ['evaluation', 'scenarios', runId],
    queryFn: () => consoleApi.evaluationScenarioResults(runId),
    retry: false,
  });

  const unsafe = useQuery({
    queryKey: ['evaluation', 'unsafe', runId],
    queryFn: () => consoleApi.evaluationUnsafe(runId),
    retry: false,
  });

  const calibration = useQuery({
    queryKey: ['evaluation', 'calibration', runId],
    queryFn: () => consoleApi.evaluationCalibration(runId),
    retry: false,
  });

  const data = run.data;
  const duration = (() => {
    if (!data?.finished_at) return 'Still running';
    const started = new Date(data.started_at).getTime();
    const finished = new Date(data.finished_at).getTime();
    if (Number.isNaN(started) || Number.isNaN(finished)) return '—';
    return formatDuration(finished - started);
  })();

  return (
    <div className="mx-auto max-w-[1500px] px-6 py-7">
      <Link
        href="/evaluation"
        className="mb-4 inline-flex items-center gap-1.5 text-meta font-semibold text-ink-tertiary
                   transition-colors duration-hover hover:text-ink-primary"
      >
        <ArrowLeft className="h-3.5 w-3.5" aria-hidden />
        All evaluation runs
      </Link>

      <header className="mb-5">
        <h1 className="text-h2 font-semibold tracking-tight">
          {data ? data.suite : 'Evaluation run'}
          {data?.ablation ? (
            <span className="ml-2 rounded border border-line px-2 py-0.5 text-meta font-semibold
                             uppercase tracking-wider text-ink-secondary">
              {data.ablation}
            </span>
          ) : null}
        </h1>
        <p className="mt-1 font-mono text-meta font-medium text-ink-tertiary">{runId}</p>
      </header>

      {run.isLoading ? (
        <SkeletonRows rows={4} />
      ) : run.isError ? (
        <div className="mb-4">
          <ErrorState
            title="Cannot load this evaluation run"
            detail={(run.error as Error).message}
            consequence="The run header and its reported metrics are unavailable. The sections below are read separately."
            onRetry={() => run.refetch()}
          />
        </div>
      ) : data ? (
        <div className="mb-4 grid gap-3 sm:grid-cols-2 xl:grid-cols-5">
          <StatTile label="Status" value={data.status} />
          <StatTile label="Scenarios" value={String(data.scenarios_total)} />
          <StatTile label="Scored" value={String(data.scenarios_scored)} />
          <StatTile
            label="Harness failures"
            value={String(data.harness_failures)}
            tone={data.harness_failures > 0 ? 'warning' : 'default'}
            detail="Excluded from every model score"
          />
          <StatTile label="Duration" value={duration} detail={relativeTime(data.started_at)} />
        </div>
      ) : null}

      <div className="space-y-4">
        <SectionCard
          title="Reported metrics"
          description="Exactly as the harness recorded them. A null metric reads as not measured, never as zero."
        >
          {run.isLoading ? (
            <SkeletonRows rows={2} />
          ) : run.isError ? (
            <p className="text-body font-semibold text-ink-tertiary">
              Metrics are unavailable because the run could not be read.
            </p>
          ) : (
            <MetricGrid
              metrics={data?.metrics ?? {}}
              emptyTitle="This run reported no metrics."
              emptyDetail="The harness completed without recording an aggregate."
            />
          )}
        </SectionCard>

        <SectionCard
          title="Unsafe scenarios"
          description="Scenarios in which Aegis proposed or executed an action that the safety evaluators rejected."
        >
          {unsafe.isLoading ? (
            <SkeletonRows rows={2} />
          ) : unsafe.isError ? (
            <ErrorState
              title="Cannot load the unsafe-scenario list"
              detail={(unsafe.error as Error).message}
              consequence="Whether this run produced unsafe actions is unknown. Do not read this as none."
              onRetry={() => unsafe.refetch()}
            />
          ) : (
            <UnsafeScenarios rows={unsafe.data?.items ?? []} />
          )}
        </SectionCard>

        <SectionCard
          title="Results by category"
          description="Pass rates exclude harness failures, which are counted in their own column."
        >
          {scenarios.isLoading ? (
            <SkeletonRows rows={3} />
          ) : scenarios.isError ? (
            <ErrorState
              title="Cannot load scenario results"
              detail={(scenarios.error as Error).message}
              consequence="Per-category aggregates cannot be computed without the scenario results they are derived from."
              onRetry={() => scenarios.refetch()}
            />
          ) : (
            <CategoryAggregates results={scenarios.data?.items ?? []} />
          )}
        </SectionCard>

        <SectionCard
          title="Scenario results"
          description="Model results and harness failures, kept apart."
        >
          {scenarios.isLoading ? (
            <SkeletonRows rows={5} />
          ) : scenarios.isError ? (
            <ErrorState
              title="Cannot load scenario results"
              detail={(scenarios.error as Error).message}
              consequence="No per-scenario outcome is being shown for this run."
              onRetry={() => scenarios.refetch()}
            />
          ) : (
            <ScenarioResultsTable results={scenarios.data?.items ?? []} />
          )}
        </SectionCard>

        <SectionCard
          title="Calibration"
          description="Whether the confidence Aegis stated matched the accuracy it achieved."
        >
          {calibration.isLoading ? (
            <SkeletonRows rows={3} />
          ) : calibration.isError ? (
            <ErrorState
              title="Cannot load calibration"
              detail={(calibration.error as Error).message}
              consequence="Confidence claims for this run cannot be checked against outcomes."
              onRetry={() => calibration.refetch()}
            />
          ) : calibration.data ? (
            <CalibrationChart calibration={calibration.data} />
          ) : null}
        </SectionCard>
      </div>
    </div>
  );
}
