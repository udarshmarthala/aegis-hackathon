'use client';

import { useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { ShieldCheck } from 'lucide-react';
import { consoleApi } from '@/lib/console-api';
import { isAvailable } from '@/lib/console-types';
import { EmptyState, SkeletonRows, SourceUnavailableState } from '@/components/ui/states';
import { RecurringPatternCard } from '@/components/reliability/RecurringPatternCard';
import { QueryFailure, WindowSelector } from '@/components/reliability/panels';
import { cn } from '@/lib/utils';

/**
 * Recurring failures.
 *
 * Every pattern here comes from verified incident memory. That restriction is
 * the whole value of the page: a hypothesis that recurred but was never
 * confirmed is a repeated guess, and counting it would send an operator to fix
 * something that was never proven broken.
 */

const WINDOWS = [30, 90, 180, 365] as const;
type Window = (typeof WINDOWS)[number];

const THRESHOLDS = [2, 3, 5] as const;
type Threshold = (typeof THRESHOLDS)[number];

export default function RecurringPage() {
  const [days, setDays] = useState<Window>(90);
  const [minOccurrences, setMinOccurrences] = useState<Threshold>(2);

  const query = useQuery({
    queryKey: ['reliability', 'recurring', days, minOccurrences],
    queryFn: () => consoleApi.recurringFailures(days, minOccurrences),
    staleTime: 120_000,
  });

  const data = query.data;
  const patterns = data && isAvailable(data) ? data.items : [];

  return (
    <div className="mx-auto max-w-[1400px] px-6 py-7">
      <header className="mb-5 flex flex-wrap items-end justify-between gap-4">
        <div>
          <h1 className="text-h2 font-semibold tracking-tight">Recurring failures</h1>
          <p className="mt-1 max-w-3xl text-body font-medium text-ink-secondary">
            Failure classes that came back, computed from verified incident memory.
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-3">
          <WindowSelector value={days} options={WINDOWS} onChange={setDays} />
          <div className="flex items-center gap-1" role="group" aria-label="Minimum occurrences">
            <span className="mr-1 text-meta font-semibold uppercase tracking-wider text-ink-tertiary">
              Min occurrences
            </span>
            {THRESHOLDS.map((option) => (
              <button
                key={option}
                type="button"
                onClick={() => setMinOccurrences(option)}
                aria-pressed={minOccurrences === option}
                className={cn(
                  'tnum rounded-btn border px-2 py-1 text-meta font-semibold transition-colors duration-hover',
                  minOccurrences === option
                    ? 'border-edge bg-surface-3 text-ink-primary'
                    : 'border-hairline text-ink-tertiary hover:bg-surface-2',
                )}
              >
                {option}
              </button>
            ))}
          </div>
        </div>
      </header>

      <div className="card mb-4 flex items-start gap-3 p-4">
        <ShieldCheck className="mt-0.5 h-4 w-4 shrink-0 text-status-success" aria-hidden />
        <p className="text-meta font-medium text-ink-secondary">
          <span className="font-semibold text-ink-primary">
            Only verified incident memory is counted.
          </span>{' '}
          A diagnosis that repeated across incidents but was never confirmed by verification is a
          repeated hypothesis, not a recurring failure, and is deliberately excluded. That makes
          this list smaller than a raw clustering of alert text — and actionable, because every
          occurrence behind a count was proven.
        </p>
      </div>

      {query.isLoading ? (
        <SkeletonRows rows={5} />
      ) : query.isError ? (
        <QueryFailure
          error={query.error}
          title="Cannot load recurring failures"
          source="Incident memory"
          consequence="No pattern analysis is being shown. This is a query failure, not an absence of recurrence."
          onRetry={() => query.refetch()}
        />
      ) : data && !isAvailable(data) ? (
        <SourceUnavailableState
          source="Incident memory"
          reason={data.reason}
          consequence="Recurrence cannot be computed. Aegis is not claiming that nothing recurs — it cannot read the memory that would tell it."
          onRetry={() => query.refetch()}
        />
      ) : patterns.length === 0 ? (
        <EmptyState
          title="No verified failure recurred in this window."
          detail={`Nothing reached ${minOccurrences} verified occurrences in the last ${days} days.`}
          hint="widen the window or lower the minimum occurrence count"
        />
      ) : (
        <>
          <p className="mb-3 text-meta font-semibold text-ink-tertiary" role="status" aria-live="polite">
            {patterns.length} pattern{patterns.length === 1 ? '' : 's'} over the last {days} days
          </p>
          <div className="grid gap-3 lg:grid-cols-2">
            {patterns.map((pattern) => (
              <RecurringPatternCard key={pattern.fingerprint} pattern={pattern} />
            ))}
          </div>
        </>
      )}
    </div>
  );
}
