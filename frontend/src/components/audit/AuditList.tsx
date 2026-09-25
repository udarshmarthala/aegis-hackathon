'use client';

import Link from 'next/link';
import { useState } from 'react';
import { ChevronDown, ChevronRight, Link2 } from 'lucide-react';
import type { AuditEntry } from '@/lib/console-types';
import { ActorBadge, actorRule, isHuman } from '@/components/audit/ActorBadge';
import { MonoBlock } from '@/components/approvals/detail-primitives';
import { cn, formatClock, relativeTime } from '@/lib/utils';

/**
 * Audit records as sentences, not as a database dump (UX spec 83).
 *
 * The raw event stays one click away, because an operator reconstructing a
 * decision eventually needs the payload - but they should not have to read JSON
 * to find out that a human approved a rollback at 10:38.
 */

function readableEvent(eventType: string): string {
  const words = eventType.replace(/[._]/g, ' ').trim();
  return words.charAt(0).toUpperCase() + words.slice(1);
}

export function AuditList({
  items,
  onPivot,
  activeCorrelationId,
}: {
  items: AuditEntry[];
  onPivot: (correlationId: string) => void;
  activeCorrelationId: string;
}) {
  return (
    <ol className="space-y-2">
      {items.map((entry, index) => (
        <AuditRow
          key={entry.id ?? `${entry.created_at}-${entry.event_type}-${index}`}
          entry={entry}
          onPivot={onPivot}
          activeCorrelationId={activeCorrelationId}
        />
      ))}
    </ol>
  );
}

function AuditRow({
  entry,
  onPivot,
  activeCorrelationId,
}: {
  entry: AuditEntry;
  onPivot: (correlationId: string) => void;
  activeCorrelationId: string;
}) {
  const [open, setOpen] = useState(false);
  const rowId = `audit-${entry.id ?? `${entry.created_at}-${entry.event_type}`}`;
  const human = isHuman(entry.actor_type);
  const detailKeys = Object.keys(entry.detail ?? {});

  return (
    <li
      className={cn(
        'card border-l-2 p-3',
        actorRule(entry.actor_type),
        human && 'bg-surface-2',
      )}
    >
      <div className="flex flex-wrap items-start gap-3">
        <button
          type="button"
          onClick={() => setOpen((value) => !value)}
          aria-expanded={open}
          aria-controls={rowId}
          aria-label={open ? 'Hide raw event data' : 'Show raw event data'}
          className="mt-0.5 shrink-0 rounded-btn border border-hairline p-1 text-ink-secondary
                     transition-colors duration-hover hover:bg-surface-3 hover:text-ink-primary"
        >
          {open ? (
            <ChevronDown className="h-3.5 w-3.5" aria-hidden />
          ) : (
            <ChevronRight className="h-3.5 w-3.5" aria-hidden />
          )}
        </button>

        <time
          dateTime={entry.created_at}
          className="tnum shrink-0 font-mono text-meta font-semibold text-ink-secondary"
          title={entry.created_at}
        >
          {formatClock(entry.created_at)}
        </time>

        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            <ActorBadge actorType={entry.actor_type} actor={entry.actor} />
            <span
              className={cn(
                'text-body',
                human ? 'font-bold text-ink-primary' : 'font-semibold text-ink-secondary',
              )}
            >
              {readableEvent(entry.event_type)}
            </span>
          </div>

          <p className="mt-1 break-all font-mono text-meta font-medium text-ink-tertiary">
            {entry.resource_type ? `${entry.resource_type}:${entry.resource_id ?? 'unknown'}` : 'no resource recorded'}
            {' · '}
            {relativeTime(entry.created_at)}
          </p>

          <div className="mt-1.5 flex flex-wrap items-center gap-3">
            {entry.incident_id ? (
              <Link
                href={`/incidents/${entry.incident_id}`}
                className="font-mono text-meta font-semibold text-accent
                           transition-opacity duration-hover hover:opacity-80"
              >
                {entry.incident_id}
              </Link>
            ) : null}
            {entry.correlation_id ? (
              <button
                type="button"
                onClick={() => onPivot(entry.correlation_id as string)}
                aria-pressed={activeCorrelationId === entry.correlation_id}
                className={cn(
                  'inline-flex items-center gap-1 rounded border px-1.5 py-0.5 font-mono text-meta font-semibold',
                  'transition-colors duration-hover',
                  activeCorrelationId === entry.correlation_id
                    ? 'border-edge bg-surface-3 text-ink-primary'
                    : 'border-hairline text-ink-tertiary hover:bg-surface-3 hover:text-ink-primary',
                )}
                title="Show everything sharing this correlation id"
              >
                <Link2 className="h-3 w-3" aria-hidden />
                {entry.correlation_id}
              </button>
            ) : (
              <span className="text-meta font-medium text-ink-tertiary">no correlation id</span>
            )}
          </div>
        </div>
      </div>

      {open ? (
        <div id={rowId} className="mt-3">
          {detailKeys.length === 0 ? (
            <p className="text-meta font-semibold text-ink-tertiary">
              This event carries no detail payload.
            </p>
          ) : (
            <MonoBlock label="Raw event data" text={JSON.stringify(entry.detail, null, 2)} />
          )}
        </div>
      ) : null}
    </li>
  );
}
