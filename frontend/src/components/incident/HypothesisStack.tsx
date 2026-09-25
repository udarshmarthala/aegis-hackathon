'use client';

import { useState } from 'react';
import { ChevronDown } from 'lucide-react';
import type { Hypothesis } from '@/lib/types';
import { cn } from '@/lib/utils';
import { EmptyState } from '@/components/ui/states';

/**
 * A hypothesis stack, never a single AI conclusion (UX spec 21).
 *
 * Showing the alternatives — and what was ruled out — is what lets an engineer
 * disagree with Aegis on evidence rather than on vibes. Confidence shown here is
 * derived by the backend from evidence coverage and corroboration; it is never
 * a model's self-reported number.
 */
export function HypothesisStack({
  hypotheses,
  onCiteEvidence,
}: {
  hypotheses: Hypothesis[];
  onCiteEvidence?: (evidenceId: string) => void;
}) {
  const [expanded, setExpanded] = useState<string | null>(hypotheses[0]?.id ?? null);

  if (hypotheses.length === 0) {
    return (
      <EmptyState
        title="No hypotheses yet."
        detail="Aegis forms competing explanations once it has gathered evidence."
      />
    );
  }

  return (
    <ul className="space-y-1.5">
      {hypotheses.map((h, index) => {
        const open = expanded === h.id;
        const rejected = h.state === 'REJECTED';
        const strongest = index === 0 && !rejected;
        const percent = Math.round(h.confidence * 100);

        return (
          <li key={h.id} className={cn('card overflow-hidden', rejected && 'opacity-55')}>
            <button
              type="button"
              onClick={() => setExpanded(open ? null : h.id)}
              aria-expanded={open}
              className="flex w-full items-start gap-3 px-3.5 py-3 text-left
                         transition-colors duration-hover hover:bg-surface-2"
            >
              <span className="tnum mt-0.5 shrink-0 font-mono text-meta text-ink-tertiary">
                {String(index + 1).padStart(2, '0')}
              </span>

              <span className="min-w-0 flex-1">
                <span className="flex items-baseline gap-2">
                  <span className="truncate text-body font-medium">{h.statement}</span>
                  {strongest ? (
                    <span className="shrink-0 text-meta text-status-info">strongest</span>
                  ) : null}
                  {rejected ? (
                    <span className="shrink-0 text-meta text-ink-tertiary">rejected</span>
                  ) : null}
                </span>
                <span className="mt-1 flex items-center gap-2">
                  <span className="h-1 w-24 overflow-hidden rounded-full bg-surface-4">
                    <span
                      className={cn(
                        'block h-full rounded-full',
                        percent >= 75
                          ? 'bg-status-success'
                          : percent >= 45
                            ? 'bg-status-warning'
                            : 'bg-status-critical',
                      )}
                      style={{ width: `${percent}%` }}
                    />
                  </span>
                  <span className="tnum text-meta text-ink-secondary">{percent}%</span>
                  <span className="text-meta text-ink-tertiary">
                    {h.supporting.length} supporting · {h.contradicting.length} contradicting
                  </span>
                </span>
              </span>

              <ChevronDown
                className={cn(
                  'mt-0.5 h-4 w-4 shrink-0 text-ink-tertiary transition-transform duration-hover',
                  open && 'rotate-180',
                )}
                aria-hidden
              />
            </button>

            {open ? (
              <div className="space-y-3 border-t border-hairline px-3.5 py-3">
                <EvidenceRefRow label="Supporting" ids={h.supporting} onCite={onCiteEvidence} />
                <EvidenceRefRow
                  label="Contradicting"
                  ids={h.contradicting}
                  onCite={onCiteEvidence}
                  tone="critical"
                />

                {h.missing.length ? (
                  <div>
                    <p className="label-meta mb-1">Missing evidence</p>
                    <ul className="space-y-0.5">
                      {h.missing.map((m) => (
                        <li key={m} className="text-meta text-ink-secondary">— {m}</li>
                      ))}
                    </ul>
                  </div>
                ) : null}

                {h.predictions.length ? (
                  <div>
                    <p className="label-meta mb-1">Predictions</p>
                    <ul className="space-y-1">
                      {h.predictions.map((p, i) => (
                        <li key={i} className="flex items-start gap-2 text-meta">
                          <span
                            className={cn(
                              'mt-1 h-1.5 w-1.5 shrink-0 rounded-full',
                              !p.tested
                                ? 'bg-status-neutral'
                                : p.holds
                                  ? 'bg-status-success'
                                  : 'bg-status-critical',
                            )}
                            aria-hidden
                          />
                          <span className="text-ink-secondary">
                            {p.statement}
                            <span className="text-ink-tertiary">
                              {' '}
                              — {!p.tested ? 'not yet tested' : p.holds ? 'held' : 'did not hold'}
                            </span>
                          </span>
                        </li>
                      ))}
                    </ul>
                  </div>
                ) : null}

                {h.rejected_reason ? (
                  <p className="text-meta text-ink-tertiary">
                    Rejected because: {h.rejected_reason}
                  </p>
                ) : null}
              </div>
            ) : null}
          </li>
        );
      })}
    </ul>
  );
}

function EvidenceRefRow({
  label,
  ids,
  onCite,
  tone,
}: {
  label: string;
  ids: string[];
  onCite?: (id: string) => void;
  tone?: 'critical';
}) {
  if (!ids.length) return null;
  return (
    <div>
      <p className="label-meta mb-1">{label}</p>
      <div className="flex flex-wrap gap-1">
        {ids.map((id) => (
          <button
            key={id}
            type="button"
            onClick={() => onCite?.(id)}
            className={cn(
              'rounded border px-1.5 py-0.5 font-mono text-meta transition-colors duration-hover',
              tone === 'critical'
                ? 'border-status-critical/30 text-status-critical/90 hover:bg-status-critical/10'
                : 'border-line text-ink-secondary hover:bg-surface-3',
            )}
            title="Jump to this evidence item"
          >
            {id.slice(0, 11)}
          </button>
        ))}
      </div>
    </div>
  );
}
