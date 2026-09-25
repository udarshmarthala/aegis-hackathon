'use client';

import { useMemo, useState } from 'react';
import { useQueries, useQuery } from '@tanstack/react-query';
import { consoleApi } from '@/lib/console-api';
import { SkeletonRows } from '@/components/ui/states';
import { CountBars, type CountDatum } from '@/components/reliability/CountBars';
import { QueryFailure, SectionCard, StatTile, WindowSelector } from '@/components/reliability/panels';
import { ServiceLoadTable } from '@/components/reliability/ServiceLoadTable';
import { WindowComparison, type WindowDatum } from '@/components/reliability/WindowComparison';
import { formatDuration } from '@/lib/utils';
import type { Tone } from '@/components/reliability/panels';

/**
 * Reliability overview.
 *
 * The one number this page must never invent is MTTR. `mttr_seconds` is null
 * when nothing resolved inside the window, and a null rendered as 0 reads as
 * instant recovery - a perfect score awarded for having no data.
 */

const WINDOWS = [7, 30, 90] as const;
type Window = (typeof WINDOWS)[number];

const ACTION_LABEL: Record<string, string> = {
  succeeded: 'Succeeded',
  rolled_back: 'Rolled back',
  failed: 'Failed',
  blocked: 'Blocked by policy',
  awaiting_human: 'Awaiting a human',
};

const ACTION_TONE: Record<string, Tone> = {
  succeeded: 'success',
  rolled_back: 'warning',
  failed: 'critical',
  blocked: 'muted',
  awaiting_human: 'warning',
};

function verdictTone(verdict: string): Tone {
  const upper = verdict.toUpperCase();
  if (upper.includes('PASS')) return 'success';
  if (upper.includes('FAIL')) return 'critical';
  if (upper.includes('INCONCLUSIVE')) return 'warning';
  if (upper.includes('UNAVAILABLE')) return 'warning';
  return 'default';
}

function verdictLabel(verdict: string): string {
  const upper = verdict.toUpperCase();
  if (upper.includes('UNAVAILABLE')) return 'Unavailable — could not check';
  return verdict.charAt(0).toUpperCase() + verdict.slice(1).toLowerCase().replace(/_/g, ' ');
}

