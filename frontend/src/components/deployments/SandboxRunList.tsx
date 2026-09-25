'use client';

import Link from 'next/link';
import { useQuery } from '@tanstack/react-query';
import { consoleApi } from '@/lib/console-api';
import type { SandboxRunRow } from '@/lib/console-types';
import { EmptyState, ErrorState, SkeletonRows } from '@/components/ui/states';
import { Field, MonoBlock, StructuredDetail } from '@/components/approvals/detail-primitives';
import { cn, formatDuration, relativeTime } from '@/lib/utils';

/**
 * Sandbox executions.
 *
 * Exit code, timeout and kill are three different endings and are reported as
 * three different things: a run the supervisor killed at its resource ceiling
 * tells you about the sandbox, while exit code 1 tells you about the code. The
 * network mode and resource limits are shown because they are the conditions
 * the result is only valid under.
 */

function outcomeOf(run: SandboxRunRow): { label: string; tone: string } {
  if (run.killed) return { label: 'Killed', tone: 'border-status-critical/50 text-status-critical' };
  if (run.timed_out) return { label: 'Timed out', tone: 'border-status-warning/50 text-status-warning' };
  if (run.exit_code === null) {
    return { label: 'No exit code', tone: 'border-status-warning/40 text-status-warning' };
  }
  if (run.exit_code === 0) return { label: 'Exit 0', tone: 'border-status-success/40 text-status-success' };
  return { label: `Exit ${run.exit_code}`, tone: 'border-status-critical/40 text-status-critical' };
}

export function SandboxRunList({ incidentId }: { incidentId?: string }) {
  const query = useQuery({
    queryKey: ['sandbox-runs', incidentId ?? 'all'],
    queryFn: () => consoleApi.sandboxRuns(incidentId ? { incident_id: incidentId } : {}),
    refetchInterval: 30_000,
  });

  if (query.isLoading) return <SkeletonRows rows={3} />;
  if (query.isError) {
    return (
      <ErrorState
        title="Cannot load sandbox runs"
        detail={(query.error as Error).message}
        consequence="Execution history cannot be listed. This is not a statement that nothing ran."
        onRetry={() => query.refetch()}
      />
    );
  }

  const items = query.data?.items ?? [];
  if (items.length === 0) {
    return (
      <EmptyState
        title="Nothing has been executed in the sandbox."
        detail={
          incidentId
            ? 'Aegis has not run a reproduction, a test or a patch verification for this incident.'
            : 'No sandbox execution has been recorded in this window.'
        }
        hint="sandbox runs appear when the debugging agent reproduces a failure or verifies a repair"
      />
    );
  }

  return (
    <ul className="space-y-3">
      {items.map((run) => (
        <SandboxRunCard key={run.id} run={run} />
      ))}
    </ul>
  );
}

export function SandboxRunCard({ run }: { run: SandboxRunRow }) {
  const outcome = outcomeOf(run);

  return (
    <li className="card p-4">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <span
              className={cn(
                'rounded border px-1.5 py-0.5 text-meta font-bold uppercase tracking-wider',
                outcome.tone,
              )}
            >
              {outcome.label}
            </span>
            <h3 className="text-body font-semibold text-ink-primary">{run.purpose}</h3>
          </div>
          <p className="mt-1.5 break-all font-mono text-meta font-medium text-ink-secondary">
            $ {run.command}
          </p>
        </div>
        {run.incident_id ? (
          <Link
            href={`/incidents/${run.incident_id}`}
            className="shrink-0 font-mono text-meta font-semibold text-accent
                       transition-opacity duration-hover hover:opacity-80"
          >
            {run.incident_id}
          </Link>
        ) : null}
      </div>

      <dl className="mt-3 grid gap-3 sm:grid-cols-3 xl:grid-cols-6">
        <Field label="Image" value={<span className="break-all font-mono">{run.image}</span>} />
        <Field
          label="Repo"
          value={
            <span className="break-all font-mono">
              {run.repo ? `${run.repo}@${run.base_ref ?? 'HEAD'}` : '—'}
            </span>
          }
        />
        <Field
          label="Duration"
          value={
            <span className="tnum">
              {run.duration_ms === null ? '—' : formatDuration(run.duration_ms)}
            </span>
          }
        />
        <Field
          label="Network"
          value={
            <span className={cn(run.network === 'none' ? 'text-ink-primary' : 'text-status-warning')}>
              {run.network}
            </span>
          }
        />
        <Field
          label="Timed out"
          value={
            <span className={run.timed_out ? 'text-status-warning' : undefined}>
              {run.timed_out ? 'yes' : 'no'}
            </span>
          }
        />
        <Field
          label="Killed"
          value={
            <span className={run.killed ? 'text-status-critical' : undefined}>
              {run.killed ? 'yes' : 'no'}
            </span>
          }
        />
      </dl>

      <div className="mt-3">
        <h4 className="label-meta mb-1.5 font-semibold">Resource limits</h4>
        <StructuredDetail
          data={run.resource_limits}
          emptyLabel="No resource limits recorded for this run."
          emptyTone="warning"
        />
      </div>

      <div className="mt-3 grid gap-3 lg:grid-cols-2">
        {run.stdout_excerpt ? (
          <MonoBlock label="stdout excerpt" text={run.stdout_excerpt} />
        ) : (
          <p className="text-meta font-semibold text-ink-tertiary">stdout was empty.</p>
        )}
        {run.stderr_excerpt ? (
          <MonoBlock label="stderr excerpt" text={run.stderr_excerpt} tone="critical" />
        ) : (
          <p className="text-meta font-semibold text-ink-tertiary">stderr was empty.</p>
        )}
      </div>

      <p className="mt-2.5 text-meta font-medium text-ink-tertiary">
        started {relativeTime(run.started_at)}
        {run.finished_at ? ` · finished ${relativeTime(run.finished_at)}` : ' · not finished'}
      </p>
    </li>
  );
}
