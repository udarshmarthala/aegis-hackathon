'use client';

import {
  CartesianGrid,
  Legend,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts';
import { EmptyState } from '@/components/ui/states';
import { num, pct } from '@/lib/utils';
import type { CalibrationView } from '@/lib/console-types';

/**
 * Reliability diagram.
 *
 * Confidence is a claim, and this is the page where the claim is checked: a
 * point below the diagonal is over-confidence, which for an autonomous SRE is
 * the dangerous direction. Bins with no scored sample break the line rather
 * than interpolating across a hole in the data.
 */
export function CalibrationChart({ calibration }: { calibration: CalibrationView }) {
  const bins = calibration.bins ?? [];
  const points = bins.map((bin) => {
    const midpoint = (bin.lower + bin.upper) / 2;
    return {
      range: `${num(bin.lower, 2)}–${num(bin.upper, 2)}`,
      confidence: bin.mean_confidence ?? midpoint,
      accuracy: bin.accuracy,
      ideal: midpoint,
      count: bin.count,
    };
  });

  const summary = points
    .map((point) =>
      point.accuracy === null
        ? `${point.range}: no scored sample`
        : `${point.range}: ${point.count} samples, accuracy ${pct(point.accuracy, 0)}`,
    )
    .join('. ');

  return (
    <div className="space-y-4">
      <div className="grid gap-3 sm:grid-cols-2">
        <div className="card p-3.5">
          <p className="label-meta font-semibold">Brier score</p>
          <p
            className={
              calibration.brier_score === null
                ? 'mt-1 text-body font-semibold text-ink-tertiary'
                : 'tnum mt-1 text-h2 font-semibold text-ink-primary'
            }
          >
            {calibration.brier_score === null ? 'Not measured' : num(calibration.brier_score, 4)}
          </p>
          <p className="mt-1 text-meta font-medium text-ink-tertiary">
            Mean squared error of the confidence claims. Lower is better.
          </p>
        </div>
        <div className="card p-3.5">
          <p className="label-meta font-semibold">Expected calibration error</p>
          <p
            className={
              calibration.expected_calibration_error === null
                ? 'mt-1 text-body font-semibold text-ink-tertiary'
                : 'tnum mt-1 text-h2 font-semibold text-ink-primary'
            }
          >
            {calibration.expected_calibration_error === null
              ? 'Not measured'
              : num(calibration.expected_calibration_error, 4)}
          </p>
          <p className="mt-1 text-meta font-medium text-ink-tertiary">
            Average gap between stated confidence and observed accuracy.
          </p>
        </div>
      </div>

      {points.length === 0 ? (
        <EmptyState
          title="No calibration bin was produced for this run."
          detail="Bins appear once scenarios carry both a confidence claim and a scored outcome."
        />
      ) : (
        <>
          <div
            role="img"
            aria-label={`Reliability diagram. ${summary || 'No bin carries a scored sample.'}`}
          >
            <ResponsiveContainer width="100%" height={260}>
              <LineChart data={points} margin={{ top: 8, right: 16, bottom: 12, left: 0 }}>
                <CartesianGrid stroke="var(--border-subtle)" />
                <XAxis
                  dataKey="confidence"
                  type="number"
                  domain={[0, 1]}
                  tickFormatter={(value: number) => num(value, 1)}
                  stroke="var(--text-tertiary)"
                  tick={{ fontSize: 10, fill: 'var(--text-tertiary)' }}
                />
                <YAxis
                  domain={[0, 1]}
                  tickFormatter={(value: number) => num(value, 1)}
                  stroke="var(--text-tertiary)"
                  tick={{ fontSize: 10, fill: 'var(--text-tertiary)' }}
                />
                <Tooltip
                  contentStyle={{
                    background: 'var(--surface-2)',
                    border: '1px solid var(--border-default)',
                    borderRadius: 7,
                    fontSize: 11,
                    fontWeight: 600,
                    color: 'var(--text-primary)',
                  }}
                />
                <Legend wrapperStyle={{ fontSize: 11, fontWeight: 600 }} />
                <Line
                  type="linear"
                  dataKey="ideal"
                  name="Perfect calibration"
                  stroke="var(--status-neutral)"
                  strokeDasharray="4 3"
                  dot={false}
                  isAnimationActive={false}
                />
                <Line
                  type="linear"
                  dataKey="accuracy"
                  name="Observed accuracy"
                  stroke="var(--accent)"
                  strokeWidth={2}
                  connectNulls={false}
                  isAnimationActive={false}
                />
              </LineChart>
            </ResponsiveContainer>
          </div>
          <p className="text-meta font-medium text-ink-tertiary">
            Stated confidence runs along the horizontal axis, observed accuracy up the vertical one.
            A point below the dashed line is over-confidence: Aegis claimed more certainty than it
            earned.
          </p>

          <table className="w-full border-collapse text-body">
            <caption className="sr-only">Calibration bins</caption>
            <thead>
              <tr className="border-b border-hairline text-left">
                {['Confidence band', 'Samples', 'Mean confidence', 'Observed accuracy', 'Gap'].map(
                  (header) => (
                    <th key={header} scope="col" className="label-meta px-3 py-2 font-semibold">
                      {header}
                    </th>
                  ),
                )}
              </tr>
            </thead>
            <tbody>
              {bins.map((bin) => {
                const gap =
                  bin.accuracy !== null && bin.mean_confidence !== null
                    ? bin.mean_confidence - bin.accuracy
                    : null;
                return (
                  <tr
                    key={`${bin.lower}-${bin.upper}`}
                    className="border-b border-hairline last:border-0"
                  >
                    <th scope="row" className="tnum px-3 py-2 text-left font-semibold text-ink-primary">
                      {num(bin.lower, 2)} – {num(bin.upper, 2)}
                    </th>
                    <td className="tnum px-3 py-2 font-medium text-ink-secondary">{bin.count}</td>
                    <td className="tnum px-3 py-2 font-medium text-ink-secondary">
                      {bin.mean_confidence === null ? 'not measured' : num(bin.mean_confidence, 3)}
                    </td>
                    <td className="tnum px-3 py-2 font-medium text-ink-secondary">
                      {bin.accuracy === null ? 'no scored sample' : num(bin.accuracy, 3)}
                    </td>
                    <td
                      className={
                        gap !== null && gap > 0.1
                          ? 'tnum px-3 py-2 font-semibold text-status-critical'
                          : 'tnum px-3 py-2 font-medium text-ink-secondary'
                      }
                    >
                      {gap === null ? '—' : `${gap > 0 ? '+' : ''}${num(gap, 3)}`}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </>
      )}
    </div>
  );
}
