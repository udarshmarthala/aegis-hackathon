'use client';

import { useQuery } from '@tanstack/react-query';
import { X } from 'lucide-react';
import { consoleApi } from '@/lib/console-api';
import { isAvailable } from '@/lib/console-types';
import { ErrorState, SkeletonRows, SourceUnavailableState } from '@/components/ui/states';
import { cn, pct } from '@/lib/utils';
import type { NodeHealth, PositionedNode } from './graph-model';

/**
 * Detail for the selected node.
 *
 * Blast radius and dependencies are separate queries on purpose: one being
 * unavailable must not blank the other, and "the topology store could not
 * answer" has to arrive as a stated reason rather than an empty dependency list
 * that reads as a service with no dependencies at all.
 */

const HEALTH_TONE: Record<NodeHealth, string> = {
  healthy: 'border-status-success/40 bg-status-success/10 text-status-success',
  degraded: 'border-status-warning/40 bg-status-warning/10 text-status-warning',
  critical: 'border-status-critical/40 bg-status-critical/10 text-status-critical',
  unknown: 'border-line bg-surface-3 text-ink-secondary',
};

const HEALTH_WORD: Record<NodeHealth, string> = {
  healthy: 'Healthy',
  degraded: 'Degraded',
  critical: 'Critical',
  unknown: 'Health unknown',
};

function asRecord(value: unknown): Record<string, unknown> | null {
  return typeof value === 'object' && value !== null ? (value as Record<string, unknown>) : null;
}

function readText(value: unknown, ...keys: string[]): string | null {
  const record = asRecord(value);
  if (!record) return null;
  for (const key of keys) {
    const field = record[key];
    if (typeof field === 'string' && field.trim() !== '') return field;
  }
  return null;
}

function readIds(value: unknown, key: string): string[] {
  const record = asRecord(value);
  const field = record?.[key];
  if (!Array.isArray(field)) return [];
  return field.filter((item): item is string => typeof item === 'string');
}

