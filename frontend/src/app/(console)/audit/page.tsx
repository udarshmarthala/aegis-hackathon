'use client';

import { useState, type FormEvent } from 'react';
import { useQuery } from '@tanstack/react-query';
import { AlertTriangle, Search, X } from 'lucide-react';
import { consoleApi } from '@/lib/console-api';
import type { AuditEntry } from '@/lib/console-types';
import { EmptyState, ErrorState, SkeletonRows } from '@/components/ui/states';
import { AuditList } from '@/components/audit/AuditList';
import { cn } from '@/lib/utils';

/**
 * The audit trail.
 *
 * Two things this page must never do. It must not let an agent action read like
 * a human decision, because "did a person authorise this" is the entire point
 * of the record. And it must not present the trail as complete when writes have
 * failed - a dropped audit write means the trail has holes, and every
 * completeness claim made from it is qualified from then on.
 */

const ACTOR_TYPES: Array<AuditEntry['actor_type']> = ['human', 'agent', 'system'];

export default function AuditPage() {
  const [actorType, setActorType] = useState('');
  const [eventTypeDraft, setEventTypeDraft] = useState('');
  const [correlationDraft, setCorrelationDraft] = useState('');
  const [eventType, setEventType] = useState('');
  const [correlationId, setCorrelationId] = useState('');

  const query = useQuery({
    queryKey: ['audit', eventType, actorType, correlationId],
    queryFn: () =>
      consoleApi.audit({
        event_type: eventType || undefined,
        actor_type: actorType || undefined,
        correlation_id: correlationId || undefined,
        limit: 200,
      }),
    refetchInterval: 30_000,
  });

  const items = query.data?.items ?? [];
  const writeFailures = query.data?.write_failures ?? 0;
  const humanDecisions = items.filter((entry) => entry.actor_type === 'human').length;
  const filtered = eventType !== '' || actorType !== '' || correlationId !== '';

  function applyFilters(event: FormEvent) {
    event.preventDefault();
    setEventType(eventTypeDraft.trim());
    setCorrelationId(correlationDraft.trim());
  }

  function pivot(value: string) {
    setCorrelationDraft(value);
    setCorrelationId(value);
  }

  function clearFilters() {
    setEventTypeDraft('');
    setCorrelationDraft('');
    setEventType('');
    setCorrelationId('');
    setActorType('');
  }

  return (
    <div className="mx-auto max-w-[1200px] px-6 py-7">
      <header className="mb-4">
        <h1 className="text-h2 font-semibold tracking-tight">Audit</h1>
        <p className="mt-1 text-body font-medium text-ink-secondary" aria-live="polite">
          {query.isLoading
            ? 'Reading the trail…'
            : `${items.length} events · ${humanDecisions} recorded against a human actor`}
        </p>
      </header>

      {writeFailures > 0 ? (
        <div className="card mb-4 border-status-warning/40 bg-status-warning/5 p-4" role="alert">
          <div className="flex items-start gap-3">
            <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0 text-status-warning" aria-hidden />
            <div>
              <p className="text-body font-bold text-status-warning">
                {writeFailures} audit write{writeFailures === 1 ? '' : 's'} failed
              </p>
              <p className="mt-1 text-meta font-medium text-ink-secondary">
                Events were produced that this trail does not contain. Anything below is a partial
                record, and it cannot be used to prove that an action was not taken.
              </p>
            </div>
          </div>
        </div>
      ) : null}

      <form onSubmit={applyFilters} className="card mb-4 flex flex-wrap items-end gap-3 p-3.5">
        <div className="flex items-center gap-1.5" role="group" aria-label="Actor type">
          <span className="label-meta font-semibold">Actor</span>
          {ACTOR_TYPES.map((value) => (
            <button
              key={value}
              type="button"
              onClick={() => setActorType((prev) => (prev === value ? '' : value))}
              aria-pressed={actorType === value}
              className={cn(
                'rounded-btn border px-2 py-1 text-meta font-semibold capitalize transition-colors duration-hover',
                actorType === value
                  ? 'border-edge bg-surface-3 text-ink-primary'
                  : 'border-hairline text-ink-tertiary hover:bg-surface-2',
              )}
            >
              {value}
            </button>
          ))}
        </div>

        <label className="flex min-w-[180px] flex-col gap-1">
          <span className="label-meta font-semibold">Event type</span>
          <input
            value={eventTypeDraft}
            onChange={(event) => setEventTypeDraft(event.target.value)}
            placeholder="approval.decided"
            maxLength={64}
            className="rounded-btn border border-hairline bg-surface-2 px-2 py-1.5
                       font-mono text-meta font-semibold text-ink-primary placeholder:text-ink-tertiary"
          />
        </label>

        <label className="flex min-w-[220px] flex-1 flex-col gap-1">
          <span className="label-meta font-semibold">Correlation id</span>
          <input
            value={correlationDraft}
            onChange={(event) => setCorrelationDraft(event.target.value)}
            placeholder="pivot to everything sharing an id"
            maxLength={64}
            className="rounded-btn border border-hairline bg-surface-2 px-2 py-1.5
                       font-mono text-meta font-semibold text-ink-primary placeholder:text-ink-tertiary"
          />
        </label>

        <button
          type="submit"
          className="inline-flex items-center gap-1.5 rounded-btn border border-line px-3 py-1.5
                     text-meta font-semibold text-ink-primary transition-colors duration-hover
                     hover:bg-surface-3"
        >
          <Search className="h-3.5 w-3.5" aria-hidden />
          Search
        </button>

        {filtered ? (
          <button
            type="button"
            onClick={clearFilters}
            className="inline-flex items-center gap-1.5 rounded-btn border border-hairline px-3 py-1.5
                       text-meta font-semibold text-ink-tertiary transition-colors duration-hover
                       hover:bg-surface-3 hover:text-ink-primary"
          >
            <X className="h-3.5 w-3.5" aria-hidden />
            Clear
          </button>
        ) : null}
      </form>

      {query.isLoading ? (
        <SkeletonRows rows={8} />
      ) : query.isError ? (
        <ErrorState
          title="Cannot load the audit trail"
          detail={(query.error as Error).message}
          consequence="The trail is unreadable from here. An absence of events on this screen proves nothing."
          onRetry={() => query.refetch()}
        />
      ) : items.length === 0 ? (
        <EmptyState
          title={filtered ? 'No events match this filter.' : 'The audit trail is empty.'}
          detail={
            filtered
              ? 'The trail was read successfully and contains no event matching these criteria.'
              : 'Aegis has not recorded an auditable event yet. The store answered; it holds nothing.'
          }
          hint={filtered ? 'clear the filters, or widen the event type' : undefined}
          action={
            filtered ? (
              <button
                type="button"
                onClick={clearFilters}
                className="rounded-btn border border-line px-2.5 py-1.5 text-meta font-semibold
                           text-ink-primary transition-colors duration-hover hover:bg-surface-3"
              >
                Clear filters
              </button>
            ) : undefined
          }
        />
      ) : (
        <AuditList items={items} onPivot={pivot} activeCorrelationId={correlationId} />
      )}
    </div>
  );
}
