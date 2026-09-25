'use client';

import {
  Activity, Archive, Box, Brain, Cloud, Cpu, Database, FileCode2, Globe, Image as ImageIcon,
  ListChecks, Sigma, Sparkles, TreePine, Wrench, type LucideIcon,
} from 'lucide-react';
import { cn } from '@/lib/utils';
import type { KnownSource, Source } from '@/lib/war-room/types';

/**
 * Small pieces every war-room panel shares.
 *
 * `SourceLabel` is the honesty rule made concrete: every card, event and panel
 * says which path actually produced it. The label is text, never an icon alone,
 * so a fallback cannot be mistaken for the real thing by anyone who does not
 * know the icon set - and the fallback paths carry a distinct outline on top.
 */

const SOURCE_META: Record<KnownSource, { icon: LucideIcon; text: string; meaning: string }> = {
  bedrock: { icon: Cloud, text: 'Bedrock', meaning: 'Claude on Amazon Bedrock' },
  gemini: { icon: Sparkles, text: 'Gemini', meaning: 'Gemini Flash' },
  scripted: { icon: FileCode2, text: 'Scripted', meaning: 'Deterministic scripted policy (fallback)' },
  rule: { icon: ListChecks, text: 'Rule', meaning: 'Deterministic rule-based compaction' },
  rawtree: { icon: TreePine, text: 'RawTree', meaning: 'RawTree' },
  postgres: { icon: Database, text: 'Postgres', meaning: 'Postgres (system of record)' },
  prometheus: { icon: Activity, text: 'Prometheus', meaning: 'Prometheus metrics' },
  runtime: { icon: Box, text: 'Runtime', meaning: 'Container runtime adapter' },
  nimble: { icon: Globe, text: 'Nimble', meaning: 'Nimble live web search' },
  fixture: { icon: Archive, text: 'Fixture', meaning: 'Captured fixture (fallback)' },
  flux: { icon: ImageIcon, text: 'FLUX', meaning: 'Black Forest Labs FLUX' },
  zscore: { icon: Sigma, text: 'z-score', meaning: 'In-process z-score (fallback)' },
  memory: { icon: Brain, text: 'Memory', meaning: 'Incident memory' },
  tool: { icon: Wrench, text: 'Tool', meaning: 'Tool output' },
  system: { icon: Cpu, text: 'System', meaning: 'Orchestrator' },
};

/** Paths that exist because the preferred one was unavailable. */
export const FALLBACK_SOURCES: ReadonlySet<string> = new Set(['scripted', 'fixture', 'zscore']);

export function sourceText(source: Source | null | undefined): string {
  if (!source) return 'unknown';
  return (SOURCE_META as Record<string, { text: string }>)[source]?.text ?? source;
}

export function SourceLabel({
  source,
  className,
  size = 'sm',
}: {
  source: Source | null | undefined;
  className?: string;
  size?: 'sm' | 'md';
}) {
  const key = source ?? 'unknown';
  const meta = (SOURCE_META as Record<string, (typeof SOURCE_META)[KnownSource]>)[key];
  const Icon = meta?.icon ?? Cpu;
  const fallback = FALLBACK_SOURCES.has(key);
  return (
    <span
      data-testid="source-label"
      data-source={key}
      title={`Source: ${meta?.meaning ?? key}`}
      className={cn(
        'inline-flex shrink-0 items-center gap-1 rounded border font-semibold',
        size === 'md' ? 'px-2 py-0.5 text-[0.85rem]' : 'px-1.5 py-px text-[0.72rem]',
        fallback
          ? 'border-status-warning/50 bg-status-warning/10 text-status-warning'
          : 'border-line bg-surface-3 text-ink-secondary',
        className,
      )}
    >
      <Icon className={size === 'md' ? 'h-3.5 w-3.5' : 'h-3 w-3'} aria-hidden />
      <span className="sr-only">source </span>
      {meta?.text ?? key}
    </span>
  );
}

/**
 * A minimal sparkline. SVG rather than a chart library because six of these
 * update every five seconds and none of them needs axes or tooltips.
 */
export function Sparkline({
  values,
  className,
  tone = 'var(--text-secondary)',
  max,
  label,
}: {
  values: readonly number[];
  className?: string;
  tone?: string;
  /** Fixed upper bound (e.g. 1 for ratios); otherwise scaled to the data. */
  max?: number;
  label: string;
}) {
  const clean = values.filter((v) => Number.isFinite(v));
  if (clean.length < 2) {
    return (
      <svg className={className} viewBox="0 0 100 24" role="img" aria-label={`${label}: not enough samples`}>
        <line x1="0" y1="23" x2="100" y2="23" stroke="var(--border-strong)" strokeDasharray="3 3" />
      </svg>
    );
  }
  const hi = max ?? Math.max(...clean);
  const lo = max !== undefined ? 0 : Math.min(...clean);
  const span = hi - lo || 1;
  const step = 100 / (clean.length - 1);
  const points = clean
    .map((v, i) => `${(i * step).toFixed(2)},${(22 - ((v - lo) / span) * 20 + 1).toFixed(2)}`)
    .join(' ');
  return (
    <svg
      className={className}
      viewBox="0 0 100 24"
      preserveAspectRatio="none"
      role="img"
      aria-label={`${label}: ${clean.length} samples, latest ${clean[clean.length - 1]?.toFixed(3)}`}
    >
      <polyline
        points={points}
        fill="none"
        stroke={tone}
        strokeWidth="1.6"
        vectorEffect="non-scaling-stroke"
        strokeLinejoin="round"
      />
    </svg>
  );
}

export function Panel({
  title,
  source,
  right,
  children,
  className,
  bodyClassName,
  labelledBy,
}: {
  title: string;
  source?: Source | string | null;
  right?: React.ReactNode;
  children: React.ReactNode;
  className?: string;
  bodyClassName?: string;
  labelledBy?: string;
}) {
  const id = labelledBy ?? `wr-${title.toLowerCase().replace(/[^a-z0-9]+/g, '-')}`;
  return (
    <section
      aria-labelledby={id}
      className={cn('card flex min-h-0 min-w-0 flex-col bg-surface-1', className)}
    >
      <header className="flex items-center gap-2 border-b border-hairline px-3 py-2">
        <h2 id={id} className="text-[0.8rem] font-bold uppercase tracking-wider text-ink-secondary">
          {title}
        </h2>
        {source ? <SourceLabel source={source} /> : null}
        <div className="ml-auto flex items-center gap-2">{right}</div>
      </header>
      <div className={cn('min-h-0 flex-1 overflow-auto p-3', bodyClassName)}>{children}</div>
    </section>
  );
}

export function Kbd({ children }: { children: React.ReactNode }) {
  return (
    <kbd
      className="rounded border border-edge bg-surface-3 px-1.5 py-px font-mono text-[0.72rem]
                 font-bold text-ink-secondary"
    >
      {children}
    </kbd>
  );
}

export function formatTokens(n: number | null | undefined): string {
  if (n === null || n === undefined || !Number.isFinite(n)) return '—';
  if (n >= 10_000) return `${(n / 1000).toFixed(0)}k`;
  if (n >= 1000) return `${(n / 1000).toFixed(1)}k`;
  return String(Math.round(n));
}