export default function ReliabilityPage() {
  const [days, setDays] = useState<Window>(7);

  // One query per window, keyed by window, so the selector is instant and the
  // comparison chart reuses exactly the same cached responses.
  const summaries = useQueries({
    queries: WINDOWS.map((window) => ({
      queryKey: ['reliability', 'summary', window],
      queryFn: () => consoleApi.reliabilitySummary(window),
      staleTime: 60_000,
      refetchInterval: 60_000,
    })),
  });

  const active = summaries[WINDOWS.indexOf(days)];
  const summary = active?.data;

  const load = useQuery({
    queryKey: ['reliability', 'service-load', days],
    queryFn: () => consoleApi.serviceLoad(days),
    staleTime: 60_000,
    refetchInterval: 60_000,
  });

  const comparison = useMemo<WindowDatum[]>(() => {
    const rows: WindowDatum[] = [];
    WINDOWS.forEach((window, index) => {
      const data = summaries[index]?.data;
      if (!data) return;
      rows.push({
        window: `${window}d`,
        incidents: data.incidents.total,
        p1: data.incidents.p1,
        resolved: data.incidents.resolved,
      });
    });
    return rows;
  }, [summaries]);

  // A window whose query failed is excluded from the chart and then named in
  // words underneath it, rather than being drawn as a bar.
  //
  // The alternative - an explicit "unavailable" slot - has no honest shape in a
  // bar chart: any bar has a height, and a height of zero reads as a quiet week,
  // which is precisely the misreading this page exists to prevent. A hatched or
  // zero-height bar would also carry no reason. So the bar is dropped and the
  // absence is stated, because a missing bar must never be mistaken for a
  // measured absence of incidents.
  const comparisonGaps = useMemo(
    () =>
      WINDOWS.flatMap((window, index) => {
        const query = summaries[index];
        if (!query?.isError) return [];
        return [
          {
            window,
            reason: query.error instanceof Error ? query.error.message : String(query.error),
            refetch: () => {
              void query.refetch();
            },
          },
        ];
      }),
    [summaries],
  );

  const actionData = useMemo<CountDatum[]>(() => {
    if (!summary) return [];
    const outcomes = Object.keys(ACTION_LABEL);
    const rows: CountDatum[] = outcomes.map((key) => ({
      key,
      label: ACTION_LABEL[key] ?? key,
      value: summary.actions[key] ?? 0,
      tone: ACTION_TONE[key] ?? 'default',
    }));
    const accounted = rows.reduce((sum, row) => sum + row.value, 0);
    const proposed = summary.actions.proposed ?? 0;
    if (proposed > accounted) {
      // Proposed is the total; anything not in a terminal state is still in
      // flight and is shown rather than quietly dropped from the distribution.
      rows.push({
        key: 'in_flight',
        label: 'Still in flight',
        value: proposed - accounted,
        tone: 'muted',
      });
    }
    return rows;
  }, [summary]);

  const verificationData = useMemo<CountDatum[]>(() => {
    if (!summary) return [];
    return Object.entries(summary.verification).map(([verdict, count]) => ({
      key: verdict,
      label: verdictLabel(verdict),
      value: count,
      tone: verdictTone(verdict),
    }));
  }, [summary]);

  const mttrKnown = typeof summary?.incidents.mttr_seconds === 'number';

  return (
    <div className="mx-auto max-w-[1500px] px-6 py-7">
      <header className="mb-5 flex flex-wrap items-end justify-between gap-4">
        <div>
          <h1 className="text-h2 font-semibold tracking-tight">Reliability</h1>
          <p className="mt-1 text-body font-medium text-ink-secondary">
            Incident load, remediation outcomes and verification verdicts over the selected window.
          </p>
        </div>
        <WindowSelector value={days} options={WINDOWS} onChange={setDays} />
      </header>

      {active?.isLoading ? (
        <SkeletonRows rows={6} />
      ) : active?.isError ? (
        <QueryFailure
          error={active.error}
          title="Cannot load reliability aggregates"
          source="Reliability aggregates"
          consequence="No reliability numbers are being shown. Aegis refuses to render zeros it did not measure."
          unavailableConsequence="The aggregate query did not come back, so no window can be summarised. This is a source outage, not a quiet, healthy week."
          onRetry={() => active.refetch()}
        />
      ) : summary ? (
        <div className="space-y-4">
          <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-5">
            <StatTile label="Incidents" value={String(summary.incidents.total)}
                      detail={`Last ${summary.window_days} days`} />
            <StatTile label="Resolved" value={String(summary.incidents.resolved)} tone="success" />
            <StatTile
              label="Open"
              value={String(summary.incidents.open)}
              tone={summary.incidents.open > 0 ? 'warning' : 'default'}
            />
            <StatTile
              label="P1"
              value={String(summary.incidents.p1)}
              tone={summary.incidents.p1 > 0 ? 'critical' : 'default'}
            />
            <StatTile
              label="MTTR"
              value={
                mttrKnown
                  ? formatDuration((summary.incidents.mttr_seconds ?? 0) * 1000)
                  : 'Not enough data'
              }
              tone={mttrKnown ? 'default' : 'muted'}
              detail={
                mttrKnown
                  ? 'Mean time from creation to resolution'
                  : 'No incident resolved in this window, so no mean can be computed'
              }
            />
          </div>

          <div className="grid gap-4 xl:grid-cols-2">
            <SectionCard
              title="Incident volume by window"
              description="How much the last week accounts for against the last month and quarter."
            >
              <WindowComparison data={comparison} />
              {comparisonGaps.length > 0 ? (
                <div
                  role="status"
                  className="mt-3 rounded border border-status-warning/30 bg-status-warning/5 p-2.5"
                >
                  <p className="text-meta font-semibold text-status-warning">
                    {comparisonGaps.map((gap) => `${gap.window}d`).join(' and ')}{' '}
                    {comparisonGaps.length === 1 ? 'is' : 'are'} missing from this chart because the
                    aggregate query failed — absent, not zero.
                  </p>
                  <ul className="mt-1 space-y-0.5">
                    {comparisonGaps.map((gap) => (
                      <li key={gap.window} className="text-meta font-medium text-ink-secondary">
                        {gap.window} days: {gap.reason}
                      </li>
                    ))}
                  </ul>
                  <button
                    type="button"
                    onClick={() => comparisonGaps.forEach((gap) => gap.refetch())}
                    className="mt-2 inline-flex items-center gap-1.5 rounded-btn border border-line px-2 py-1
                               text-meta text-ink-secondary transition-colors duration-hover hover:bg-surface-3"
                  >
                    Retry {comparisonGaps.length === 1 ? 'this window' : 'these windows'}
                  </button>
                </div>
              ) : null}
            </SectionCard>

            <SectionCard
              title="Action outcomes"
              description={`${summary.actions.proposed ?? 0} actions were proposed in this window. Outcomes below.`}
            >
              <CountBars
                data={actionData}
                summary={`Remediation action outcomes over ${summary.window_days} days: ${actionData
                  .map((datum) => `${datum.label} ${datum.value}`)
                  .join(', ')}`}
                emptyTitle="No remediation action was proposed."
                emptyDetail="Aegis investigated without proposing a write in this window."
                valueHeader="Actions"
              />
            </SectionCard>
          </div>

          <SectionCard
            title="Verification verdicts"
            description="Deterministic before-and-after checks on executed actions. Unavailable is its own verdict: it means the check could not run, not that it passed."
          >
            <CountBars
              data={verificationData}
              summary={`Verification verdicts over ${summary.window_days} days: ${verificationData
                .map((datum) => `${datum.label} ${datum.value}`)
                .join(', ')}`}
              emptyTitle="No verification run completed in this window."
              emptyDetail="Verification records appear once an executed action finishes its before-and-after checks."
              valueHeader="Runs"
            />
          </SectionCard>

          <SectionCard
            title="Per-service incident load"
            description={`Attributed from affected services over the last ${days} days.`}
          >
            {load.isLoading ? (
              <SkeletonRows rows={4} />
            ) : load.isError ? (
              <QueryFailure
                error={load.error}
                title="Cannot load per-service load"
                source="Incident store"
                consequence="Service ranking is unavailable; the aggregates above are unaffected."
                onRetry={() => load.refetch()}
              />
            ) : (
              <ServiceLoadTable rows={load.data?.items ?? []} windowDays={days} />
            )}
          </SectionCard>
        </div>
      ) : null}
    </div>
  );
}
