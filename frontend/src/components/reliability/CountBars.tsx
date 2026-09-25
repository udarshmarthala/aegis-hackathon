'use client';

import {
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts';
import { EmptyState } from '@/components/ui/states';
import type { Tone } from './panels';

/**
 * A distribution, drawn and tabulated.
 *
 * The chart is labelled as an image and the same numbers appear underneath as
 * text, because a chart alone is unreadable to a screen reader and unusable in
 * a copy-paste into an incident channel.
 */

const TONE_FILL: Record<Tone, string> = {
  default: 'var(--accent)',
  critical: 'var(--status-critical)',
  warning: 'var(--status-warning)',
  success: 'var(--status-success)',
  muted: 'var(--status-neutral)',
};

const TONE_TEXT: Record<Tone, string> = {
  default: 'text-ink-primary',
  critical: 'text-status-critical',
  warning: 'text-status-warning',
  success: 'text-status-success',
  muted: 'text-ink-tertiary',
};

export interface CountDatum {
  key: string;
  label: string;
  value: number;
  tone: Tone;
}

export function CountBars({
  data,
  summary,
  emptyTitle,
  emptyDetail,
  valueHeader = 'Count',
}: {
  data: CountDatum[];
  summary: string;
  emptyTitle: string;
  emptyDetail: string;
  valueHeader?: string;
}) {
  const total = data.reduce((sum, datum) => sum + datum.value, 0);
  if (data.length === 0 || total === 0) {
    return <EmptyState title={emptyTitle} detail={emptyDetail} />;
  }

  return (
    <div className="space-y-3">
      <div role="img" aria-label={summary}>
        <ResponsiveContainer width="100%" height={Math.max(130, data.length * 32)}>
          <BarChart data={data} layout="vertical" margin={{ top: 4, right: 16, bottom: 4, left: 4 }}>
            <CartesianGrid horizontal={false} stroke="var(--border-subtle)" />
            <XAxis
              type="number"
              allowDecimals={false}
              stroke="var(--text-tertiary)"
              tick={{ fontSize: 10, fill: 'var(--text-tertiary)' }}
            />
            <YAxis
              type="category"
              dataKey="label"
              width={132}
              stroke="var(--text-tertiary)"
              tick={{ fontSize: 10, fill: 'var(--text-secondary)' }}
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
            <Bar dataKey="value" radius={[0, 3, 3, 0]} isAnimationActive={false}>
              {data.map((datum) => (
                <Cell key={datum.key} fill={TONE_FILL[datum.tone]} />
              ))}
            </Bar>
          </BarChart>
        </ResponsiveContainer>
      </div>

      <table className="w-full border-collapse text-body">
        <caption className="sr-only">{summary}</caption>
        <thead>
          <tr className="border-b border-hairline text-left">
            <th scope="col" className="label-meta px-2 py-1.5 font-semibold">
              Outcome
            </th>
            <th scope="col" className="label-meta px-2 py-1.5 text-right font-semibold">
              {valueHeader}
            </th>
            <th scope="col" className="label-meta px-2 py-1.5 text-right font-semibold">
              Share
            </th>
          </tr>
        </thead>
        <tbody>
          {data.map((datum) => (
            <tr key={datum.key} className="border-b border-hairline last:border-0">
              <th scope="row" className={`px-2 py-1.5 text-left font-semibold ${TONE_TEXT[datum.tone]}`}>
                {datum.label}
              </th>
              <td className="tnum px-2 py-1.5 text-right font-semibold text-ink-primary">
                {datum.value}
              </td>
              <td className="tnum px-2 py-1.5 text-right font-medium text-ink-tertiary">
                {Math.round((datum.value / total) * 100)}%
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
