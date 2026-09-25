'use client';

import { cn } from '@/lib/utils';

/**
 * `QueryFailure` now lives with the primitives it composes, where it also
 * learned to treat an unreachable backend as an unavailable source. It is
 * re-exported here because the reliability surfaces import it by this path;
 * new call sites should import it from `@/components/ui/states` directly.
 */
export { QueryFailure } from '@/components/ui/states';

/**
 * Shared furniture for the reliability surfaces.
 *
 * `StatTile` insists on a string value rather than a number so a caller has to
 * decide what an absent measurement says. "Not enough data" and "0" are
 * different claims, and only one of them is honest when nothing resolved.
 */

export type Tone = 'default' | 'critical' | 'warning' | 'success' | 'muted';

const TONE_CLASS: Record<Tone, string> = {
  default: 'text-ink-primary',
  critical: 'text-status-critical',
  warning: 'text-status-warning',
  success: 'text-status-success',
  muted: 'text-ink-tertiary',
};

export function StatTile({
  label,
  value,
  detail,
  tone = 'default',
}: {
  label: string;
  value: string;
  detail?: string;
  tone?: Tone;
}) {
  return (
    <div className="card p-3.5">
      <p className="label-meta font-semibold">{label}</p>
      <p className={cn('tnum mt-1 text-h2 font-semibold tracking-tight', TONE_CLASS[tone])}>
        {value}
      </p>
      {detail ? (
        <p className="mt-1 text-meta font-medium text-ink-tertiary">{detail}</p>
      ) : null}
    </div>
  );
}

export function WindowSelector<T extends number>({
  value,
  options,
  onChange,
  label = 'Window',
}: {
  value: T;
  options: readonly T[];
  onChange: (next: T) => void;
  label?: string;
}) {
  return (
    <div className="flex items-center gap-1" role="group" aria-label={`${label} in days`}>
      <span className="mr-1 text-meta font-semibold uppercase tracking-wider text-ink-tertiary">
        {label}
      </span>
      {options.map((option) => (
        <button
          key={option}
          type="button"
          onClick={() => onChange(option)}
          aria-pressed={value === option}
          className={cn(
            'tnum rounded-btn border px-2 py-1 text-meta font-semibold transition-colors duration-hover',
            value === option
              ? 'border-edge bg-surface-3 text-ink-primary'
              : 'border-hairline text-ink-tertiary hover:bg-surface-2',
          )}
        >
          {option}d
        </button>
      ))}
    </div>
  );
}

export function SectionCard({
  title,
  description,
  action,
  children,
}: {
  title: string;
  description?: string;
  action?: React.ReactNode;
  children: React.ReactNode;
}) {
  const headingId = `section-${title.toLowerCase().replace(/[^a-z0-9]+/g, '-')}`;
  return (
    <section aria-labelledby={headingId} className="card p-4">
      <div className="mb-3 flex flex-wrap items-start justify-between gap-3">
        <div>
          <h2 id={headingId} className="text-body font-semibold text-ink-primary">
            {title}
          </h2>
          {description ? (
            <p className="mt-0.5 max-w-2xl text-meta font-medium text-ink-secondary">
              {description}
            </p>
          ) : null}
        </div>
        {action}
      </div>
      {children}
    </section>
  );
}
