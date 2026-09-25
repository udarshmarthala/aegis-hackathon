'use client';

import Link from 'next/link';
import { useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { ChevronDown, ChevronRight, ExternalLink } from 'lucide-react';
import { consoleApi } from '@/lib/console-api';
import type { PatchRow } from '@/lib/console-types';
import {
  EmptyState, ErrorState, Skeleton, SkeletonRows, SourceUnavailableState,
} from '@/components/ui/states';
import { DiffView } from '@/components/deployments/DiffView';
import { cn, relativeTime } from '@/lib/utils';

/**
 * Candidate repairs.
 *
 * Reproduction and test state are reported as the backend recorded them,
 * including the uncomfortable combinations: a patch that reproduced the failure,
 * passed its tests and was still rejected is a real and important outcome, and
 * hiding it would make the repair loop look better than it is.
 */

export function StateChipText({ state }: { state: string }) {
  const upper = state.toUpperCase();
  const tone =
    upper.includes('REJECT') || upper.includes('FAIL')
      ? 'border-status-critical/40 text-status-critical'
      : upper.includes('APPLIED') || upper.includes('MERGED') || upper.includes('VERIFIED')
        ? 'border-status-success/40 text-status-success'
        : upper.includes('PENDING') || upper.includes('PROPOSED') || upper.includes('AWAIT')
          ? 'border-status-warning/40 text-status-warning'
          : 'border-hairline text-ink-secondary';
  return (
    <span
      className={cn(
        'inline-block rounded border px-1.5 py-0.5 text-meta font-bold uppercase tracking-wider',
        tone,
      )}
    >
      {state}
    </span>
  );
}

/**
 * A sandbox outcome, in three states rather than two.
 *
 * `null` means no run is linked to this patch yet. Rendering that as a failure
 * would tell an operator the tests ran and did not pass, which is a different
 * and worse fact than "this has not been tested" - the same conflation the
 * backend refuses to make.
 */
function Outcome({
  ok,
  yes,
  no,
  pending,
}: {
  ok: boolean | null;
  yes: string;
  no: string;
  pending: string;
}) {
  const tone =
    ok === null
      ? 'text-ink-tertiary'
      : ok
        ? 'text-status-success'
        : 'text-status-critical';
  const dot =
    ok === null
      ? 'bg-status-neutral'
      : ok
        ? 'bg-status-success'
        : 'bg-status-critical';

  return (
    <span
      className={cn(
        'inline-flex items-center gap-1.5 text-meta font-bold uppercase tracking-wider',
        tone,
      )}
    >
      <span className={cn('h-1.5 w-1.5 rounded-full', dot)} aria-hidden />
      {ok === null ? pending : ok ? yes : no}
    </span>
  );
}

export function PatchList({ incidentId }: { incidentId?: string }) {
  const query = useQuery({
    queryKey: ['patches', incidentId ?? 'all'],
    queryFn: () => consoleApi.patches(incidentId ? { incident_id: incidentId } : {}),
    refetchInterval: 30_000,
  });

  if (query.isLoading) return <SkeletonRows rows={4} />;
  if (query.isError) {
    return (
      <ErrorState
        title="Cannot load patches"
        detail={(query.error as Error).message}
        consequence="Candidate repairs cannot be listed. This is not a statement that none exist."
        onRetry={() => query.refetch()}
      />
    );
  }

  const items = query.data?.items ?? [];
  if (items.length === 0) {
    return (
      <EmptyState
        title="No candidate patches."
        detail={
          incidentId
            ? 'Aegis has not proposed a code repair for this incident.'
            : 'Aegis has not proposed a code repair in this window.'
        }
        hint="patches appear once the debugging agent reproduces a failure in the sandbox"
      />
    );
  }

  return (
    <ul className="space-y-2">
      {items.map((patch) => (
        <PatchItem key={patch.id} patch={patch} />
      ))}
    </ul>
  );
}

function PatchItem({ patch }: { patch: PatchRow }) {
  const [open, setOpen] = useState(false);
  const panelId = `patch-diff-${patch.id}`;

  const diff = useQuery({
    queryKey: ['patch-diff', patch.id],
    queryFn: () => consoleApi.patchDiff(patch.id),
    enabled: open,
  });

  const testedAndRejected =
    patch.tests_passed === true && patch.state.toUpperCase().includes('REJECT');

  return (
    <li className="card overflow-hidden">
      <div className="flex flex-wrap items-start gap-3 p-3.5">
        <button
          type="button"
          onClick={() => setOpen((value) => !value)}
          aria-expanded={open}
          aria-controls={panelId}
          className="mt-0.5 shrink-0 rounded-btn border border-hairline p-1 text-ink-secondary
                     transition-colors duration-hover hover:bg-surface-3 hover:text-ink-primary"
          aria-label={open ? `Hide diff for patch ${patch.id}` : `Show diff for patch ${patch.id}`}
        >
          {open ? (
            <ChevronDown className="h-3.5 w-3.5" aria-hidden />
          ) : (
            <ChevronRight className="h-3.5 w-3.5" aria-hidden />
          )}
        </button>

        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            <StateChipText state={patch.state} />
            <h3 className="min-w-0 break-words text-body font-semibold text-ink-primary">
              {patch.summary || 'Untitled patch'}
            </h3>
          </div>

          <p className="mt-1 break-words text-meta font-medium text-ink-secondary">
            {patch.rationale || 'No rationale recorded.'}
          </p>

          <div className="mt-2 flex flex-wrap items-center gap-x-4 gap-y-1">
            <Outcome
              ok={patch.reproduced}
              yes="Failure reproduced"
              no="Not reproduced"
              pending="Reproduction not run"
            />
            <Outcome
              ok={patch.tests_passed}
              yes="Tests passed"
              no="Tests failed"
              pending="Tests not run"
            />
            <span className="tnum text-meta font-bold">
              <span className="text-status-success">+{patch.lines_added}</span>{' '}
              <span className="text-status-critical">-{patch.lines_removed}</span>
            </span>
            <span className="font-mono text-meta font-medium text-ink-tertiary">
              {patch.repo} @ {patch.base_ref}
            </span>
            <span className="text-meta font-medium text-ink-tertiary">
              {relativeTime(patch.created_at)}
            </span>
          </div>

          {testedAndRejected ? (
            <p className="mt-2 text-meta font-bold text-status-warning">
              This patch passed its tests and was still rejected. The tests passing was not
              sufficient evidence that it was the right repair.
            </p>
          ) : null}

          {patch.files_changed.length > 0 ? (
            <ul className="mt-2 flex flex-wrap gap-1.5">
              {patch.files_changed.map((file) => (
                <li
                  key={file}
                  className="rounded border border-hairline bg-surface-2 px-1.5 py-0.5
                             font-mono text-meta font-medium text-ink-secondary"
                >
                  {file}
                </li>
              ))}
            </ul>
          ) : (
            <p className="mt-2 text-meta font-semibold text-ink-tertiary">
              No changed files recorded on this patch.
            </p>
          )}

          <div className="mt-2 flex flex-wrap items-center gap-3">
            <Link
              href={`/incidents/${patch.incident_id}`}
              className="text-meta font-semibold text-accent transition-opacity duration-hover hover:opacity-80"
            >
              {patch.incident_id}
            </Link>
            {patch.pull_request_url ? (
              <a
                href={patch.pull_request_url}
                target="_blank"
                rel="noreferrer noopener"
                className="inline-flex items-center gap-1 text-meta font-semibold text-ink-secondary
                           transition-colors duration-hover hover:text-ink-primary"
              >
                Pull request
                <ExternalLink className="h-3 w-3" aria-hidden />
              </a>
            ) : null}
          </div>
        </div>
      </div>

      {open ? (
        <div id={panelId} className="border-t border-hairline bg-surface-1 p-3.5">
          {diff.isLoading ? (
            <Skeleton className="h-40 w-full" />
          ) : diff.isError ? (
            <ErrorState
              title="Cannot load the diff"
              detail={(diff.error as Error).message}
              consequence="The change itself cannot be reviewed. Do not judge this patch on its summary."
              onRetry={() => diff.refetch()}
            />
          ) : diff.data && !diff.data.found ? (
            <SourceUnavailableState
              source="Patch diff"
              reason="The diff artifact for this patch is not in the store."
              consequence="The patch record exists but its content cannot be shown, so it cannot be reviewed here."
              onRetry={() => diff.refetch()}
            />
          ) : !diff.data?.diff ? (
            <EmptyState
              title="This patch contains no changes."
              detail="The diff was retrieved successfully and is empty."
            />
          ) : (
            <>
              <DiffView diff={diff.data.diff} label={`${patch.repo} @ ${patch.base_ref}`} />
              {diff.data.diff_sha256 ? (
                <p className="mt-2 break-all font-mono text-meta font-medium text-ink-tertiary">
                  sha256 {diff.data.diff_sha256}
                </p>
              ) : null}
            </>
          )}
        </div>
      ) : null}
    </li>
  );
}
