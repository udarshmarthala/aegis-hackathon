'use client';

import Link from 'next/link';
import { useMemo } from 'react';
import { useQuery } from '@tanstack/react-query';
import { ArrowUpRight, ShieldCheck, Siren, XOctagon } from 'lucide-react';
import { consoleApi } from '@/lib/console-api';
import type { OperatorTask } from '@/lib/console-types';
import { SeverityBadge } from '@/components/ui/primitives';
import { EmptyState, ErrorState, SkeletonRows } from '@/components/ui/states';
import {
  ExpiryChip, asSeverity, expiryOf, useNow,
} from '@/components/approvals/detail-primitives';
import { cn, formatClock } from '@/lib/utils';

/**
 * My Tasks (UX spec 43): not a report, a queue.
 *
 * The page answers one question - what is waiting on me - and orders it the way
 * the backend does: approvals first, because an approval that lapses silently
 * undoes a decision a human already made and the whole investigation has to
 * start again.
 */

const GROUPS: Array<{
  kind: OperatorTask['kind'];
  title: string;
  blurb: string;
  icon: typeof ShieldCheck;
}> = [
  {
    kind: 'approval',
    title: 'Approvals',
    blurb: 'A proposed change is waiting on a human decision and will expire.',
    icon: ShieldCheck,
  },
  {
    kind: 'escalation',
    title: 'Escalations',
    blurb: 'An action failed or was rolled back in the last 24 hours.',
    icon: XOctagon,
  },
  {
    kind: 'blocked_incident',
    title: 'Blocked incidents',
    blurb: 'Aegis stopped and needs a human to unblock the investigation.',
    icon: Siren,
  },
];

function linkFor(task: OperatorTask): { href: string; label: string } {
  if (task.kind === 'approval' && task.approval_id) {
    return { href: `/approvals#approval-${task.approval_id}`, label: 'Open approval' };
  }
  return { href: `/incidents/${task.incident_id}`, label: 'Open incident' };
}

export default function TasksPage() {
  const now = useNow();

  const query = useQuery({
    queryKey: ['tasks'],
    queryFn: () => consoleApi.tasks(50),
    refetchInterval: 20_000,
  });

  const items = useMemo(
    () =>
      [...(query.data?.items ?? [])].sort(
        (a, b) => a.priority - b.priority || (a.due_at ?? '9999').localeCompare(b.due_at ?? '9999'),
      ),
    [query.data],
  );

  const expiringSoon = items.filter(
    (task) => task.due_at !== null && expiryOf(task.due_at, now).tone !== 'ok',
  ).length;

  return (
    <div className="mx-auto max-w-[1100px] px-6 py-7">
      <header className="mb-5">
        <h1 className="text-h2 font-semibold tracking-tight">My Tasks</h1>
        <p className="mt-1 text-body font-medium text-ink-secondary" aria-live="polite">
          {query.isLoading
            ? 'Loading your queue…'
            : `${items.length} waiting on ${query.data?.operator ?? 'you'}`}
          {expiringSoon > 0 ? (
            <span className="ml-2 font-bold text-status-warning">
              {expiringSoon} close to expiry
            </span>
          ) : null}
        </p>
      </header>

      {query.isLoading ? (
        <SkeletonRows rows={6} />
      ) : query.isError ? (
        <ErrorState
          title="Cannot load your tasks"
          detail={(query.error as Error).message}
          consequence="Aegis is unreachable. Work may be waiting on you that this page cannot show."
          onRetry={() => query.refetch()}
        />
      ) : items.length === 0 ? (
        <EmptyState
          title="Nothing is waiting on you."
          detail="No open approvals, no failed actions in the last 24 hours, and no blocked incidents."
          hint="watch the incident list — Aegis will raise work here when it needs a decision"
        />
      ) : (
        <div className="space-y-6">
          {GROUPS.map((group) => {
            const groupItems = items.filter((task) => task.kind === group.kind);
            if (groupItems.length === 0) return null;
            const Icon = group.icon;
            return (
              <section key={group.kind} aria-labelledby={`group-${group.kind}`}>
                <div className="mb-2 flex items-baseline gap-2">
                  <Icon className="h-4 w-4 shrink-0 text-ink-tertiary" aria-hidden />
                  <h2 id={`group-${group.kind}`} className="text-h3 font-semibold tracking-tight">
                    {group.title}
                  </h2>
                  <span className="tnum text-meta font-bold text-ink-tertiary">
                    {groupItems.length}
                  </span>
                  <p className="ml-1 truncate text-meta font-medium text-ink-tertiary">
                    {group.blurb}
                  </p>
                </div>

                <ul className="space-y-2">
                  {groupItems.map((task) => (
                    <TaskRow
                      key={`${task.kind}-${task.approval_id ?? task.action_id ?? task.incident_id}`}
                      task={task}
                      now={now}
                    />
                  ))}
                </ul>
              </section>
            );
          })}
        </div>
      )}
    </div>
  );
}

function TaskRow({ task, now }: { task: OperatorTask; now: number }) {
  const severity = asSeverity(task.severity);
  const expiry = task.due_at ? expiryOf(task.due_at, now) : null;
  const urgent = expiry !== null && (expiry.tone === 'urgent' || expiry.tone === 'lapsed');
  const target = linkFor(task);

  return (
    <li>
      <div
        className={cn(
          'card p-3.5 transition-colors duration-hover hover:bg-surface-2',
          urgent && 'border-status-critical/40 bg-status-critical/5',
          expiry?.tone === 'soon' && 'border-status-warning/40',
        )}
      >
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div className="min-w-0 flex-1">
            <div className="flex flex-wrap items-center gap-2">
              {severity ? <SeverityBadge severity={severity} /> : null}
              <h3 className="min-w-0 truncate text-body font-semibold text-ink-primary">
                {task.title}
              </h3>
              {expiry ? <ExpiryChip expiry={expiry} /> : null}
            </div>
            <p className="mt-1.5 break-words text-meta font-medium text-ink-secondary">
              {task.context}
            </p>
            <p className="mt-1 font-mono text-meta font-medium text-ink-tertiary">
              {task.incident_id}
              {task.due_at ? ` · expires ${formatClock(task.due_at)}` : ''}
            </p>
          </div>

          <div className="flex shrink-0 flex-col items-end gap-1.5">
            <Link
              href={target.href}
              className="inline-flex items-center gap-1.5 rounded-btn border border-line px-2.5 py-1.5
                         text-meta font-semibold text-ink-primary transition-colors duration-hover
                         hover:bg-surface-3"
            >
              {target.label}
              <ArrowUpRight className="h-3.5 w-3.5" aria-hidden />
            </Link>
            {task.kind === 'approval' ? (
              <Link
                href={`/incidents/${task.incident_id}`}
                className="text-meta font-semibold text-ink-tertiary transition-colors duration-hover
                           hover:text-ink-primary"
              >
                View incident
              </Link>
            ) : null}
          </div>
        </div>

        {urgent ? (
          <p className="mt-2.5 text-meta font-bold text-status-critical">
            {expiry?.tone === 'lapsed'
              ? 'This approval has lapsed. The decision it carried no longer authorises anything — the action must be proposed again.'
              : 'Decide now. When this expires the action is withdrawn and the investigation has to be re-run.'}
          </p>
        ) : null}
      </div>
    </li>
  );
}
