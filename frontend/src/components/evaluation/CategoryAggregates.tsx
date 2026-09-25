'use client';

import { useMemo } from 'react';
import { EmptyState } from '@/components/ui/states';
import { num, pct } from '@/lib/utils';
import type { ScenarioResultRow } from '@/lib/console-types';
import { humaniseMetric } from './MetricGrid';

/**
 * Per-category rollup, computed from the scenario results.
 *
 * Harness failures are counted in their own column and excluded from every
 * rate: a category is not weak because the environment fell over while testing
 * it, and an aggregate that cannot tell those apart will send an engineer to
 * fix a model that was never wrong.
 */
interface CategoryRow {
  category: string;
  total: number;
  judged: number;
  passed: number;
  harnessFailures: number;
  metrics: Array<{ key: string; mean: number | null; samples: number }>;
}

function aggregate(results: ScenarioResultRow[]): CategoryRow[] {
  const groups = new Map<string, ScenarioResultRow[]>();
  for (const row of results) {
    const key = row.category || 'uncategorised';
    const list = groups.get(key) ?? [];
    list.push(row);
    groups.set(key, list);
  }

  const rows: CategoryRow[] = [];
  for (const [category, members] of groups) {
    const scored = members.filter((row) => !row.harness_failure);
    const judged = scored.filter((row) => row.passed !== null);
    const metricKeys = new Set<string>();
    for (const row of scored) for (const key of Object.keys(row.metrics)) metricKeys.add(key);

    const metrics = [...metricKeys].sort().map((key) => {
      const values = scored
        .map((row) => row.metrics[key])
        .filter((value): value is number => typeof value === 'number');
      return {
        key,
        mean: values.length === 0
          ? null
          : values.reduce((sum, value) => sum + value, 0) / values.length,
        samples: values.length,
      };
    });

    rows.push({
      category,
      total: members.length,
      judged: judged.length,
      passed: judged.filter((row) => row.passed === true).length,
      harnessFailures: members.length - scored.length,
      metrics,
    });
  }

  return rows.sort((a, b) => a.category.localeCompare(b.category));
}

export function CategoryAggregates({ results }: { results: ScenarioResultRow[] }) {
  const rows = useMemo(() => aggregate(results), [results]);

  if (rows.length === 0) {
    return (
      <EmptyState
        title="No scenario result to aggregate."
        detail="Category rollups appear once the run has scored at least one scenario."
      />
    );
  }

  return (
    <table className="w-full border-collapse text-body">
      <caption className="sr-only">Per-category results for this evaluation run</caption>
      <thead>
        <tr className="border-b border-hairline text-left">
          {['Category', 'Scenarios', 'Judged', 'Passed', 'Pass rate', 'Harness failures', 'Mean metrics'].map(
            (header) => (
              <th key={header} scope="col" className="label-meta px-3 py-2 font-semibold">
                {header}
              </th>
            ),
          )}
        </tr>
      </thead>
      <tbody>
        {rows.map((row) => (
          <tr key={row.category} className="border-b border-hairline align-top last:border-0">
            <th scope="row" className="px-3 py-2 text-left font-semibold text-ink-primary">
              {row.category}
            </th>
            <td className="tnum px-3 py-2 font-medium text-ink-secondary">{row.total}</td>
            <td className="tnum px-3 py-2 font-medium text-ink-secondary">{row.judged}</td>
            <td className="tnum px-3 py-2 font-semibold text-ink-primary">{row.passed}</td>
            <td
              className={
                row.judged === 0
                  ? 'px-3 py-2 font-semibold text-ink-tertiary'
                  : 'tnum px-3 py-2 font-semibold text-ink-primary'
              }
            >
              {row.judged === 0 ? 'Not judged' : pct(row.passed / row.judged, 1)}
            </td>
            <td
              className={
                row.harnessFailures > 0
                  ? 'tnum px-3 py-2 font-semibold text-status-warning'
                  : 'tnum px-3 py-2 font-medium text-ink-tertiary'
              }
            >
              {row.harnessFailures}
            </td>
            <td className="max-w-[320px] px-3 py-2">
              {row.metrics.length === 0 ? (
                <span className="text-meta font-medium text-ink-tertiary">None recorded</span>
              ) : (
                <dl className="space-y-0.5">
                  {row.metrics.map((metric) => (
                    <div key={metric.key} className="flex items-baseline justify-between gap-3">
                      <dt className="text-meta font-medium text-ink-tertiary">
                        {humaniseMetric(metric.key)}
                      </dt>
                      <dd className="tnum text-meta font-semibold text-ink-secondary">
                        {metric.mean === null
                          ? 'not measured'
                          : `${num(metric.mean, 3)} (n=${metric.samples})`}
                      </dd>
                    </div>
                  ))}
                </dl>
              )}
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}
