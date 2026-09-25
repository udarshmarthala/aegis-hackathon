'use client';

import { cn } from '@/lib/utils';
import type { ServiceRow } from '@/lib/console-types';

/**
 * Health treatment for live systems.
 *
 * `unknown` is a real health value, not a missing one. The runtime adapter
 * reports it when it can see the service but cannot judge it, which is a
 * different operational fact from "healthy" - so it gets its own chip rather
 * than being folded into the quiet default.
 */
export type Health = ServiceRow['health'];

const HEALTH_TONE: Record<Health, { dot: string; text: string; label: string }> = {
  critical: { dot: 'bg-status-critical', text: 'text-status-critical', label: 'Critical' },
  degraded: { dot: 'bg-status-warning', text: 'text-status-warning', label: 'Degraded' },
  unknown: { dot: 'bg-status-neutral', text: 'text-ink-secondary', label: 'Unknown' },
  healthy: { dot: 'bg-status-success', text: 'text-status-success', label: 'Healthy' },
};

export const HEALTH_ORDER: Health[] = ['critical', 'degraded', 'unknown', 'healthy'];

/** Sort key: the things that need attention sort first, by default, always. */
export function healthRank(health: string): number {
  const index = HEALTH_ORDER.indexOf(health as Health);
  return index === -1 ? HEALTH_ORDER.length : index;
}

function toneFor(health: string) {
  return HEALTH_TONE[health as Health] ?? {
    dot: 'bg-status-neutral',
    text: 'text-ink-secondary',
    label: health,
  };
}

export function HealthChip({ health, className }: { health: string; className?: string }) {
  const tone = toneFor(health);
  return (
    <span
      className={cn('inline-flex items-center gap-1.5 text-body font-semibold', tone.text, className)}
    >
      <span className={cn('h-1.5 w-1.5 rounded-full', tone.dot)} aria-hidden />
      {tone.label}
    </span>
  );
}

/** Instance-level health uses the same vocabulary in a denser form. */
export function HealthDot({ health }: { health: string }) {
  const tone = toneFor(health);
  return (
    <span className={cn('inline-flex items-center gap-1.5 text-meta font-semibold', tone.text)}>
      <span className={cn('h-1.5 w-1.5 rounded-full', tone.dot)} aria-hidden />
      {tone.label}
    </span>
  );
}

/**
 * Readiness. Rendered as a ratio because "2/3" and "2" answer different
 * questions, and the missing instance is the interesting one.
 */
export function Readiness({ ready, desired }: { ready: number; desired: number }) {
  const short = desired > 0 && ready < desired;
  return (
    <span
      className={cn(
        'tnum text-body font-semibold',
        short ? 'text-status-warning' : 'text-ink-primary',
      )}
    >
      {ready}
      <span className="text-ink-tertiary">/</span>
      {desired}
    </span>
  );
}
