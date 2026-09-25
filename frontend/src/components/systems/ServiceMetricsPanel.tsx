'use client';

import { useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import {
  CartesianGrid, Line, LineChart, ResponsiveContainer, XAxis, YAxis,
} from 'recharts';
import { consoleApi } from '@/lib/console-api';
import type { MetricSignal, ServiceMetrics } from '@/lib/console-types';
import { EmptyState, ErrorState, Skeleton, SourceUnavailableState } from '@/components/ui/states';
import { cn, num, pct } from '@/lib/utils';

/**
 * Golden signals for one service (UX spec 75: a chart answers an operational
 * question or it does not ship).
 *
 * Each signal is fetched and rendered independently because the backend reports
 * them independently. An unavailable signal shows why it is unavailable; it is
 * never drawn as a flat line at zero, which would read as "no errors" when the
 * truth is "no telemetry".
 */

type SignalKey = 'error_rate' | 'latency_p99' | 'request_rate';

interface SignalSpec {
  key: SignalKey;
  label: string;
  question: string;
  stroke: string;
  format: (value: number) => string;
}

const SIGNALS: SignalSpec[] = [
  {
    key: 'error_rate',
    label: 'Error rate',
    question: 'Are requests failing, and did that change?',
    stroke: 'var(--status-critical)',
    format: (v) => pct(v, 2),
  },
  {
    key: 'latency_p99',
    label: 'Latency p99',
    question: 'Is the slow tail getting slower?',
    stroke: 'var(--status-warning)',
    format: (v) => `${num(v, 0)}ms`,
  },
  {
    key: 'request_rate',
    label: 'Request rate',
    question: 'Is this a failure, or an absence of traffic?',
    stroke: 'var(--accent)',
    format: (v) => `${num(v, 2)}/s`,
  },
];

const WINDOWS: Array<{ seconds: number; label: string }> = [
  { seconds: 3600, label: '1h' },
  { seconds: 21_600, label: '6h' },
  { seconds: 86_400, label: '24h' },
];

function clockOf(epochSeconds: number): string {
  const date = new Date(epochSeconds * 1000);
  if (Number.isNaN(date.getTime())) return '';
  return date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', hour12: false });
}

function signalOf(metrics: ServiceMetrics | undefined, key: SignalKey): MetricSignal | undefined {
  if (!metrics || typeof metrics.signals !== 'object' || metrics.signals === null) return undefined;
  return (metrics.signals as Partial<Record<SignalKey, MetricSignal>>)[key];
}

export function ServiceMetricsPanel({ serviceId }: { serviceId: string }) {
  const [windowSeconds, setWindowSeconds] = useState(3600);

  const query = useQuery({
    queryKey: ['systems', 'metrics', serviceId, windowSeconds],
    queryFn: () => consoleApi.serviceMetrics(serviceId, windowSeconds),
    refetchInterval: 30_000,
  });

  return (
    <section aria-labelledby="golden-signals" className="space-y-3">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h2 id="golden-signals" className="text-h3 font-semibold tracking-tight">
          Golden signals
        </h2>
        <div className="flex items-center gap-1.5" role="group" aria-label="Metric window">
          {WINDOWS.map((option) => (
            <button
              key={option.seconds}
              type="button"
              onClick={() => setWindowSeconds(option.seconds)}
              aria-pressed={windowSeconds === option.seconds}
              className={cn(
                'rounded-btn border px-2 py-1 text-meta font-semibold transition-colors duration-hover',
                windowSeconds === option.seconds
                  ? 'border-edge bg-surface-3 text-ink-primary'
                  : 'border-hairline text-ink-tertiary hover:bg-surface-2',
              )}
            >
              {option.label}
            </button>
          ))}
        </div>
      </div>

      {query.isError ? (
        <ErrorState
          title="Cannot load golden signals"
          detail={(query.error as Error).message}
          consequence="Telemetry state is unknown for this service; no signal can be read as healthy."
          onRetry={() => query.refetch()}
        />
      ) : (
        <div className="grid gap-3 xl:grid-cols-3">
          {SIGNALS.map((spec) => (
            <SignalCard
              key={spec.key}
              spec={spec}
              signal={signalOf(query.data, spec.key)}
              loading={query.isLoading}
              onRetry={() => query.refetch()}
            />
          ))}
        </div>
      )}
    </section>
  );
}

function SignalCard({
  spec,
  signal,
  loading,
  onRetry,
}: {
  spec: SignalSpec;
  signal: MetricSignal | undefined;
  loading: boolean;
  onRetry: () => void;
}) {
  const points = signal?.points ?? [];
  const latest = signal?.latest;

  return (
    <article className="card p-4">
      <header className="mb-3">
        <h3 className="text-body font-semibold text-ink-primary">{spec.label}</h3>
        <p className="mt-0.5 text-meta font-medium text-ink-tertiary">{spec.question}</p>
      </header>

      {loading ? (
        <Skeleton className="h-[164px] w-full" />
      ) : !signal ? (
        <SourceUnavailableState
          source={spec.label}
          reason="The metrics endpoint did not report this signal."
          consequence="Treat this signal as unknown, not as zero."
          onRetry={onRetry}
        />
      ) : !signal.available ? (
        <SourceUnavailableState
          source={spec.label}
          reason={signal.reason ?? 'the metrics source could not be reached'}
          consequence="Aegis cannot read this signal, so it is unknown — not zero, and not healthy."
          onRetry={onRetry}
        />
      ) : points.length === 0 ? (
        <EmptyState
          title="No samples in this window."
          detail="The metrics source answered and returned no data points for this period."
          hint="widen the window, or confirm the service is emitting this metric"
        />
      ) : (
        <>
          <p className="tnum mb-2 text-h2 font-bold text-ink-primary">
            {typeof latest === 'number' ? spec.format(latest) : '—'}
            <span className="ml-2 text-meta font-semibold uppercase tracking-wider text-ink-tertiary">
              latest · {points.length} samples
            </span>
          </p>
          <div className="h-[140px] w-full">
            <ResponsiveContainer width="100%" height="100%">
              <LineChart data={points} margin={{ top: 4, right: 8, bottom: 0, left: 0 }}>
                <CartesianGrid stroke="var(--border-subtle)" vertical={false} />
                <XAxis
                  dataKey="t"
                  tickFormatter={clockOf}
                  stroke="var(--text-tertiary)"
                  tick={{ fontSize: 10, fill: 'var(--text-tertiary)' }}
                  tickLine={false}
                  axisLine={{ stroke: 'var(--border-subtle)' }}
                  minTickGap={28}
                />
                <YAxis
                  stroke="var(--text-tertiary)"
                  tick={{ fontSize: 10, fill: 'var(--text-tertiary)' }}
                  tickLine={false}
                  axisLine={false}
                  width={52}
                  tickFormatter={(value: number) => spec.format(value)}
                />
                <Line
                  type="monotone"
                  dataKey="v"
                  stroke={spec.stroke}
                  strokeWidth={1.75}
                  dot={false}
                  isAnimationActive={false}
                />
              </LineChart>
            </ResponsiveContainer>
          </div>
          <table className="mt-2 w-full text-meta">
            <caption className="sr-only">
              {spec.label} samples, first and last value in the selected window
            </caption>
            <tbody>
              <tr>
                <th scope="row" className="label-meta py-0.5 text-left font-semibold">
                  Window start
                </th>
                <td className="tnum py-0.5 text-right font-semibold text-ink-secondary">
                  {points[0] ? `${spec.format(points[0].v)} at ${clockOf(points[0].t)}` : '—'}
                </td>
              </tr>
              <tr>
                <th scope="row" className="label-meta py-0.5 text-left font-semibold">
                  Window end
                </th>
                <td className="tnum py-0.5 text-right font-semibold text-ink-secondary">
                  {(() => {
                    const last = points[points.length - 1];
                    return last ? `${spec.format(last.v)} at ${clockOf(last.t)}` : '—';
                  })()}
                </td>
              </tr>
            </tbody>
          </table>
        </>
      )}
    </article>
  );
}
