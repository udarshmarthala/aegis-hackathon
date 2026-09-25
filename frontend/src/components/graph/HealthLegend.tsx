'use client';

/**
 * Legend for the canvas.
 *
 * Health is encoded twice - colour and outline - so the legend names both. A
 * legend that only explains colour is useless to the operators who need one.
 */
const ENTRIES: Array<{ label: string; ring: string; dash?: string }> = [
  { label: 'Healthy', ring: 'stroke-status-success' },
  { label: 'Degraded (dashed)', ring: 'stroke-status-warning', dash: '4 2' },
  { label: 'Critical (heavy)', ring: 'stroke-status-critical' },
  { label: 'Unknown (dotted)', ring: 'stroke-status-neutral', dash: '1 2' },
];

export function HealthLegend({ hasSelection }: { hasSelection: boolean }) {
  return (
    <ul className="flex flex-wrap items-center gap-x-4 gap-y-1.5" aria-label="Graph legend">
      {ENTRIES.map((entry) => (
        <li key={entry.label} className="flex items-center gap-1.5">
          <svg width="14" height="14" viewBox="0 0 14 14" aria-hidden>
            <circle
              cx="7"
              cy="7"
              r="5"
              className={`fill-none ${entry.ring}`}
              strokeWidth={entry.label.startsWith('Critical') ? 2.4 : 1.4}
              strokeDasharray={entry.dash}
            />
          </svg>
          <span className="text-meta font-semibold text-ink-secondary">{entry.label}</span>
        </li>
      ))}
      <li className="flex items-center gap-1.5">
        <svg width="14" height="14" viewBox="0 0 14 14" aria-hidden>
          <circle cx="7" cy="7" r="5.5" className="fill-none stroke-status-warning"
                  strokeWidth={1.2} strokeDasharray="3 3" />
        </svg>
        <span className="text-meta font-semibold text-ink-secondary">
          {hasSelection ? 'In blast radius of selection' : 'Blast radius, once a node is selected'}
        </span>
      </li>
    </ul>
  );
}