export function NodeDetailPanel({ node, onClose }: { node: PositionedNode; onClose: () => void }) {
  const radius = useQuery({
    queryKey: ['graph', 'blast-radius', node.id],
    queryFn: () => consoleApi.blastRadius(node.id),
    staleTime: 30_000,
  });

  const dependencies = useQuery({
    queryKey: ['graph', 'dependencies', node.id],
    queryFn: () => consoleApi.dependencies(node.id),
    staleTime: 30_000,
  });

  return (
    <aside className="card flex h-full flex-col overflow-hidden" aria-label={`Detail for ${node.name}`}>
      <header className="flex items-start justify-between gap-3 border-b border-hairline px-4 py-3">
        <div className="min-w-0">
          <h2 className="truncate text-body font-semibold text-ink-primary">{node.name}</h2>
          <p className="mt-0.5 truncate font-mono text-meta font-medium text-ink-tertiary">
            {node.id}
          </p>
        </div>
        <button
          type="button"
          onClick={onClose}
          aria-label="Close service detail"
          className="shrink-0 rounded-btn border border-line p-1 text-ink-tertiary
                     transition-colors duration-hover hover:bg-surface-3 hover:text-ink-primary"
        >
          <X className="h-3.5 w-3.5" aria-hidden />
        </button>
      </header>

      <div className="flex-1 space-y-4 overflow-y-auto px-4 py-3.5">
        <div className="flex flex-wrap items-center gap-2">
          <span
            className={cn(
              'rounded border px-1.5 py-0.5 text-meta font-semibold uppercase tracking-wider',
              HEALTH_TONE[node.health],
            )}
          >
            {HEALTH_WORD[node.health]}
          </span>
          <span className="rounded border border-line px-1.5 py-0.5 text-meta font-semibold uppercase tracking-wider text-ink-secondary">
            {node.kind}
          </span>
          {node.environment ? (
            <span className="rounded border border-line px-1.5 py-0.5 text-meta font-semibold uppercase tracking-wider text-ink-secondary">
              {node.environment}
            </span>
          ) : null}
        </div>

        <dl className="grid grid-cols-2 gap-x-3 gap-y-2">
          <Field label="Connections" value={String(node.degree)} />
          <Field label="Hops from root" value={node.hops === null ? 'root' : String(node.hops)} />
        </dl>

        <section aria-labelledby="blast-radius-heading" className="space-y-2">
          <h3 id="blast-radius-heading" className="label-meta font-semibold">
            Blast radius
          </h3>
          {radius.isLoading ? (
            <SkeletonRows rows={2} />
          ) : radius.isError ? (
            <ErrorState
              title="Blast radius unavailable"
              detail={(radius.error as Error).message}
              consequence="The impact of an action on this service cannot be estimated from here."
              onRetry={() => radius.refetch()}
            />
          ) : radius.data && !isAvailable(radius.data) ? (
            <SourceUnavailableState
              source="Topology"
              reason={radius.data.reason}
              consequence="Aegis cannot say what fails with this service. Treat any action here as higher risk."
              onRetry={() => radius.refetch()}
            />
          ) : radius.data ? (
            <div className="space-y-2">
              <dl className="grid grid-cols-2 gap-x-3 gap-y-2">
                <Field label="Services affected" value={String(radius.data.size)} />
                <Field label="Request share" value={pct(radius.data.estimated_request_share, 1)} />
                <Field
                  label="Customer facing"
                  value={radius.data.customer_facing ? 'Yes' : 'No'}
                  tone={radius.data.customer_facing ? 'warning' : undefined}
                />
              </dl>
              <IdList label="Directly affected" ids={radius.data.directly_affected} />
              <IdList label="Downstream" ids={radius.data.downstream} />
            </div>
          ) : null}
        </section>

        <section aria-labelledby="dependencies-heading" className="space-y-2">
          <h3 id="dependencies-heading" className="label-meta font-semibold">
            Dependencies
          </h3>
          {dependencies.isLoading ? (
            <SkeletonRows rows={2} />
          ) : dependencies.isError ? (
            <ErrorState
              title="Dependencies unavailable"
              detail={(dependencies.error as Error).message}
              consequence="Upstream dependencies are unknown for this service."
              onRetry={() => dependencies.refetch()}
            />
          ) : dependencies.data && !isAvailable(dependencies.data) ? (
            <SourceUnavailableState
              source="Topology"
              reason={dependencies.data.reason}
              consequence="This is not a service without dependencies — it is a service Aegis could not look up."
              onRetry={() => dependencies.refetch()}
            />
          ) : dependencies.data ? (
            <div className="space-y-2">
              <NamedList
                label="Upstream"
                empty="No upstream dependency is recorded in the topology."
                items={dependencies.data.upstream.map((item, index) => ({
                  key: `${readText(item, 'node_id', 'resource_id') ?? 'node'}-${index}`,
                  primary: readText(item, 'name', 'node_id') ?? 'unnamed node',
                  secondary: readText(item, 'label') ?? '',
                }))}
              />
              <NamedList
                label="Shares a dependency with"
                empty="No peer shares a datastore, cache or queue with this service."
                items={dependencies.data.sharing_a_dependency.map((item, index) => ({
                  key: `${readText(item, 'resource_id') ?? 'shared'}-${index}`,
                  primary: readText(item, 'name', 'resource_id') ?? 'unnamed resource',
                  secondary: readIds(item, 'service_ids').join(', '),
                }))}
              />
            </div>
          ) : null}
        </section>
      </div>
    </aside>
  );
}

function Field({ label, value, tone }: { label: string; value: string; tone?: 'warning' }) {
  return (
    <div>
      <dt className="label-meta font-semibold">{label}</dt>
      <dd
        className={cn(
          'tnum mt-0.5 text-body font-semibold',
          tone === 'warning' ? 'text-status-warning' : 'text-ink-primary',
        )}
      >
        {value}
      </dd>
    </div>
  );
}

function IdList({ label, ids }: { label: string; ids: string[] }) {
  return (
    <div>
      <p className="label-meta font-semibold">{label}</p>
      {ids.length === 0 ? (
        <p className="mt-0.5 text-meta font-medium text-ink-tertiary">None recorded.</p>
      ) : (
        <ul className="mt-1 flex flex-wrap gap-1">
          {ids.map((id) => (
            <li
              key={id}
              className="rounded border border-hairline bg-surface-2 px-1.5 py-0.5
                         font-mono text-meta font-medium text-ink-secondary"
            >
              {id}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function NamedList({
  label,
  items,
  empty,
}: {
  label: string;
  items: Array<{ key: string; primary: string; secondary: string }>;
  empty: string;
}) {
  return (
    <div>
      <p className="label-meta font-semibold">{label}</p>
      {items.length === 0 ? (
        <p className="mt-0.5 text-meta font-medium text-ink-tertiary">{empty}</p>
      ) : (
        <ul className="mt-1 space-y-1">
          {items.map((item) => (
            <li key={item.key} className="rounded border border-hairline bg-surface-2 px-2 py-1">
              <p className="truncate text-meta font-semibold text-ink-primary">{item.primary}</p>
              {item.secondary ? (
                <p className="truncate text-meta font-medium text-ink-tertiary">{item.secondary}</p>
              ) : null}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
