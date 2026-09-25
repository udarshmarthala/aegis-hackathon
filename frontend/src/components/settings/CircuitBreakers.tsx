'use client';

import { EmptyState } from '@/components/ui/states';
import { cn } from '@/lib/utils';

/**
 * Circuit breaker states.
 *
 * An open breaker is a decision, not a fault: Aegis has stopped calling a
 * dependency so it can recover. Surfacing it here is what stops an engineer
 * spending an hour debugging an integration that is deliberately being left
 * alone.
 */
function tone(state: string): { chip: string; meaning: string } {
  const upper = state.toUpperCase();
  if (upper.includes('OPEN') && !upper.includes('HALF')) {
    return {
      chip: 'border-status-critical/40 bg-status-critical/10 text-status-critical',
      meaning: 'Calls are being refused locally. Aegis is protecting this dependency and itself.',
    };
  }
  if (upper.includes('HALF')) {
    return {
      chip: 'border-status-warning/40 bg-status-warning/10 text-status-warning',
      meaning: 'Trial calls are being allowed through to test whether the dependency recovered.',
    };
  }
  if (upper.includes('CLOSED')) {
    return {
      chip: 'border-status-success/40 bg-status-success/10 text-status-success',
      meaning: 'Calls flow normally.',
    };
  }
  return {
    chip: 'border-line bg-surface-3 text-ink-secondary',
    meaning: 'State reported by the resilience layer.',
  };
}

export function CircuitBreakers({ breakers }: { breakers: Record<string, string> }) {
  const entries = Object.entries(breakers).sort(([a], [b]) => a.localeCompare(b));

  if (entries.length === 0) {
    return (
      <EmptyState
        title="No circuit breaker has been exercised."
        detail="Breakers register on first use. None here means no guarded call has been made since start-up."
      />
    );
  }

  return (
    <ul className="grid gap-2 sm:grid-cols-2 xl:grid-cols-3">
      {entries.map(([name, state]) => {
        const style = tone(state);
        return (
          <li key={name} className="rounded-card border border-hairline bg-surface-2 p-3">
            <div className="flex items-start justify-between gap-2">
              <p className="truncate text-body font-semibold text-ink-primary">{name}</p>
              <span
                className={cn(
                  'shrink-0 rounded border px-1.5 py-0.5 text-meta font-semibold uppercase tracking-wider',
                  style.chip,
                )}
              >
                {state}
              </span>
            </div>
            <p className="mt-1.5 text-meta font-medium text-ink-tertiary">{style.meaning}</p>
          </li>
        );
      })}
    </ul>
  );
}
