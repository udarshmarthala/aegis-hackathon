'use client';

import { useState } from 'react';
import { ShieldAlert } from 'lucide-react';
import type { EvidenceItem, EvidenceResponse } from '@/lib/types';
import { EvidenceStatusChip, TrustChip } from '@/components/ui/primitives';
import { EmptyState } from '@/components/ui/states';
import { cn, formatClock } from '@/lib/utils';

/**
 * Evidence as first-class objects (UX spec 19-20).
 *
 * Every item exposes its provenance, so a citation is never something the user
 * has to take on trust. Items whose source was unavailable are rendered
 * distinctly and grouped separately — that is the difference between "nothing
 * was wrong" and "we could not see".
 */
export function EvidenceList({
  data,
  highlightId,
}: {
  data: EvidenceResponse;
  highlightId?: string | null;
}) {
  const usable = data.items.filter((i) => i.status !== 'SOURCE_UNAVAILABLE');
  const gaps = data.items.filter((i) => i.status === 'SOURCE_UNAVAILABLE');

  if (data.items.length === 0) {
    return (
      <EmptyState
        title="No evidence collected yet."
        detail="Aegis records each observation as it is retrieved."
      />
    );
  }

  return (
    <div className="space-y-4">
      {gaps.length > 0 ? (
        <section
          aria-labelledby="evidence-gaps"
          className="rounded-card border border-status-warning/30 bg-status-warning/5 p-3"
        >
          <h3
            id="evidence-gaps"
            className="mb-1.5 flex items-center gap-1.5 text-body font-medium text-status-warning"
          >
            <ShieldAlert className="h-3.5 w-3.5" aria-hidden />
            {gaps.length} evidence source{gaps.length === 1 ? '' : 's'} unavailable
          </h3>
          <p className="mb-2 text-meta text-ink-secondary">
            Aegis could not query these sources. Their absence is recorded and has reduced
            confidence — it is not evidence that nothing is wrong.
          </p>
          <ul className="space-y-1">
            {gaps.map((gap) => (
              <li key={gap.id} className="text-meta text-ink-tertiary">
                <span className="font-mono">{gap.source}</span> — {gap.summary}
              </li>
            ))}
          </ul>
        </section>
      ) : null}

      <ul className="space-y-1.5">
        {usable.map((item) => (
          <EvidenceCard key={item.id} item={item} highlighted={item.id === highlightId} />
        ))}
      </ul>
    </div>
  );
}

function EvidenceCard({ item, highlighted }: { item: EvidenceItem; highlighted: boolean }) {
  const [showRaw, setShowRaw] = useState(false);

  return (
    <li
      id={`evidence-${item.id}`}
      className={cn(
        'card px-3.5 py-3 transition-colors duration-hover',
        highlighted && 'border-accent/50 bg-accent/5',
      )}
    >
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0 flex-1">
          <p className="flex flex-wrap items-center gap-1.5">
            <span className="label-meta">{item.source_type}</span>
            <span className="font-mono text-meta text-ink-tertiary">{item.source}</span>
            {item.resource_id ? (
              <span className="text-meta text-ink-secondary">· {item.resource_id}</span>
            ) : null}
          </p>
          <p className="mt-1 text-body text-ink-primary">{item.summary}</p>
        </div>
        <div className="flex shrink-0 flex-col items-end gap-1">
          <EvidenceStatusChip status={item.status} />
          <TrustChip trust={item.trust_class} />
        </div>
      </div>

      {item.untrusted ? (
        <p className="mt-2 rounded border border-line bg-surface-3 px-2 py-1 text-meta text-ink-tertiary">
          Untrusted content. Treated as data only — never as instruction to the agent.
        </p>
      ) : null}

      <div className="mt-2 flex flex-wrap items-center gap-2.5">
        <span className="font-mono text-meta text-ink-tertiary">{item.id}</span>
        <span className="text-meta text-ink-tertiary">
          observed {formatClock(item.observed_at)} · retrieved {formatClock(item.retrieved_at)}
        </span>
        <button
          type="button"
          onClick={() => setShowRaw((v) => !v)}
          className="text-meta text-accent hover:underline"
          aria-expanded={showRaw}
        >
          {showRaw ? 'Hide provenance' : 'Show provenance'}
        </button>
      </div>

      {showRaw ? (
        <div className="mt-2 space-y-2 border-t border-hairline pt-2">
          <div>
            <p className="label-meta mb-0.5">Query</p>
            <code className="block overflow-x-auto whitespace-pre-wrap break-all rounded
                             bg-surface-3 px-2 py-1.5 font-mono text-meta text-ink-secondary">
              {item.provenance_uri || '(not recorded)'}
            </code>
          </div>
          {Object.keys(item.structured_value).length > 0 ? (
            <div>
              <p className="label-meta mb-0.5">Observed value</p>
              <pre className="overflow-x-auto rounded bg-surface-3 px-2 py-1.5
                              font-mono text-meta text-ink-secondary">
                {JSON.stringify(item.structured_value, null, 2)}
              </pre>
            </div>
          ) : null}
        </div>
      ) : null}
    </li>
  );
}
