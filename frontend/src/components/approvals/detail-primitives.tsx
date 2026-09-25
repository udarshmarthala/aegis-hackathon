'use client';

import { useEffect, useState, type ReactNode } from 'react';
import { cn, formatDuration } from '@/lib/utils';
import type { Severity } from '@/lib/types';

/**
 * The small, shared vocabulary of the decision surfaces.
 *
 * Approvals, deployments and the debug workbench all render the same kinds of
 * thing: a structured plan the backend stored as JSON, a monospace excerpt, a
 * risk tier, an expiry. They share one implementation so a rollback plan looks
 * identical wherever an operator meets it - recognising a shape at 2 a.m. is
 * faster than reading it (UX spec 85).
 */

const SEVERITIES = new Set<string>(['P1', 'P2', 'P3', 'P4']);

/** Wire severity is a plain string; only render a badge for one we know. */
export function asSeverity(value: string | null | undefined): Severity | null {
  return value && SEVERITIES.has(value) ? (value as Severity) : null;
}

/* ------------------------------------------------------------- risk tier -- */

const TIER_TONE: Record<number, string> = {
  1: 'border-status-success/40 bg-status-success/10 text-status-success',
  2: 'border-status-warning/40 bg-status-warning/10 text-status-warning',
  3: 'border-status-critical/40 bg-status-critical/10 text-status-critical',
  4: 'border-status-critical/60 bg-status-critical/15 text-status-critical',
};

const TIER_MEANING: Record<number, string> = {
  1: 'Reversible, contained',
  2: 'Reversible, service-wide',
  3: 'Hard to reverse or broad',
  4: 'Irreversible or production-wide',
};

/**
 * Risk tier is stated, not implied. The tier is a deterministic policy output,
 * so it is shown as a fact with its meaning attached rather than as a colour an
 * operator has to have memorised.
 */
export function RiskTierBadge({ tier, size = 'md' }: { tier: number; size?: 'sm' | 'md' }) {
  const tone = TIER_TONE[tier] ?? 'border-line bg-surface-3 text-ink-secondary';
  const meaning = TIER_MEANING[tier] ?? 'Unclassified action type';
  return (
    <span
      className={cn(
        'inline-flex items-center gap-1.5 rounded border font-bold tabular-nums',
        tone,
        size === 'md' ? 'px-2 py-1 text-body' : 'px-1.5 py-0.5 text-meta',
      )}
      title={`Risk tier ${tier} - ${meaning}`}
    >
      <span className="uppercase tracking-wider">Tier {tier}</span>
      {size === 'md' ? (
        <span className="font-medium tracking-normal opacity-80">{meaning}</span>
      ) : null}
    </span>
  );
}

/* ----------------------------------------------------------------- field -- */

export function Field({
  label,
  value,
  className,
}: {
  label: string;
  value: ReactNode;
  className?: string;
}) {
  return (
    <div className={cn('min-w-0', className)}>
      <dt className="label-meta font-semibold">{label}</dt>
      <dd className="mt-1 break-words text-body font-medium text-ink-primary">{value}</dd>
    </div>
  );
}

export function MonoBlock({
  text,
  label,
  tone = 'default',
  maxHeight = 'max-h-64',
}: {
  text: string;
  label: string;
  tone?: 'default' | 'critical';
  maxHeight?: string;
}) {
  return (
    <figure className="min-w-0">
      <figcaption className="label-meta font-semibold">{label}</figcaption>
      <pre
        className={cn(
          'mt-1 overflow-auto rounded-btn border border-hairline bg-surface-2 p-2.5',
          'whitespace-pre-wrap break-words font-mono text-meta font-medium leading-relaxed',
          tone === 'critical' ? 'text-status-critical' : 'text-ink-secondary',
          maxHeight,
        )}
        tabIndex={0}
      >
        {text}
      </pre>
    </figure>
  );
}

/* ------------------------------------------------------------ structured -- */

export function formatValue(value: unknown): string {
  if (value === null || value === undefined) return '—';
  if (typeof value === 'string') return value.length ? value : '—';
  if (typeof value === 'number' || typeof value === 'boolean') return String(value);
  if (Array.isArray(value)) {
    if (value.length === 0) return 'none';
    if (value.every((v) => typeof v === 'string' || typeof v === 'number')) {
      return value.join(', ');
    }
  }
  return JSON.stringify(value, null, 2);
}

