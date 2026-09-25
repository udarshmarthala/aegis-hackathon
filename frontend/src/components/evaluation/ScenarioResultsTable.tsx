'use client';

import { AlertTriangle } from 'lucide-react';
import { EmptyState } from '@/components/ui/states';
import { cn, formatDuration, num, pct } from '@/lib/utils';
import type { ScenarioResultRow } from '@/lib/console-types';
import { humaniseMetric } from './MetricGrid';

/**
 * Scenario results, with harness failures held apart from model failures.
 *
 * This separation is the difference between a benchmark and a rumour. When
 * Prometheus is down the scenario produces no judgement about the model at all,
 * and folding those runs into a pass rate would score the model on an outage it
 * had nothing to do with.
 */
export function ScenarioResultsTable({ results }: { results: ScenarioResultRow[] }) {
  if (results.length === 0) {
    return (
      <EmptyState
        title="No scenario result was recorded for this run."
        detail="Results appear as the harness scores each scenario."
      />
    );
  }

  const harness = results.filter((row) => row.harness_failure);
  const scored = results.filter((row) => !row.harness_failure);
  const judged = scored.filter((row) => row.passed !== null);
  const passed = judged.filter((row) => row.passed === true).length;

  return (
    <div className="space-y-5">
      <p className="text-meta font-semibold text-ink-secondary" role="status" aria-live="polite">
        {judged.length === 0
          ? 'No scenario produced a judgement about the model.'
          : `${passed} of ${judged.length} judged scenarios passed (${pct(passed / judged.length, 1)}).`}{' '}
        <span className="font-medium text-ink-tertiary">
          {harness.length === 0
            ? 'No harness failure in this run.'
            : `${harness.length} scenario${harness.length === 1 ? '' : 's'} failed in the harness and ${harness.length === 1 ? 'is' : 'are'} excluded from that rate.`}
        </span>
      </p>

      <section aria-labelledby="scored-scenarios">
        <h3 id="scored-scenarios" className="mb-2 text-body font-semibold text-ink-primary">
          Model results
        </h3>
        {scored.length === 0 ? (
          <EmptyState
            title="No scenario reached the model."
            detail="Every scenario in this run failed inside the harness."
          />
        ) : (
          <ResultTable rows={scored} caption="Scenarios that produced a judgement about the model" />
        )}
      </section>

      <section aria-labelledby="harness-failures">
        <h3
          id="harness-failures"
          className="mb-2 flex items-center gap-2 text-body font-semibold text-ink-primary"
        >
          <AlertTriangle className="h-4 w-4 text-status-warning" aria-hidden />
          Harness failures
        </h3>
        <p className="mb-2 max-w-3xl text-meta font-medium text-ink-secondary">
          The environment, a provider or the scenario fixture failed before the model could be
          judged. These runs say nothing about model quality and are excluded from every pass rate
          and aggregate on this page.
        </p>
        {harness.length === 0 ? (
          <p className="rounded-card border border-hairline bg-surface-2 px-3 py-2.5 text-body font-semibold text-ink-secondary">
            None. Every scenario in this run reached the model.
          </p>
        ) : (
          <div className="rounded-card border border-status-warning/30 bg-status-warning/5 p-1">
            <ResultTable rows={harness} caption="Scenarios that failed inside the harness" />
          </div>
        )}
      </section>
    </div>
  );
}

function ResultTable({ rows, caption }: { rows: ScenarioResultRow[]; caption: string }) {
  return (
    <table className="w-full border-collapse text-body">
      <caption className="sr-only">{caption}</caption>
      <thead>
        <tr className="border-b border-hairline text-left">
          {['Scenario', 'Category', 'Result', 'Failure class', 'Duration', 'Metrics'].map((header) => (
            <th key={header} scope="col" className="label-meta px-3 py-2 font-semibold">
              {header}
            </th>
          ))}
        </tr>
      </thead>
      <tbody>
        {rows.map((row) => {
          const metrics = Object.entries(row.metrics);
          return (
            <tr key={row.scenario_id} className="border-b border-hairline align-top last:border-0">
              <th scope="row" className="max-w-[320px] px-3 py-2 text-left">
                <span className="block truncate font-semibold text-ink-primary">{row.title}</span>
                <span className="block truncate font-mono text-meta font-medium text-ink-tertiary">
                  {row.scenario_id}
                </span>
              </th>
              <td className="px-3 py-2 font-medium text-ink-secondary">{row.category}</td>
              <td className="px-3 py-2">
                <ResultChip passed={row.passed} harnessFailure={row.harness_failure} />
              </td>
              <td className="px-3 py-2 font-medium text-ink-secondary">
                {row.failure_class ?? '—'}
              </td>
              <td className="tnum px-3 py-2 font-medium text-ink-secondary">
                {typeof row.duration_ms === 'number' ? formatDuration(row.duration_ms) : '—'}
              </td>
              <td className="max-w-[300px] px-3 py-2">
                {metrics.length === 0 ? (
                  <span className="text-meta font-medium text-ink-tertiary">None recorded</span>
                ) : (
                  <details>
                    <summary className="cursor-pointer text-meta font-semibold text-ink-secondary">
                      {metrics.length} metric{metrics.length === 1 ? '' : 's'}
                    </summary>
                    <dl className="mt-1.5 space-y-0.5">
                      {metrics.map(([key, value]) => (
                        <div key={key} className="flex items-baseline justify-between gap-3">
                          <dt className="text-meta font-medium text-ink-tertiary">
                            {humaniseMetric(key)}
                          </dt>
                          <dd
                            className={
                              value === null
                                ? 'text-meta font-semibold text-ink-tertiary'
                                : 'tnum text-meta font-semibold text-ink-secondary'
                            }
                          >
                            {value === null ? 'not measured' : num(value, 3)}
                          </dd>
                        </div>
                      ))}
                    </dl>
                  </details>
                )}
              </td>
            </tr>
          );
        })}
      </tbody>
    </table>
  );
}

function ResultChip({
  passed,
  harnessFailure,
}: {
  passed: boolean | null;
  harnessFailure: boolean;
}) {
  const label = harnessFailure
    ? 'Harness failure'
    : passed === true
      ? 'Pass'
      : passed === false
        ? 'Fail'
        : 'Not scored';
  const tone = harnessFailure
    ? 'border-status-warning/40 bg-status-warning/10 text-status-warning'
    : passed === true
      ? 'border-status-success/40 bg-status-success/10 text-status-success'
      : passed === false
        ? 'border-status-critical/40 bg-status-critical/10 text-status-critical'
        : 'border-line bg-surface-3 text-ink-secondary';
  return (
    <span
      className={cn(
        'inline-block rounded border px-1.5 py-0.5 text-meta font-semibold uppercase tracking-wider',
        tone,
      )}
    >
      {label}
    </span>
  );
}
