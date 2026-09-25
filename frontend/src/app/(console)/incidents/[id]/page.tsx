'use client';

import { useCallback, useEffect, useState } from 'react';
import { useParams } from 'next/navigation';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { toast } from 'sonner';
import * as Dialog from '@radix-ui/react-dialog';
import { CheckCircle2, RefreshCw } from 'lucide-react';
import { api, subscribeIncident } from '@/lib/api';
import { ConfidenceMeter, SeverityBadge, StateChip } from '@/components/ui/primitives';
import {
  EmptyState, QueryFailure, SkeletonRows, WorkingIndicator,
} from '@/components/ui/states';
import { EvidenceList } from '@/components/incident/EvidenceList';
import { HypothesisStack } from '@/components/incident/HypothesisStack';
import { Timeline } from '@/components/incident/Timeline';
import { cn, formatDuration } from '@/lib/utils';

const LIVE_STATES = ['TRIAGING', 'INVESTIGATING', 'DIAGNOSING', 'DEBUGGING', 'VERIFYING'];

/**
 * Confirmation for resolving an incident.
 *
 * Resolving is a human-authored state change, so it is confirmed the way an
 * approval is (UX spec 39) rather than with a second, weaker pattern: a modal
 * that names the consequence, and a confirm button that says what it does.
 *
 * The reason is not optional and is not defaulted. It is the only record of why
 * a human closed an investigation the system had not closed itself, and it
 * lands in the audit trail — a default would put a sentence nobody meant into
 * permanent evidence.
 */