function humanKey(key: string): string {
  return key.replace(/[_.]/g, ' ');
}

/**
 * A stored plan, rendered as readable fields rather than a JSON dump.
 *
 * `emptyLabel` is required at every call site because an absent plan is the
 * thing an operator most needs to notice: "no rollback plan" must never look
 * like a section that simply rendered nothing.
 */
export function StructuredDetail({
  data,
  emptyLabel,
  emptyTone = 'muted',
}: {
  data: Record<string, unknown> | null | undefined;
  emptyLabel: string;
  emptyTone?: 'muted' | 'warning' | 'critical';
}) {
  const entries = data ? Object.entries(data) : [];
  if (entries.length === 0) {
    return (
      <p
        className={cn(
          'text-body font-semibold',
          emptyTone === 'critical'
            ? 'text-status-critical'
            : emptyTone === 'warning'
              ? 'text-status-warning'
              : 'text-ink-tertiary',
        )}
      >
        {emptyLabel}
      </p>
    );
  }
  return (
    <dl className="grid gap-2.5 sm:grid-cols-2">
      {entries.map(([key, value]) => {
        const rendered = formatValue(value);
        const multiline = rendered.includes('\n');
        return (
          <div key={key} className={cn('min-w-0', multiline && 'sm:col-span-2')}>
            <dt className="label-meta font-semibold">{humanKey(key)}</dt>
            <dd
              className={cn(
                'mt-0.5 break-words text-body font-medium text-ink-primary',
                multiline && 'whitespace-pre-wrap font-mono text-meta text-ink-secondary',
              )}
            >
              {rendered}
            </dd>
          </div>
        );
      })}
    </dl>
  );
}

/* ---------------------------------------------------------------- expiry -- */

export type ExpiryTone = 'ok' | 'soon' | 'urgent' | 'lapsed';

export interface Expiry {
  ms: number;
  tone: ExpiryTone;
  label: string;
}

/**
 * Time left on an approval.
 *
 * A lapsed approval silently undoes a decision someone already made, so the
 * thresholds are deliberately early: the operator should see the warning while
 * there is still time to act on it.
 */
export function expiryOf(expiresAt: string, now: number): Expiry {
  const target = new Date(expiresAt).getTime();
  if (Number.isNaN(target)) {
    return { ms: 0, tone: 'lapsed', label: 'expiry unknown' };
  }
  const ms = target - now;
  if (ms <= 0) return { ms, tone: 'lapsed', label: `lapsed ${formatDuration(-ms)} ago` };
  if (ms < 5 * 60_000) return { ms, tone: 'urgent', label: `${formatDuration(ms)} left` };
  if (ms < 15 * 60_000) return { ms, tone: 'soon', label: `${formatDuration(ms)} left` };
  return { ms, tone: 'ok', label: `${formatDuration(ms)} left` };
}

export const EXPIRY_TEXT: Record<ExpiryTone, string> = {
  ok: 'text-ink-secondary',
  soon: 'text-status-warning',
  urgent: 'text-status-critical',
  lapsed: 'text-status-critical',
};

export function ExpiryChip({ expiry }: { expiry: Expiry }) {
  const emphatic = expiry.tone === 'urgent' || expiry.tone === 'lapsed';
  return (
    <span
      className={cn(
        'inline-flex items-center gap-1.5 rounded border px-2 py-0.5',
        'text-meta font-bold uppercase tracking-wider',
        EXPIRY_TEXT[expiry.tone],
        expiry.tone === 'ok'
          ? 'border-hairline'
          : expiry.tone === 'soon'
            ? 'border-status-warning/40 bg-status-warning/10'
            : 'border-status-critical/50 bg-status-critical/10',
      )}
    >
      {emphatic ? <span className="h-1.5 w-1.5 rounded-full bg-current" aria-hidden /> : null}
      {expiry.label}
    </span>
  );
}

/** One shared clock for every countdown on a page. */
export const EXPIRY_TICK_MS = 15_000;

/**
 * A coarse, shared clock.
 *
 * Countdowns re-render on this tick rather than each running its own timer, and
 * the first value is computed during mount rather than at module load so the
 * server and client agree on the initial render.
 */
export function useNow(intervalMs: number = EXPIRY_TICK_MS): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now()), intervalMs);
    return () => clearInterval(timer);
  }, [intervalMs]);
  return now;
}
