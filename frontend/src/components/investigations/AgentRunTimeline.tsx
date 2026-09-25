'use client';

import { EmptyState } from '@/components/ui/states';
import { cn, formatClock, formatDuration, relativeTime } from '@/lib/utils';
import type { AgentRunRow } from '@/lib/console-types';
import { STATUS_DOT_CLASS, STATUS_TONE_CLASS, runStatusTone } from './domain';

/**
 * What each agent was asked to do, and what it reported back.
 *
 * Chain-of-thought is not shown here and is not fetched: the record is task,
 * tools, result, evidence and duration. That is what makes an investigation
 * auditable. Private model reasoning would make it merely voyeuristic, and the
 * UX spec forbids it outright (section 30).
 */
export function AgentRunTimeline({ runs }: { runs: AgentRunRow[] }) {
  if (runs.length === 0) {
    return (
      <EmptyState
        title="No agent run is recorded for this incident."
        detail="Runs appear as the orchestrator dispatches capability agents. An incident triaged deterministically may have none."
      />
    );
  }

  return (
    <ol className="relative space-y-3 border-l border-hairline pl-5">
      {runs.map((run) => {
        const tone = runStatusTone(run.status);
        return (
          <li key={run.id} className="relative">
            <span
              className={cn(
                'absolute -left-[25px] top-2 h-2 w-2 rounded-full ring-4 ring-canvas',
                STATUS_DOT_CLASS[tone],
              )}
              aria-hidden
            />
            <article className="card p-3.5">
              <header className="flex flex-wrap items-start justify-between gap-2">
                <div className="min-w-0">
                  <h3 className="truncate text-body font-semibold text-ink-primary">
                    {run.agent_role}
                  </h3>
                  <p className="mt-0.5 text-meta font-medium text-ink-tertiary">
                    {run.started_at ? (
                      <>
                        <span className="tnum">{formatClock(run.started_at)}</span>
                        {' · '}
                        {relativeTime(run.started_at)}
                      </>
                    ) : (
                      'Start time not recorded'
                    )}
                    {typeof run.duration_ms === 'number'
                      ? ` · ${formatDuration(run.duration_ms)}`
                      : ' · duration not recorded'}
                  </p>
                </div>
                <span
                  className={cn(
                    'shrink-0 rounded border px-1.5 py-0.5 text-meta font-semibold uppercase tracking-wider',
                    STATUS_TONE_CLASS[tone],
                  )}
                >
                  {run.status}
                </span>
              </header>

              <dl className="mt-2.5 space-y-2">
                <div>
                  <dt className="label-meta font-semibold">Task</dt>
                  <dd className="mt-0.5 text-body font-medium text-ink-secondary">
                    {run.task || 'No task statement recorded.'}
                  </dd>
                </div>
                <div>
                  <dt className="label-meta font-semibold">Result</dt>
                  <dd className="mt-0.5 text-body font-medium text-ink-secondary">
                    {run.summary || 'No operational summary was returned by this run.'}
                  </dd>
                </div>
                {run.error ? (
                  <div>
                    <dt className="label-meta font-semibold text-status-critical">Error</dt>
                    <dd className="mt-0.5 break-words text-meta font-semibold text-status-critical">
                      {run.error}
                    </dd>
                  </div>
                ) : null}
              </dl>

              <footer className="mt-3 flex flex-wrap items-center gap-1.5 border-t border-hairline pt-2.5">
                <Meta label="Model" value={run.model ?? 'not recorded'} />
                <Meta label="Provider" value={run.provider ?? 'not recorded'} />
                <Meta label="Prompt" value={run.prompt_version ?? 'not recorded'} />
                <Meta
                  label="Evidence"
                  value={
                    run.evidence_ids.length === 0
                      ? 'none cited'
                      : `${run.evidence_ids.length} reference${run.evidence_ids.length === 1 ? '' : 's'}`
                  }
                />
              </footer>
              {run.evidence_ids.length > 0 ? (
                <ul className="mt-2 flex flex-wrap gap-1">
                  {run.evidence_ids.map((id) => (
                    <li
                      key={id}
                      className="rounded border border-hairline bg-surface-2 px-1.5 py-0.5
                                 font-mono text-meta font-medium text-ink-secondary"
                    >
                      {id}
                    </li>
                  ))}
                </ul>
              ) : null}
            </article>
          </li>
        );
      })}
    </ol>
  );
}

function Meta({ label, value }: { label: string; value: string }) {
  return (
    <span className="inline-flex items-center gap-1 rounded border border-hairline bg-surface-2 px-1.5 py-0.5">
      <span className="text-meta font-semibold uppercase tracking-wider text-ink-tertiary">
        {label}
      </span>
      <span className="text-meta font-semibold text-ink-secondary">{value}</span>
    </span>
  );
}
