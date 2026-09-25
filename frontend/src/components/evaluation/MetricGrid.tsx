'use client';

import { EmptyState } from '@/components/ui/states';
import { num } from '@/lib/utils';

/**
 * Benchmark metrics.
 *
 * Values are rendered exactly as the harness reported them. The API carries no
 * units, so nothing here rescales a number into a percentage it might not be -
 * a 0.42 that is dollars must not be printed as 42%.
 *
 * A metric present but null is "not measured", which is deliberately different
 * from a metric that scored zero.
 */
export function humaniseMetric(key: string): string {
  const spaced = key.replace(/_/g, ' ');
  return spaced.charAt(0).toUpperCase() + spaced.slice(1);
}

export function MetricGrid({
  metrics,
  emptyTitle,
  emptyDetail,
}: {
  metrics: Record<string, number | null>;
  emptyTitle: string;
  emptyDetail: string;
}) {
  const entries = Object.entries(metrics);
  if (entries.length === 0) {
    return <EmptyState title={emptyTitle} detail={emptyDetail} />;
  }

  return (
    <dl className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
      {entries.map(([key, value]) => (
        <div key={key} className="card p-3.5">
          <dt className="label-meta font-semibold">{humaniseMetric(key)}</dt>
          <dd
            className={
              value === null
                ? 'mt-1 text-body font-semibold text-ink-tertiary'
                : 'tnum mt-1 text-h2 font-semibold text-ink-primary'
            }
          >
            {value === null ? 'Not measured' : num(value, 3)}
          </dd>
        </div>
      ))}
    </dl>
  );
}
