'use client';

import {
  Bar,
  BarChart,
  CartesianGrid,
  Legend,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts';
import { EmptyState } from '@/components/ui/states';

/**
 * Incident volume across the three windows.
 *
 * Labelled a comparison rather than a trend, deliberately. The API exposes
 * cumulative aggregates, not time buckets, so each bar contains the ones to its
 * left. Drawing that as a rising line would invent a trajectory the data does
 * not support.
 */

export interface WindowDatum {
  window: string;
  incidents: number;
  p1: number;
  resolved: number;
}

export function WindowComparison({ data }: { data: WindowDatum[] }) {
  if (data.length === 0) {
    return (
      <EmptyState
        title="No window has finished loading yet."
        detail="Comparison appears once at least one reliability window returns."
      />
    );
  }

  const summary = data
    .map((d) => `${d.window}: ${d.incidents} incidents, ${d.p1} P1, ${d.resolved} resolved`)
    .join('. ');

  return (
    <div className="space-y-3">
      <div role="img" aria-label={`Incident volume by window. ${summary}.`}>
        <ResponsiveContainer width="100%" height={200}>
          <BarChart data={data} margin={{ top: 8, right: 12, bottom: 4, left: 0 }}>
            <CartesianGrid vertical={false} stroke="var(--border-subtle)" />
            <XAxis
              dataKey="window"
              stroke="var(--text-tertiary)"
              tick={{ fontSize: 10, fill: 'var(--text-secondary)' }}
            />
            <YAxis
              allowDecimals={false}
              stroke="var(--text-tertiary)"
              tick={{ fontSize: 10, fill: 'var(--text-tertiary)' }}
            />
            <Tooltip
              cursor={{ fill: 'var(--surface-3)' }}
              contentStyle={{
                background: 'var(--surface-2)',
                border: '1px solid var(--border-default)',
                borderRadius: 7,
                fontSize: 11,
                fontWeight: 600,
                color: 'var(--text-primary)',
              }}
            />
            <Legend wrapperStyle={{ fontSize: 11, fontWeight: 600, color: 'var(--text-secondary)' }} />
            <Bar dataKey="incidents" name="Incidents" fill="var(--accent)" radius={[3, 3, 0, 0]}
                 isAnimationActive={false} />
            <Bar dataKey="resolved" name="Resolved" fill="var(--status-success)" radius={[3, 3, 0, 0]}
                 isAnimationActive={false} />
            <Bar dataKey="p1" name="P1" fill="var(--status-critical)" radius={[3, 3, 0, 0]}
                 isAnimationActive={false} />
          </BarChart>
        </ResponsiveContainer>
      </div>
      <p className="text-meta font-medium text-ink-tertiary">
        Cumulative windows, not a time series: the 90 day bar contains the 30 and 7 day bars.
      </p>
    </div>
  );
}
