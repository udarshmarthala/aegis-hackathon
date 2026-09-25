'use client';

import { AlertTriangle, Inbox, Loader2, PlugZap, RefreshCw } from 'lucide-react';
import { ApiError, NetworkError } from '@/lib/api';
import { cn } from '@/lib/utils';

/**
 * Empty, error and loading states.
 *
 * The UX spec is emphatic that "No data." is never acceptable and that an
 * unavailable source must never look like an absence of results. Each state
 * here answers: what happened, what is affected, what Aegis did instead, and
 * what the user can do (spec sections 64-66).
 */

export function EmptyState({
  title,
  detail,
  hint,
  action,
}: {
  title: string;
  detail?: string;
  hint?: string;
  action?: React.ReactNode;
}) {
  return (
    <div className="flex flex-col items-center justify-center gap-2 px-6 py-14 text-center">
      <Inbox className="h-5 w-5 text-ink-tertiary" aria-hidden />
      <p className="text-body text-ink-secondary">{title}</p>
      {detail ? <p className="text-meta text-ink-tertiary">{detail}</p> : null}
      {hint ? <p className="text-meta text-ink-tertiary">Try: {hint}</p> : null}
      {action ? <div className="pt-2">{action}</div> : null}
    </div>
  );
}

/**
 * A dependency that could not be reached. Distinct from EmptyState on purpose:
 * this tells the operator that Aegis reduced confidence because it could not
 * see, which is operationally very different from seeing nothing wrong.
 */
export function SourceUnavailableState({
  source,
  reason,
  consequence,
  onRetry,
}: {
  source: string;
  reason: string;
  consequence: string;
  onRetry?: () => void;
}) {
  return (
    <div className="card border-status-warning/30 bg-status-warning/5 p-4">
      <div className="flex items-start gap-3">
        <PlugZap className="mt-0.5 h-4 w-4 shrink-0 text-status-warning" aria-hidden />
        <div className="space-y-1.5">
          <p className="text-body font-medium text-status-warning">{source} unavailable</p>
          <p className="text-meta text-ink-secondary">{reason}</p>
          <p className="text-meta text-ink-tertiary">{consequence}</p>
          {onRetry ? (
            <button
              type="button"
              onClick={onRetry}
              className="mt-1 inline-flex items-center gap-1.5 rounded-btn border border-line px-2 py-1
                         text-meta text-ink-secondary transition-colors duration-hover hover:bg-surface-3"
            >
              <RefreshCw className="h-3 w-3" aria-hidden />
              Retry
            </button>
          ) : null}
        </div>
      </div>
    </div>
  );
}

export function ErrorState({
  title,
  detail,
  consequence,
  onRetry,
}: {
  title: string;
  detail: string;
  consequence?: string;
  onRetry?: () => void;
}) {
  return (
    <div className="card border-status-critical/30 p-5" role="alert">
      <div className="flex items-start gap-3">
        <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0 text-status-critical" aria-hidden />
        <div className="space-y-1.5">
          <p className="text-body font-medium text-status-critical">{title}</p>
          <p className="text-meta text-ink-secondary">{detail}</p>
          {consequence ? <p className="text-meta text-ink-tertiary">{consequence}</p> : null}
          {onRetry ? (
            <button
              type="button"
              onClick={onRetry}
              className="mt-1 inline-flex items-center gap-1.5 rounded-btn border border-line px-2 py-1
                         text-meta text-ink-secondary transition-colors duration-hover hover:bg-surface-3"
            >
              <RefreshCw className="h-3 w-3" aria-hidden />
              Retry
            </button>
          ) : null}
        </div>
      </div>
    </div>
  );
}

/**
 * A failed query, rendered as what actually happened to it.
 *
 * Invariant 6 is decided here, and on two axes rather than one, because a
 * console can lose "could not look" in two different places. A `NetworkError`
 * means Aegis was never reached, so nothing was consulted at all. An `ApiError`
 * carrying `SOURCE_UNAVAILABLE` or `CIRCUIT_OPEN` means Aegis answered and said
 * *its* dependency was down — still a source that could not be consulted, not a
 * broken console. Discriminating on only one of those axes silently demotes the
 * other into a generic failure, which is why this primitive is the single place
 * either decision is made.
 *
 * Anything else — a 500, a 403, a thrown non-Error — is a genuine error and
 * keeps the error treatment.
 */
export function QueryFailure({
  error,
  title,
  source,
  consequence,
  unavailableConsequence,
  onRetry,
}: {
  error: unknown;
  title: string;
  source: string;
  consequence: string;
  /** Used when the failure is a source outage, where the consequence differs. */
  unavailableConsequence?: string;
  onRetry?: () => void;
}) {
  const message = error instanceof Error ? error.message : String(error);
  const unreachable =
    error instanceof NetworkError ||
    (error instanceof ApiError &&
      (error.code === 'SOURCE_UNAVAILABLE' || error.code === 'CIRCUIT_OPEN'));

  if (unreachable) {
    return (
      <SourceUnavailableState
        source={source}
        reason={message}
        consequence={unavailableConsequence ?? consequence}
        onRetry={onRetry}
      />
    );
  }

  // Status, code and correlation id are what an operator quotes to whoever
  // reads the logs, so they belong on screen rather than only in the console.
  let detail = message;
  if (error instanceof ApiError) {
    const markers = [`HTTP ${error.status}`, error.code];
    if (error.correlationId) markers.push(`correlation ${error.correlationId}`);
    detail = `${message} (${markers.join(' · ')})`;
  }

  return <ErrorState title={title} detail={detail} consequence={consequence} onRetry={onRetry} />;
}

/** Structural skeleton, never a centred spinner (UX spec 66). */
export function Skeleton({ className }: { className?: string }) {
  return <div className={cn('animate-pulse rounded bg-surface-3', className)} aria-hidden />;
}

export function SkeletonRows({ rows = 5 }: { rows?: number }) {
  return (
    <div className="space-y-2" aria-busy="true" aria-live="polite">
      {Array.from({ length: rows }).map((_, i) => (
        <Skeleton key={i} className="h-11 w-full" />
      ))}
    </div>
  );
}

/** Named progress, so the user always knows what the system is doing. */
export function WorkingIndicator({ label }: { label: string }) {
  return (
    <span className="inline-flex items-center gap-1.5 text-meta text-ink-secondary">
      <Loader2 className="h-3 w-3 animate-spin" aria-hidden />
      {label}
    </span>
  );
}
