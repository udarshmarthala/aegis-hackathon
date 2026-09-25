'use client';

import { useQuery } from '@tanstack/react-query';
import { NetworkError, api } from '@/lib/api';
import {
  EmptyState, ErrorState, SkeletonRows, SourceUnavailableState,
} from '@/components/ui/states';
import { cn } from '@/lib/utils';

/**
 * A row of the backend registry.
 *
 * Declared as a type alias rather than an interface so it carries an implicit
 * index signature: the API client types this endpoint as bare
 * `Record<string, unknown>`, and without that the narrowing below needs a
 * double cast through `unknown`, which hides any real divergence.
 */
type RegistryRow = {
  action_type: string;
  risk_tier: number;
  executable: boolean;
  idempotent: boolean;
  reversible: boolean;
  max_blast_radius: number;
  min_confidence: number;
  requires_verification: boolean;
  description: string;
};

/**
 * The real action boundary, read from the backend registry rather than
 * documented separately (UX spec 56). Tier 3 rows are shown as "never" because
 * no executor is registered for them — policy cannot allow what does not exist.
 *
 * This table is the page's safety claim, so a failed read must never fall
 * through to an empty one: a blank action boundary reads as "Aegis can perform
 * no actions", which is the opposite of the truth and the more dangerous
 * direction to be wrong in.
 */
export function ActionRegistryTable() {
  const registry = useQuery({ queryKey: ['action-registry'], queryFn: api.actionRegistry });

  if (registry.isLoading) return <SkeletonRows rows={6} />;

  if (registry.isError) {
    return registry.error instanceof NetworkError ? (
      <SourceUnavailableState
        source="Action registry"
        reason={registry.error.message}
        consequence="The action boundary cannot be read. Nothing below should be taken as the set of actions Aegis can or cannot perform."
        onRetry={() => registry.refetch()}
      />
    ) : (
      <ErrorState
        title="Cannot load the action registry"
        detail={(registry.error as Error).message}
        consequence="The action boundary cannot be shown. This is not a statement that Aegis has no registered actions."
        onRetry={() => registry.refetch()}
      />
    );
  }

  const rows = (registry.data?.items ?? []) as RegistryRow[];

  if (rows.length === 0) {
    return (
      <EmptyState
        title="The action registry is empty."
        detail="The backend answered and reported no registered action types, so Aegis can currently propose nothing."
        hint="check that the execution registry is populated in this deployment"
      />
    );
  }

  return (
    <section aria-labelledby="registry">
      <h2 id="registry" className="mb-2.5 label-meta">Action registry</h2>
      <p className="mb-2.5 text-meta text-ink-tertiary">
        Every action Aegis can represent. Risk tier comes from a static table, never from
        model output — that is what stops an agent arguing a dangerous action into a lower class.
      </p>

      <div className="card overflow-x-auto">
        <table className="w-full min-w-[820px] border-collapse text-body">
          <thead>
            <tr className="border-b border-hairline text-left">
              {['Action', 'Tier', 'Executable', 'Idempotent', 'Reversible', 'Max blast', 'Min conf.'].map(
                (header) => (
                  <th key={header} scope="col" className="px-3 py-2 label-meta font-normal">
                    {header}
                  </th>
                ),
              )}
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => (
              <tr key={row.action_type} className="border-b border-hairline last:border-0">
                <td className="px-3 py-2 align-top">
                  <span className="font-mono">{row.action_type}</span>
                  <span className="mt-0.5 block max-w-md text-meta text-ink-tertiary">
                    {row.description}
                  </span>
                </td>
                <td className="px-3 py-2 align-top tnum">
                  <span
                    className={cn(
                      row.risk_tier === 3 && 'text-status-critical',
                      row.risk_tier === 2 && 'text-status-warning',
                    )}
                  >
                    {row.risk_tier}
                  </span>
                </td>
                <td className="px-3 py-2 align-top">
                  <span className={row.executable ? 'text-ink-secondary' : 'text-status-critical'}>
                    {row.executable ? 'yes' : 'never'}
                  </span>
                </td>
                <td className="px-3 py-2 align-top text-ink-tertiary">
                  {row.idempotent ? 'yes' : 'no'}
                </td>
                <td className="px-3 py-2 align-top text-ink-tertiary">
                  {row.reversible ? 'yes' : 'no'}
                </td>
                <td className="px-3 py-2 align-top tnum text-ink-tertiary">
                  {row.max_blast_radius}
                </td>
                <td className="px-3 py-2 align-top tnum text-ink-tertiary">
                  {Math.round(row.min_confidence * 100)}%
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}
