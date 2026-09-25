'use client';

import { useMemo } from 'react';
import { cn } from '@/lib/utils';
import type { GraphLayout, NodeHealth } from './graph-model';

/**
 * The same topology as a table.
 *
 * This is the accessible equivalent of the canvas, not a consolation prize: it
 * carries every node, its health and every typed edge, so an operator on a
 * screen reader, a narrow window or a broken GPU reads the identical topology.
 */

const HEALTH_TEXT: Record<NodeHealth, string> = {
  healthy: 'text-status-success',
  degraded: 'text-status-warning',
  critical: 'text-status-critical',
  unknown: 'text-ink-tertiary',
};

const HEALTH_WORD: Record<NodeHealth, string> = {
  healthy: 'Healthy',
  degraded: 'Degraded',
  critical: 'Critical',
  unknown: 'Unknown',
};

export function TopologyList({
  layout,
  rootId,
  selectedId,
  onSelect,
}: {
  layout: GraphLayout;
  rootId: string;
  selectedId: string | null;
  onSelect: (id: string) => void;
}) {
  const outgoing = useMemo(() => {
    const map = new Map<string, Array<{ type: string; target: string }>>();
    for (const edge of layout.edges) {
      const list = map.get(edge.source) ?? [];
      list.push({ type: edge.type, target: edge.target });
      map.set(edge.source, list);
    }
    return map;
  }, [layout]);

  return (
    <div className="card overflow-hidden">
      <table className="w-full border-collapse text-body">
        <caption className="sr-only">
          Services in the neighbourhood of {rootId}, with their health and outgoing dependencies
        </caption>
        <thead>
          <tr className="border-b border-hairline text-left">
            {['Service', 'Kind', 'Health', 'Hops', 'Depends on'].map((header) => (
              <th key={header} scope="col" className="label-meta px-3 py-2 font-semibold">
                {header}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {layout.nodes.map((node) => {
            const edges = outgoing.get(node.id) ?? [];
            return (
              <tr
                key={node.id}
                className={cn(
                  'border-b border-hairline last:border-0 transition-colors duration-hover hover:bg-surface-2',
                  node.id === selectedId && 'bg-surface-3',
                )}
              >
                <td className="max-w-[260px] px-3 py-2 align-top">
                  <button
                    type="button"
                    onClick={() => onSelect(node.id)}
                    className="block max-w-full truncate text-left font-semibold text-ink-primary"
                    aria-pressed={node.id === selectedId}
                  >
                    {node.name}
                    {node.id === rootId ? (
                      <span className="ml-1.5 text-meta font-semibold text-accent">root</span>
                    ) : null}
                  </button>
                  <span className="block truncate font-mono text-meta font-medium text-ink-tertiary">
                    {node.id}
                  </span>
                </td>
                <td className="px-3 py-2 align-top font-medium text-ink-secondary">{node.kind}</td>
                <td className={cn('px-3 py-2 align-top font-semibold', HEALTH_TEXT[node.health])}>
                  {HEALTH_WORD[node.health]}
                </td>
                <td className="tnum px-3 py-2 align-top font-medium text-ink-secondary">
                  {node.hops === null ? '—' : node.hops}
                </td>
                <td className="px-3 py-2 align-top">
                  {edges.length === 0 ? (
                    <span className="font-medium text-ink-tertiary">
                      No outgoing edge in this view
                    </span>
                  ) : (
                    <ul className="space-y-0.5">
                      {edges.map((edge) => (
                        <li key={`${edge.type}-${edge.target}`} className="font-medium text-ink-secondary">
                          <span className="font-semibold uppercase tracking-wider text-ink-tertiary">
                            {edge.type}
                          </span>{' '}
                          {layout.byId.get(edge.target)?.name ?? edge.target}
                        </li>
                      ))}
                    </ul>
                  )}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
