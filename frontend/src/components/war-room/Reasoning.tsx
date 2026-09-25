'use client';

import { motion, useReducedMotion, AnimatePresence } from 'framer-motion';
import { CheckCircle2, Circle, CircleDot, ExternalLink, MinusCircle, XCircle } from 'lucide-react';
import { cn } from '@/lib/utils';
import type { EvidenceView } from '@/lib/war-room/reducer';
import type { Goal, HorizonHypothesis } from '@/lib/war-room/types';
import { SourceLabel, Sparkline } from './primitives';

/**
 * The agent's working memory, as it edits it: hypotheses (row 2), the goal
 * tree and the in-context evidence cards (row 3).
 *
 * Confidence here is the orchestrator's derived value from cited evidence, not
 * whatever the model said - the label says so, because a bar without that
 * caveat reads as the model's own certainty.
 */

export function HypothesisBars({ hypotheses }: { hypotheses: readonly HorizonHypothesis[] }) {
  if (hypotheses.length === 0) {
    return <p className="text-[0.95rem] text-ink-tertiary">No hypotheses yet.</p>;
  }
  const sorted = [...hypotheses].sort((a, b) => b.confidence - a.confidence);
  return (
    <ul className="space-y-2.5" aria-label="Hypotheses by derived confidence">
      {sorted.map((h, i) => {
        const pct = Math.round(h.confidence * 100);
        return (
          <li key={h.id} className="min-w-0">
            <div className="flex items-baseline gap-2">
              <span className="font-mono text-[0.75rem] font-bold text-ink-tertiary">{h.id}</span>
              <span className="min-w-0 flex-1 truncate text-[0.95rem] font-semibold text-ink-primary" title={h.statement}>
                {h.statement}
              </span>
              {i === 0 ? (
                <span className="rounded border border-accent/50 px-1 text-[0.66rem] font-bold uppercase text-accent">
                  leading
                </span>
              ) : null}
              <span className="tnum w-12 text-right text-[1.05rem] font-bold">{pct}%</span>
            </div>
            <div className="mt-1 flex items-center gap-2">
              <div
                role="meter"
                aria-label={`${h.statement} derived confidence`}
                aria-valuemin={0}
                aria-valuemax={100}
                aria-valuenow={pct}
                className="h-2.5 flex-1 overflow-hidden rounded-full bg-surface-3"
              >
                <div
                  className={cn(
                    'h-full rounded-full transition-[width] duration-500',
                    i === 0 ? 'bg-accent' : 'bg-ink-tertiary',
                  )}
                  style={{ width: `${pct}%` }}
                />
              </div>
              <Sparkline values={h.history} max={1} className="h-5 w-20 shrink-0" label="confidence history" />
            </div>
            <p className="mt-0.5 truncate font-mono text-[0.72rem] text-ink-tertiary">
              <span className="text-status-success">+{h.supporting.join(' ') || '—'}</span>
              {'  '}
              <span className="text-status-critical">-{h.refuting.join(' ') || '—'}</span>
              {h.suggested_action ? <span className="ml-2 text-ink-secondary">→ {h.suggested_action}</span> : null}
            </p>
          </li>
        );
      })}
    </ul>
  );
}

const GOAL_ICON = {
  pending: { icon: Circle, tone: 'text-ink-tertiary' },
  active: { icon: CircleDot, tone: 'text-accent' },
  done: { icon: CheckCircle2, tone: 'text-status-success' },
  failed: { icon: XCircle, tone: 'text-status-critical' },
  skipped: { icon: MinusCircle, tone: 'text-ink-tertiary' },
} as const;

function GoalNodes({ goals, parent, depth }: { goals: readonly Goal[]; parent: string | null; depth: number }) {
  const children = goals.filter((g) => (g.parent_id ?? null) === parent);
  if (children.length === 0 || depth > 6) return null;
  return (
    <ul className={cn(depth > 0 && 'ml-4 border-l border-edge pl-2')} role={depth === 0 ? 'tree' : 'group'} aria-label={depth === 0 ? 'Goals' : undefined}>
      {children.map((g) => {
        const meta = GOAL_ICON[g.status] ?? GOAL_ICON.pending;
        const Icon = meta.icon;
        return (
          <li key={g.id} role="treeitem" aria-selected={false} className="py-0.5">
            <span className="flex items-center gap-1.5">
              <Icon className={cn('h-4 w-4 shrink-0', meta.tone)} aria-hidden />
              <span className={cn('min-w-0 truncate text-[0.92rem]', g.status === 'done' ? 'text-ink-secondary' : 'text-ink-primary', g.status === 'active' && 'font-bold')}>
                {g.title}
              </span>
              <span className="ml-auto shrink-0 text-[0.68rem] font-semibold uppercase text-ink-tertiary">{g.status}</span>
            </span>
            <GoalNodes goals={goals} parent={g.id} depth={depth + 1} />
          </li>
        );
      })}
    </ul>
  );
}

