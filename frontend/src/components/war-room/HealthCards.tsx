'use client';

import { AlertOctagon, AlertTriangle, CheckCircle2, HelpCircle, type LucideIcon } from 'lucide-react';
import { cn } from '@/lib/utils';
import type { HealthPayload, HealthService, HealthStatus } from '@/lib/war-room/types';
import { SourceLabel, Sparkline } from './primitives';

/**
 * Row 1: the four things the audience watches go red and come back.
 *
 * An unreachable metrics source renders the word "unavailable", never a zero.
 * A zero error rate is a claim that nothing is failing; showing one because
 * Prometheus could not be asked would be the page lying at the exact moment it
 * matters (invariant 6).
 */

export const HEALTH_SERVICES = ['gateway', 'checkout', 'payment', 'db-pool'] as const;

/** The verification thresholds (sustained-sample check), shown so a breach is legible. */
export const THRESHOLDS = { p99_ms: 250, error_rate: 0.02, pool_utilisation: 0.8 } as const;

const STATUS_META: Record<HealthStatus, { icon: LucideIcon; text: string; tone: string; ring: string }> = {
  healthy: {
    icon: CheckCircle2, text: 'Healthy', tone: 'text-status-success', ring: 'border-status-success/40',
  },
  degraded: {
    icon: AlertTriangle, text: 'Degraded', tone: 'text-status-warning', ring: 'border-status-warning/60',
  },
  critical: {
    icon: AlertOctagon, text: 'Critical', tone: 'text-status-critical', ring: 'border-status-critical/70',
  },
  unknown: {
    icon: HelpCircle, text: 'Unknown', tone: 'text-ink-tertiary', ring: 'border-line',
  },
};

function statusOf(svc: HealthService | undefined): HealthStatus {
  if (!svc || svc.source === 'unavailable') return 'unknown';
  return svc.status in STATUS_META ? svc.status : 'unknown';
}

type MetricKey = 'p99_ms' | 'error_rate' | 'pool_utilisation';

function render(key: MetricKey, value: number): string {
  if (key === 'p99_ms') return `${Math.round(value)} ms`;
  if (key === 'error_rate') return `${(value * 100).toFixed(value < 0.1 ? 1 : 0)}%`;
  return `${Math.round(value * 100)}%`;
}

function Metric({
  label,
  metric,
  svc,
}: {
  label: string;
  metric: MetricKey;
  svc: HealthService | undefined;
}) {
  const unavailable = !svc || svc.source === 'unavailable';
  const value = svc?.[metric] ?? null;
  const breach = value !== null && !unavailable && value >= THRESHOLDS[metric];
  let text: string;
  if (unavailable) text = 'unavailable';
  else if (value === null) text = 'no data';
  else text = render(metric, value);
  const series = unavailable ? [] : (svc?.series?.[metric] ?? []);

  return (
    <div className="min-w-0">
      <dt className="text-[0.72rem] font-semibold uppercase tracking-wider text-ink-tertiary">
        {label}
      </dt>
      <dd
        data-testid={`metric-${metric}`}
        className={cn(
          'tnum text-[1.35rem] font-bold leading-tight',
          unavailable || value === null ? 'text-[1rem] italic text-ink-tertiary' : 'text-ink-primary',
          breach && 'text-status-critical',
        )}
      >
        {text}
        {breach ? <span className="sr-only"> (over the {render(metric, THRESHOLDS[metric])} threshold)</span> : null}
      </dd>
      <Sparkline
        values={series}
        className="mt-1 h-6 w-full"
        max={metric === 'p99_ms' ? undefined : 1}
        tone={breach ? 'var(--status-critical)' : 'var(--text-secondary)'}
        label={`${label} trend`}
      />
    </div>
  );
}

export function HealthCard({ name, svc }: { name: string; svc: HealthService | undefined }) {
  const status = statusOf(svc);
  const meta = STATUS_META[status];
  const Icon = meta.icon;
  const unavailable = !svc || svc.source === 'unavailable';
  return (
    <article
      aria-label={`${name} health`}
      data-testid={`health-${name}`}
      className={cn('card flex min-w-0 flex-col gap-2 border-2 bg-surface-1 px-4 py-3', meta.ring)}
    >
      <header className="flex items-center gap-2">
        <h3 className="font-mono text-[1.15rem] font-bold text-ink-primary">{name}</h3>
        {svc?.version ? (
          <span className="rounded border border-line px-1.5 font-mono text-[0.78rem] font-semibold text-ink-secondary">
            v{svc.version}
          </span>
        ) : null}
        <span className={cn('ml-auto inline-flex items-center gap-1 text-[0.85rem] font-bold', meta.tone)}>
          <Icon className="h-4 w-4" aria-hidden />
          {unavailable ? 'Unavailable' : meta.text}
        </span>
      </header>
      <dl className="grid grid-cols-3 gap-3">
        <Metric label="p99" metric="p99_ms" svc={svc} />
        <Metric label="errors" metric="error_rate" svc={svc} />
        <Metric label="pool" metric="pool_utilisation" svc={svc} />
      </dl>
      <footer className="flex items-center gap-2">
        <SourceLabel source={svc?.source ?? 'unavailable'} />
        {unavailable ? (
          <span className="text-[0.75rem] font-medium text-ink-tertiary">
            metrics could not be read - not the same as zero
          </span>
        ) : null}
      </footer>
    </article>
  );
}

export function HealthCards({ health }: { health: HealthPayload | null }) {
  const byName = new Map((health?.services ?? []).map((s) => [s.service, s]));
  return (
    <div className="grid grid-cols-2 gap-3 xl:grid-cols-4" role="group" aria-label="Service health">
      {HEALTH_SERVICES.map((name) => (
        <HealthCard key={name} name={name} svc={byName.get(name)} />
      ))}
    </div>
  );
}
