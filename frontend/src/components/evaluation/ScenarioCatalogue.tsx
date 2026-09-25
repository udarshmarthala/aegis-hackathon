'use client';

import { useMemo, useState } from 'react';
import { EmptyState } from '@/components/ui/states';
import { cn } from '@/lib/utils';

export interface CatalogueEntry {
  id: string;
  title: string;
  category: string;
  workload: string;
  difficulty: string;
}

/**
 * The ground-truth scenario catalogue.
 *
 * What Aegis is measured against has to be inspectable, or a benchmark number
 * is an assertion with no basis. Filtering is client-side over an already
 * bounded list, so it costs nothing and never hides a category silently: the
 * active filter is always named.
 */
export function ScenarioCatalogue({ entries }: { entries: CatalogueEntry[] }) {
  const [category, setCategory] = useState<string>('');

  const categories = useMemo(
    () => [...new Set(entries.map((entry) => entry.category))].sort(),
    [entries],
  );

  const rows = category === '' ? entries : entries.filter((entry) => entry.category === category);

  if (entries.length === 0) {
    return (
      <EmptyState
        title="The scenario catalogue is empty."
        detail="Ground-truth scenarios are loaded from eval/scenarios. None are registered."
      />
    );
  }

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-1" role="group" aria-label="Filter by category">
        <button
          type="button"
          onClick={() => setCategory('')}
          aria-pressed={category === ''}
          className={cn(
            'rounded-btn border px-2 py-1 text-meta font-semibold transition-colors duration-hover',
            category === ''
              ? 'border-edge bg-surface-3 text-ink-primary'
              : 'border-hairline text-ink-tertiary hover:bg-surface-2',
          )}
        >
          All ({entries.length})
        </button>
        {categories.map((value) => (
          <button
            key={value}
            type="button"
            onClick={() => setCategory(value)}
            aria-pressed={category === value}
            className={cn(
              'rounded-btn border px-2 py-1 text-meta font-semibold transition-colors duration-hover',
              category === value
                ? 'border-edge bg-surface-3 text-ink-primary'
                : 'border-hairline text-ink-tertiary hover:bg-surface-2',
            )}
          >
            {value}
          </button>
        ))}
      </div>

      <table className="w-full border-collapse text-body">
        <caption className="sr-only">
          Ground-truth evaluation scenarios{category ? ` in category ${category}` : ''}
        </caption>
        <thead>
          <tr className="border-b border-hairline text-left">
            {['Scenario', 'Category', 'Workload', 'Difficulty'].map((header) => (
              <th key={header} scope="col" className="label-meta px-3 py-2 font-semibold">
                {header}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((entry) => (
            <tr key={entry.id} className="border-b border-hairline last:border-0">
              <th scope="row" className="max-w-[420px] px-3 py-2 text-left">
                <span className="block truncate font-semibold text-ink-primary">{entry.title}</span>
                <span className="block truncate font-mono text-meta font-medium text-ink-tertiary">
                  {entry.id}
                </span>
              </th>
              <td className="px-3 py-2 font-medium text-ink-secondary">{entry.category}</td>
              <td className="px-3 py-2 font-medium text-ink-secondary">{entry.workload}</td>
              <td className="px-3 py-2 font-medium text-ink-secondary">{entry.difficulty}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