function ResolveDialog({
  incident,
  open,
  onOpenChange,
  reason,
  onReasonChange,
  onConfirm,
  pending,
}: {
  incident: { id: string; title: string };
  open: boolean;
  onOpenChange: (open: boolean) => void;
  reason: string;
  onReasonChange: (reason: string) => void;
  onConfirm: () => void;
  pending: boolean;
}) {
  const submittable = reason.trim().length > 0 && !pending;

  // A resolve is in flight from the moment it is confirmed. Every dismissal
  // route - Cancel, Escape, a click on the overlay - funnels through
  // onOpenChange, so refusing to close here covers all three in one place
  // rather than three. Closing mid-request would hide the outcome: the operator
  // would not learn whether the incident actually closed, and a failure would
  // vanish along with the reason they typed.
  const handleOpenChange = (next: boolean) => {
    if (!next && pending) return;
    onOpenChange(next);
  };

  return (
    <Dialog.Root open={open} onOpenChange={handleOpenChange}>
      <Dialog.Portal>
        <Dialog.Overlay className="fixed inset-0 z-40 bg-canvas/85 backdrop-blur-[2px]" />
        <Dialog.Content
          className="card fixed left-1/2 top-1/2 z-50 max-h-[86vh] w-[min(560px,94vw)]
                     -translate-x-1/2 -translate-y-1/2 overflow-auto bg-surface-1 p-5
                     shadow-2xl focus:outline-none"
        >
          <div className="flex items-start gap-3">
            <CheckCircle2 className="mt-0.5 h-5 w-5 shrink-0 text-status-success" aria-hidden />
            <div className="min-w-0">
              <Dialog.Title className="text-h3 font-bold tracking-tight text-ink-primary">
                Resolve {incident.id}
              </Dialog.Title>
              <Dialog.Description className="mt-1 text-body font-medium text-ink-secondary">
                This closes the incident and ends the investigation. Nothing changes in the
                environment, and the console cannot reopen it.
              </Dialog.Description>
            </div>
          </div>

          <p className="mt-4 break-words text-body font-medium text-ink-secondary">
            {incident.title}
          </p>

          <div className="mt-4">
            <label htmlFor="resolve-reason" className="label-meta block font-semibold">
              Why this is resolved (recorded in the audit trail)
            </label>
            <textarea
              id="resolve-reason"
              value={reason}
              onChange={(event) => onReasonChange(event.target.value)}
              rows={3}
              maxLength={1000}
              required
              // The reason was captured by value when Resolve was pressed, so
              // anything typed after that never reaches the audit trail - and
              // would be wiped by the reset on success. readOnly rather than
              // disabled: it still prevents the edit, but keeps the field
              // focusable, so focus is not evicted mid-request.
              readOnly={pending}
              aria-describedby="resolve-reason-hint"
              placeholder="What actually fixed it, or why this is no longer an incident."
              className="mt-1.5 w-full rounded-btn border border-hairline bg-surface-2 p-2.5
                         text-body font-medium text-ink-primary placeholder:text-ink-tertiary"
            />
            <p id="resolve-reason-hint" className="mt-1 text-meta text-ink-tertiary">
              Required. Without it there is no record of why a human closed this.
            </p>
          </div>

          <div className="mt-5 flex flex-wrap items-center justify-end gap-2">
            <span id="resolve-status" aria-live="polite">
              {pending ? <WorkingIndicator label="Recording resolution…" /> : null}
            </span>
            {/*
              Both controls use aria-disabled rather than the disabled
              attribute. Confirm is the one the operator has just activated, so
              it still holds focus when `pending` flips - and disabling a
              focused element drops focus to the body, throwing a keyboard or
              screen-reader user out of the dialog at the exact moment the
              request starts. aria-disabled announces the same state while
              keeping the element focusable; the handlers below do the refusing.
            */}
            <Dialog.Close asChild>
              <button
                type="button"
                aria-disabled={pending}
                aria-describedby={pending ? 'resolve-status' : undefined}
                className="rounded-btn border border-line px-3 py-2 text-body font-semibold
                           text-ink-secondary transition-colors duration-hover hover:bg-surface-3
                           aria-disabled:cursor-not-allowed aria-disabled:opacity-50"
              >
                Cancel
              </button>
            </Dialog.Close>
            <button
              type="button"
              onClick={() => {
                // Guards the repeat activation that `disabled` would otherwise
                // have blocked, so a second Enter cannot fire a second resolve.
                if (!submittable) return;
                onConfirm();
              }}
              aria-disabled={!submittable}
              aria-describedby={pending ? 'resolve-status' : undefined}
              className="rounded-btn border border-status-success/50 bg-status-success/10 px-3 py-2
                         text-body font-bold text-status-success transition-colors duration-hover
                         hover:bg-status-success/20 aria-disabled:cursor-not-allowed
                         aria-disabled:opacity-50"
            >
              Resolve incident
            </button>
          </div>
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}

/**
 * The incident page answers five questions immediately (UX spec 12):
 * what is happening, how bad is it, what does Aegis think is causing it,
 * what has it proven, and what needs to happen next.
 */
export default function IncidentDetailPage() {
  const params = useParams<{ id: string }>();
  const id = params.id;
  const qc = useQueryClient();
  const [highlight, setHighlight] = useState<string | null>(null);
  const [resolveOpen, setResolveOpen] = useState(false);
  const [resolveReason, setResolveReason] = useState('');

  /**
   * The one way the resolve dialog closes.
   *
   * Closing and discarding the draft reason belong together: a reason abandoned
   * once was abandoned on purpose, and carrying it into the next attempt invites
   * an operator to submit a sentence they wrote about something else - which is
   * permanent once it reaches the audit trail. Both the success path and a
   * deliberate dismissal route through here so the two can never drift apart.
   * The failure path deliberately does not call it: a rejected transition is
   * something to read and retry, not something to lose the typing over.
   */
  const closeResolveDialog = useCallback(() => {
    setResolveOpen(false);
    setResolveReason('');
  }, []);

  const incident = useQuery({ queryKey: ['incident', id], queryFn: () => api.getIncident(id) });
  const evidence = useQuery({ queryKey: ['evidence', id], queryFn: () => api.getEvidence(id) });
  const timeline = useQuery({ queryKey: ['timeline', id], queryFn: () => api.getTimeline(id) });
  const hypotheses = useQuery({ queryKey: ['hypotheses', id], queryFn: () => api.getHypotheses(id) });
  const diagnosis = useQuery({ queryKey: ['diagnosis', id], queryFn: () => api.getDiagnosis(id) });

  const live = incident.data ? LIVE_STATES.includes(incident.data.state) : false;

  // Live updates arrive over SSE. Rather than re-rendering the page per event,
  // the affected queries are invalidated so React Query reconciles incrementally
  // (UX spec 112).
  useEffect(() => {
    if (!id) return;
    const unsubscribe = subscribeIncident(id, (type) => {
      if (type === 'evidence') qc.invalidateQueries({ queryKey: ['evidence', id] });
      if (type === 'hypotheses') qc.invalidateQueries({ queryKey: ['hypotheses', id] });
      if (type === 'diagnosis') qc.invalidateQueries({ queryKey: ['diagnosis', id] });
      if (type === 'phase' || type === 'snapshot' || type === 'finished') {
        qc.invalidateQueries({ queryKey: ['incident', id] });
      }
      qc.invalidateQueries({ queryKey: ['timeline', id] });
    });
    return unsubscribe;
  }, [id, qc]);

  const resolveIncident = useMutation({
    mutationFn: (reason: string) => api.resolve(id, reason),
    onSuccess: () => {
      toast.success('Incident resolved.', {
        description: 'The reason you gave is recorded in the audit trail.',
      });
      closeResolveDialog();
      // The new state has to render, and the transition is itself a timeline
      // entry, so both queries are refreshed rather than the header alone.
      qc.invalidateQueries({ queryKey: ['incident', id] });
      qc.invalidateQueries({ queryKey: ['timeline', id] });
    },
    onError: (error: Error) => {
      // The dialog stays open with the reason intact: a rejected transition is
      // something to read and retry, not something to lose the typing over.
      toast.error('The incident was not resolved.', { description: error.message });
    },
  });

  async function reinvestigate() {
    try {
      const result = await api.reinvestigate(id, 'requested from incident page');
      toast[result.scheduled ? 'success' : 'info'](
        result.scheduled ? 'Investigation queued' : result.reason,
      );
    } catch (err) {
      toast.error((err as Error).message);
    }
  }

  function jumpToEvidence(evidenceId: string) {
    setHighlight(evidenceId);
    document
      .getElementById(`evidence-${evidenceId}`)
      ?.scrollIntoView({ behavior: 'smooth', block: 'center' });
  }

  if (incident.isLoading) {
    return <div className="mx-auto max-w-[1600px] px-6 py-7"><SkeletonRows rows={6} /></div>;
  }

  // "Aegis may be unreachable, or this incident may not exist" was the console
  // declining to say which, when it has the answer in its hands: a failed read
  // and an incident that is genuinely absent are different facts. They are
  // separated here for the same reason they are in every panel below.
  if (incident.isError) {
    return (
      <div className="mx-auto max-w-[1600px] px-6 py-7">
        <QueryFailure
          source="Incident"
          title="Cannot load this incident"
          error={incident.error}
          consequence="Whether this incident exists cannot be determined from here."
          onRetry={() => incident.refetch()}
        />
      </div>
    );
  }

  if (!incident.data) {
    return (
      <div className="mx-auto max-w-[1600px] px-6 py-7">
        <EmptyState
          title={`There is no incident ${id}.`}
          detail="Aegis answered, and has no record of it. The identifier may be mistyped, or belong to another environment."
        />
      </div>
    );
  }

  const inc = incident.data;
  const age = Date.now() - new Date(inc.created_at).getTime();
  const gaps = evidence.data?.counts.unavailable_sources ?? 0;
  const dx = diagnosis.data;

  return (
    <div className="mx-auto max-w-[1600px] px-6 py-6">
      <header className="mb-5">
        <div className="flex flex-wrap items-start justify-between gap-4">
          <div className="min-w-0">
            <div className="flex items-center gap-2.5">
              <SeverityBadge severity={inc.severity} />
              <span className="font-mono text-meta text-ink-tertiary">{inc.id}</span>
              <StateChip state={inc.state} />
              {live ? <WorkingIndicator label="investigating" /> : null}
            </div>
            <h1 className="mt-2 text-h2 font-medium tracking-tight">{inc.title}</h1>
            <p className="mt-1 text-body text-ink-secondary">
              {inc.affected_services.length
                ? inc.affected_services.join(' · ')
                : 'No service localised yet'}
              {' — '}
              {formatDuration(age)} old
            </p>
          </div>

          <div className="flex shrink-0 items-center gap-2">
            <button
              type="button"
              onClick={reinvestigate}
              className="inline-flex items-center gap-1.5 rounded-btn border border-line px-2.5 py-1.5
                         text-body text-ink-secondary transition-colors duration-hover
                         hover:bg-surface-3 hover:text-ink-primary"
            >
              <RefreshCw className="h-3.5 w-3.5" aria-hidden />
              Re-investigate
            </button>
            {/* An already-resolved incident has nothing to resolve; offering the
                control anyway would invite a transition the backend rejects. */}
            {inc.state !== 'RESOLVED' ? (
              <button
                type="button"
                onClick={() => setResolveOpen(true)}
                aria-haspopup="dialog"
                className="inline-flex items-center gap-1.5 rounded-btn border border-status-success/40
                           px-2.5 py-1.5 text-body text-status-success transition-colors duration-hover
                           hover:bg-status-success/10"
              >
                <CheckCircle2 className="h-3.5 w-3.5" aria-hidden />
                Resolve
              </button>
            ) : null}
          </div>
        </div>
      </header>

      {/* What we know: a structured summary, never a chat bubble (spec 15). */}
      <section className="card mb-4 p-4" aria-labelledby="what-we-know">
        <div className="grid gap-5 md:grid-cols-[1fr_240px]">
          <div>
            <h2 id="what-we-know" className="label-meta mb-2">What we know</h2>
            {diagnosis.isLoading ? (
              <SkeletonRows rows={2} />
            ) : diagnosis.isError ? (
              // Abstention is a first-class conclusion here, so a failed fetch
              // must never borrow its wording: "Aegis reached no conclusion" and
              // "we could not ask Aegis for one" are opposite facts.
              <QueryFailure
                source="Diagnosis"
                title="Cannot load the diagnosis"
                error={diagnosis.error}
                consequence="Whether Aegis has reached a conclusion is unknown — this is not an abstention."
                onRetry={() => diagnosis.refetch()}
              />
            ) : !dx ? (
              <p className="text-body text-ink-secondary">
                Aegis has not reached a conclusion yet. Evidence collection is
                {live ? ' still in progress.' : ' complete but inconclusive.'}
              </p>
            ) : dx.abstained ? (
              <div className="space-y-2">
                <p className="text-body text-status-warning">
                  Aegis cannot establish a root cause yet.
                </p>
                <p className="text-body text-ink-secondary">{dx.statement}</p>
                {dx.missing_evidence.length ? (
                  <div>
                    <p className="label-meta mb-1">Missing</p>
                    <ul className="space-y-0.5">
                      {dx.missing_evidence.map((m) => (
                        <li key={m} className="text-meta text-ink-secondary">— {m}</li>
                      ))}
                    </ul>
                  </div>
                ) : null}
              </div>
            ) : (
              <div className="space-y-2">
                <p className="text-body text-ink-primary">{dx.statement}</p>
                {dx.causal_path.length ? (
                  <p className="flex flex-wrap items-center gap-1.5 text-meta text-ink-secondary">
                    {dx.causal_path.map((node, i) => (
                      <span key={`${node}-${i}`} className="flex items-center gap-1.5">
                        {i > 0 ? <span className="text-ink-tertiary">→</span> : null}
                        <span
                          className={cn(
                            'rounded border px-1.5 py-0.5 font-mono',
                            i === dx.causal_path.length - 1
                              ? 'border-status-critical/40 text-status-critical'
                              : 'border-line',
                          )}
                        >
                          {node}
                        </span>
                      </span>
                    ))}
                    <span className="text-ink-tertiary">suspected origin</span>
                  </p>
                ) : null}
                {dx.supporting_evidence.length ? (
                  <p className="text-meta text-ink-tertiary">
                    Supported by {dx.supporting_evidence.length} evidence item
                    {dx.supporting_evidence.length === 1 ? '' : 's'}
                  </p>
                ) : null}
                {dx.rejected_alternatives.length ? (
                  <div>
                    <p className="label-meta mb-1">Ruled out</p>
                    <ul className="space-y-0.5">
                      {dx.rejected_alternatives.map((r) => (
                        <li key={r} className="text-meta text-ink-secondary">— {r}</li>
                      ))}
                    </ul>
                  </div>
                ) : null}
                {dx.uncertainty ? (
                  <p className="text-meta text-ink-tertiary">Uncertainty: {dx.uncertainty}</p>
                ) : null}
              </div>
            )}
          </div>

          <div className="space-y-3 md:border-l md:border-hairline md:pl-5">
            <ConfidenceMeter
              value={dx?.confidence ?? inc.confidence}
              explain={
                evidence.data
                  ? [
                      `${evidence.data.counts.usable} usable evidence items`,
                      `${evidence.data.counts.by_trust?.TIER_A ?? 0} direct machine observations`,
                      gaps ? `${gaps} source${gaps === 1 ? '' : 's'} unavailable` : '',
                    ].filter(Boolean)
                  : undefined
              }
            />
            {gaps > 0 ? (
              <p className="rounded border border-status-warning/30 bg-status-warning/5 px-2 py-1.5
                            text-meta text-status-warning">
                Confidence is reduced because {gaps} evidence source
                {gaps === 1 ? ' was' : 's were'} unavailable.
              </p>
            ) : null}
          </div>
        </div>
      </section>

      <div className="grid gap-4 xl:grid-cols-[1fr_360px]">
        <div className="min-w-0 space-y-4">
          <section aria-labelledby="hypotheses-heading">
            <h2 id="hypotheses-heading" className="mb-2.5 label-meta">Hypotheses</h2>
            {hypotheses.isLoading ? (
              <SkeletonRows rows={3} />
            ) : hypotheses.isError ? (
              // An empty stack renders "No hypotheses yet", which would read as
              // an investigation that found nothing worth proposing.
              <QueryFailure
                source="Hypotheses"
                title="Cannot load hypotheses"
                error={hypotheses.error}
                consequence="Aegis may have candidate causes that are not shown here."
                onRetry={() => hypotheses.refetch()}
              />
            ) : (
              <HypothesisStack
                hypotheses={hypotheses.data?.items ?? []}
                onCiteEvidence={jumpToEvidence}
              />
            )}
          </section>

          <section aria-labelledby="evidence-heading">
            <div className="mb-2.5 flex items-baseline justify-between">
              <h2 id="evidence-heading" className="label-meta">Evidence</h2>
              {evidence.data ? (
                <span className="text-meta text-ink-tertiary">
                  {evidence.data.counts.usable} usable
                  {gaps ? ` · ${gaps} unavailable` : ''}
                </span>
              ) : null}
            </div>
            {evidence.isLoading ? (
              <SkeletonRows rows={4} />
            ) : evidence.isError ? (
              <QueryFailure
                source="Evidence"
                title="Cannot load evidence"
                error={evidence.error}
                consequence="Absence of evidence below cannot be distinguished from evidence Aegis could not read."
                onRetry={() => evidence.refetch()}
              />
            ) : (
              <EvidenceList data={evidence.data!} highlightId={highlight} />
            )}
          </section>
        </div>

        <aside className="min-w-0">
          <section
            aria-labelledby="timeline-heading"
            className="card sticky top-4 flex h-[calc(100vh-7rem)] flex-col p-3.5"
          >
            <h2 id="timeline-heading" className="mb-2 label-meta">Timeline</h2>
            {timeline.isLoading ? (
              <SkeletonRows rows={6} />
            ) : timeline.isError ? (
              <QueryFailure
                source="Timeline"
                title="Cannot load the timeline"
                error={timeline.error}
                consequence="What Aegis did, and when, cannot be replayed from here."
                onRetry={() => timeline.refetch()}
              />
            ) : timeline.data ? (
              <Timeline data={timeline.data} />
            ) : (
              <p className="text-meta text-ink-tertiary">Timeline unavailable.</p>
            )}
          </section>
        </aside>
      </div>

      <ResolveDialog
        incident={inc}
        open={resolveOpen}
        onOpenChange={(open) => (open ? setResolveOpen(true) : closeResolveDialog())}
        reason={resolveReason}
        onReasonChange={setResolveReason}
        onConfirm={() => resolveIncident.mutate(resolveReason)}
        pending={resolveIncident.isPending}
      />
    </div>
  );
}
