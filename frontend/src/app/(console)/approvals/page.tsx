'use client';

import { useMemo } from 'react';
import { useQuery } from '@tanstack/react-query';
import { consoleApi } from '@/lib/console-api';
import { EmptyState, ErrorState, SkeletonRows } from '@/components/ui/states';
import { ApprovalDetail } from '@/components/approvals/ApprovalDetail';
import { expiryOf, useNow } from '@/components/approvals/detail-primitives';

/**
 * The approval queue: the one place a human authorises a change to production.
 *
 * Ordering is by time remaining rather than by arrival, because the cost here
 * is asymmetric. An approval that lapses silently undoes a decision someone
 * already made, and the investigation has to be run again from the top.
 *
 * Every request is rendered in full rather than behind a drawer. Approving is
 * the highest-consequence act in the product; making the operator click to see
 * the blast radius is how "approve on vibes" happens.
 */
export default function ApprovalsPage() {
  const now = useNow();

  const query = useQuery({
    queryKey: ['approvals'],
    queryFn: () => consoleApi.pendingApprovals(50),
    refetchInterval: 15_000,
  });

  const items = useMemo(() => {
    const rows = query.data?.items ?? [];
    return [...rows].sort(
      (a, b) =>
        new Date(a.expires_at).getTime() - new Date(b.expires_at).getTime() ||
        b.action.risk_tier - a.action.risk_tier,
    );
  }, [query.data]);

  const urgent = items.filter((item) => {
    const tone = expiryOf(item.expires_at, now).tone;
    return tone === 'urgent' || tone === 'lapsed';
  }).length;

  return (
    <div className="mx-auto max-w-[1400px] px-6 py-7">
      <header className="mb-5">
        <h1 className="text-h2 font-semibold tracking-tight">Approvals</h1>
        <p className="mt-1 text-body font-medium text-ink-secondary" aria-live="polite">
          {query.isLoading
            ? 'Loading the decision queue…'
            : `${items.length} request${items.length === 1 ? '' : 's'} waiting on a human decision`}
          {urgent > 0 ? (
            <span className="ml-2 font-bold text-status-critical">
              {urgent} expiring now or already lapsed
            </span>
          ) : null}
        </p>
        <p className="mt-1 text-meta font-medium text-ink-tertiary">
          Approval grants permission. The worker still re-runs policy, authorisation, leases and
          verification before anything executes.
        </p>
      </header>

      {query.isLoading ? (
        <SkeletonRows rows={3} />
      ) : query.isError ? (
        <ErrorState
          title="Cannot load pending approvals"
          detail={(query.error as Error).message}
          consequence="Aegis is unreachable. Changes may be waiting on you that this page cannot show — this is not an empty queue."
          onRetry={() => query.refetch()}
        />
      ) : items.length === 0 ? (
        <EmptyState
          title="No approvals are waiting."
          detail="Aegis has not proposed any change that requires a human decision right now."
          hint="check My Tasks for escalations and blocked incidents"
        />
      ) : (
        <div className="space-y-4">
          {items.map((approval) => (
            <ApprovalDetail key={approval.approval_id} approval={approval} now={now} />
          ))}
        </div>
      )}
    </div>
  );
}