export function GoalTree({ goals }: { goals: readonly Goal[] }) {
  if (goals.length === 0) return <p className="text-[0.95rem] text-ink-tertiary">No goals set.</p>;
  // Orphans (parent not in the list) are shown at the root rather than lost.
  const ids = new Set(goals.map((g) => g.id));
  const normalised = goals.map((g) => (g.parent_id && !ids.has(g.parent_id) ? { ...g, parent_id: null } : g));
  return <GoalNodes goals={normalised} parent={null} depth={0} />;
}

function compression(raw: number, card: number): string | null {
  if (!raw || !card) return null;
  return `${raw.toLocaleString()}→${card} tokens`;
}

export function EvidenceCards({ evidence }: { evidence: readonly EvidenceView[] }) {
  const reduced = useReducedMotion();
  if (evidence.length === 0) return <p className="text-[0.95rem] text-ink-tertiary">No evidence in context.</p>;
  const live = evidence.filter((v) => !v.discarded).length;
  return (
    <>
      <p className="sr-only" aria-live="polite">{live} evidence cards in context</p>
      <ul className="grid grid-cols-1 gap-2 2xl:grid-cols-2" aria-label="Evidence cards">
        <AnimatePresence initial={false}>
          {[...evidence].reverse().map(({ card, discarded }) => (
            <motion.li
              key={card.id}
              layout={!reduced}
              initial={reduced ? false : { opacity: 0, y: -6 }}
              animate={{ opacity: discarded ? 0.4 : 1, y: 0, scale: discarded && !reduced ? 0.98 : 1 }}
              exit={reduced ? { opacity: 0 } : { opacity: 0, x: 24 }}
              transition={{ duration: reduced ? 0 : 0.45 }}
              data-testid="evidence-card"
              data-discarded={discarded ? 'true' : 'false'}
              className={cn(
                'rounded-btn border bg-surface-2 px-2.5 py-2',
                discarded ? 'border-dashed border-line' : card.pinned ? 'border-accent/60' : 'border-edge',
              )}
            >
              <div className="flex flex-wrap items-center gap-1.5">
                <span className="font-mono text-[0.75rem] font-bold text-ink-secondary">{card.id}</span>
                <SourceLabel source={card.source} />
                {card.origin && card.origin !== 'tool' && card.origin !== card.source ? (
                  <span className="text-[0.7rem] text-ink-tertiary">
                    from <SourceLabel source={card.origin} />
                  </span>
                ) : null}
                <span className="font-mono text-[0.7rem] text-ink-tertiary">s{card.step} {card.tool}</span>
                {compression(card.tokens_raw, card.tokens_card) ? (
                  <span className="tnum ml-auto text-[0.72rem] font-semibold text-ink-tertiary" title="raw observation tokens → card tokens">
                    {compression(card.tokens_raw, card.tokens_card)}
                  </span>
                ) : null}
              </div>
              <p className={cn('mt-1 text-[0.92rem] leading-snug', discarded ? 'text-ink-tertiary line-through' : 'text-ink-primary')}>
                {card.claim}
              </p>
              <div className="mt-1 flex items-center gap-2 text-[0.72rem]">
                {card.url ? (
                  <a
                    href={card.url}
                    target="_blank"
                    rel="noopener noreferrer"
                    className="inline-flex min-w-0 items-center gap-1 truncate text-accent underline-offset-2 hover:underline"
                  >
                    <ExternalLink className="h-3 w-3 shrink-0" aria-hidden />
                    <span className="truncate">{card.url}</span>
                  </a>
                ) : null}
                {discarded ? (
                  <span className="ml-auto font-bold text-ink-secondary">evicted → RawTree</span>
                ) : card.pinned ? (
                  <span className="ml-auto font-bold text-accent">pinned</span>
                ) : null}
              </div>
            </motion.li>
          ))}
        </AnimatePresence>
      </ul>
    </>
  );
}
