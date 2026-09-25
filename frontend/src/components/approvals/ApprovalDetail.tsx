'use client';

import Link from 'next/link';
import { useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { toast } from 'sonner';
import { ArrowUpRight, FileQuestion, ShieldCheck, ThumbsDown } from 'lucide-react';
import { consoleApi } from '@/lib/console-api';
import type { ApprovalDecision, PendingApproval } from '@/lib/console-types';
import { ConfidenceMeter, SeverityBadge } from '@/components/ui/primitives';
import { ErrorState, Skeleton, WorkingIndicator } from '@/components/ui/states';
import { ApproveDialog } from '@/components/approvals/ApproveDialog';
import {
  ExpiryChip, RiskTierBadge, StructuredDetail, asSeverity, expiryOf,
} from '@/components/approvals/detail-primitives';
import { cn, formatClock, relativeTime } from '@/lib/utils';

/**
 * One approval, with everything needed to decide it.
 *
 * The rule this surface is built around: an operator must never have to go and
 * reconstruct the investigation. What will happen, why, on what evidence, what
 * it can break, how it is verified, how it is reversed and when the proposal
 * expires all live here (UX spec 39).
 *
 * The exact arguments come from the action record rather than the approval
 * summary, so if that fetch fails the page says so instead of rendering a
 * decision surface that is quietly missing the change itself.
 */
export function ApprovalDetail({ approval, now }: { approval: PendingApproval; now: number }) {
  const qc = useQueryClient();
  const [note, setNote] = useState('');
  const [confirmOpen, setConfirmOpen] = useState(false);

  const detail = useQuery({
    queryKey: ['action', approval.action_id],
    queryFn: () => consoleApi.getAction(approval.action_id),
  });

  const decide = useMutation({
    mutationFn: (decision: ApprovalDecision) =>
      consoleApi.decideApproval(approval.approval_id, decision, note),
    onSuccess: (result, decision) => {
      if (decision === 'approved') {
        if (result.work_enqueued) {
          toast.success('Approved — queued for execution.', {
            description: 'The worker re-runs every gate before the change is made.',
          });
        } else {
          toast.warning('Approved — recorded only.', {
            description:
              'No worker accepted the job, so nothing is scheduled to execute. Check worker health before assuming this ran.',
          });
        }
      } else if (decision === 'rejected') {
        toast.success('Rejected — recorded.', {
          description: 'This action will not execute. The incident stays open.',
        });
      } else {
        toast.success('More evidence requested — recorded.', {
          description: 'Aegis will keep investigating rather than acting on this proposal.',
        });
      }
      setConfirmOpen(false);
      setNote('');
      qc.invalidateQueries({ queryKey: ['approvals'] });
      qc.invalidateQueries({ queryKey: ['tasks'] });
      qc.invalidateQueries({ queryKey: ['action', approval.action_id] });
    },
    onError: (error: Error) => {
      toast.error('The decision was not recorded.', { description: error.message });
    },
  });

  const expiry = expiryOf(approval.expires_at, now);
  const lapsed = expiry.tone === 'lapsed';
  const severity = asSeverity(approval.incident.severity);
  const evidence = approval.supporting_evidence;
  const busy = decide.isPending;

  return (
    <article
      id={`approval-${approval.approval_id}`}
      className={cn(
        'card scroll-mt-6 p-5',
        lapsed && 'border-status-critical/40',
        expiry.tone === 'urgent' && 'border-status-critical/30',
      )}
      aria-labelledby={`approval-title-${approval.approval_id}`}
    >
      {/* ---------------------------------------------------------- header */}
      <header className="flex flex-wrap items-start justify-between gap-3 border-b border-hairline pb-4">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <RiskTierBadge tier={approval.action.risk_tier} />
            {severity ? <SeverityBadge severity={severity} /> : null}
            <ExpiryChip expiry={expiry} />
          </div>
          <h2
            id={`approval-title-${approval.approval_id}`}
            className="mt-2.5 break-words text-h3 font-bold tracking-tight text-ink-primary"
          >
            {approval.action.type} on {approval.action.resource_id}
          </h2>
          <p className="mt-1 text-body font-medium text-ink-secondary">
            {approval.action.environment} · requested {relativeTime(approval.requested_at)} · expires{' '}
            {formatClock(approval.expires_at)}
          </p>
        </div>
        <Link
          href={`/incidents/${approval.incident_id}`}
          className="inline-flex shrink-0 items-center gap-1.5 rounded-btn border border-line px-2.5 py-1.5
                     text-meta font-semibold text-ink-primary transition-colors duration-hover
                     hover:bg-surface-3"
        >
          Open incident
          <ArrowUpRight className="h-3.5 w-3.5" aria-hidden />
        </Link>
      </header>

      {lapsed ? (
        <p className="mt-4 rounded-btn border border-status-critical/40 bg-status-critical/5 p-3
                      text-body font-bold text-status-critical">
          This request has expired. A decision recorded now will be rejected by the API — the action
          must be proposed again.
        </p>
      ) : null}

      {/* ------------------------------------------------------- why / what */}
      <div className="mt-4 grid gap-5 lg:grid-cols-[minmax(0,1.6fr)_minmax(0,1fr)]">
        <div className="min-w-0 space-y-5">
          <section aria-labelledby={`why-${approval.approval_id}`}>
            <h3 id={`why-${approval.approval_id}`} className="label-meta mb-1.5 font-semibold">
              Why it will happen
            </h3>
            <p className="text-body font-semibold text-ink-primary">{approval.incident.title}</p>
            <p className="mt-1.5 break-words text-body font-medium text-ink-secondary">
              {approval.diagnosis.statement ?? (
                <span className="font-bold text-status-warning">
                  No diagnosis statement was recorded for this incident. Approving means acting
                  without a stated root cause.
                </span>
              )}
            </p>
            <p className="mt-1.5 break-words text-meta font-medium text-ink-tertiary">
              Proposal reason: {approval.action.reason || 'not recorded'}
            </p>
          </section>

          <section aria-labelledby={`change-${approval.approval_id}`}>
            <h3 id={`change-${approval.approval_id}`} className="label-meta mb-1.5 font-semibold">
              What will happen
            </h3>
            {detail.isLoading ? (
              <Skeleton className="h-16 w-full" />
            ) : detail.isError ? (
              <ErrorState
                title="Cannot load the action arguments"
                detail={(detail.error as Error).message}
                consequence="The exact change cannot be shown. Do not approve an action you cannot read."
                onRetry={() => detail.refetch()}
              />
            ) : (
              <StructuredDetail
                data={detail.data?.arguments}
                emptyLabel="This action takes no arguments."
              />
            )}
            {detail.data ? (
              <p className="mt-2 font-mono text-meta font-medium text-ink-tertiary">
                idempotency key {detail.data.idempotency_key}
              </p>
            ) : null}
          </section>

          <section aria-labelledby={`effect-${approval.approval_id}`}>
            <h3 id={`effect-${approval.approval_id}`} className="label-meta mb-1.5 font-semibold">
              Expected effect
            </h3>
            <StructuredDetail
              data={approval.expected_effect}
              emptyLabel="No expected effect was recorded, so there is no stated prediction to verify against."
              emptyTone="warning"
            />
          </section>

          <section aria-labelledby={`verify-${approval.approval_id}`}>
            <h3 id={`verify-${approval.approval_id}`} className="label-meta mb-1.5 font-semibold">
              How it will be verified
            </h3>
            <StructuredDetail
              data={approval.verification_plan}
              emptyLabel="No verification plan recorded. Success or failure will not be checked automatically."
              emptyTone="warning"
            />
          </section>
        </div>

        <div className="min-w-0 space-y-5">
          <section aria-labelledby={`confidence-${approval.approval_id}`}>
            <h3 id={`confidence-${approval.approval_id}`} className="label-meta mb-1.5 font-semibold">
              Diagnosis confidence
            </h3>
            <ConfidenceMeter
              value={approval.diagnosis.confidence}
              explain={[
                `${evidence.length} supporting evidence reference${evidence.length === 1 ? '' : 's'}`,
                approval.incident.confidence !== null
                  ? `incident confidence ${Math.round(approval.incident.confidence * 100)}%`
                  : 'incident confidence not recorded',
              ]}
            />
          </section>

          <section aria-labelledby={`blast-${approval.approval_id}`}>
            <h3 id={`blast-${approval.approval_id}`} className="label-meta mb-1.5 font-semibold">
              Blast radius
            </h3>
            <StructuredDetail
              data={approval.blast_radius}
              emptyLabel="No blast radius was computed. The scope of this change is unknown."
              emptyTone="critical"
            />
          </section>

          <section aria-labelledby={`rollback-${approval.approval_id}`}>
            <h3 id={`rollback-${approval.approval_id}`} className="label-meta mb-1.5 font-semibold">
              Rollback plan
            </h3>
            <StructuredDetail
              data={approval.rollback_plan}
              emptyLabel="No rollback plan recorded. This change cannot be reversed automatically."
              emptyTone="critical"
            />
          </section>

          <section aria-labelledby={`evidence-${approval.approval_id}`}>
            <h3 id={`evidence-${approval.approval_id}`} className="label-meta mb-1.5 font-semibold">
              Supporting evidence
            </h3>
            {evidence.length === 0 ? (
              <p className="text-body font-bold text-status-critical">
                No evidence references. A proposal with no evidence behind it should not be
                approved.
              </p>
            ) : (
              <ul className="flex flex-wrap gap-1.5">
                {evidence.map((id) => (
                  <li
                    key={id}
                    className="rounded border border-hairline bg-surface-2 px-1.5 py-0.5
                               font-mono text-meta font-semibold text-ink-secondary"
                  >
                    {id}
                  </li>
                ))}
              </ul>
            )}
            <Link
              href={`/incidents/${approval.incident_id}`}
              className="mt-2 inline-block text-meta font-semibold text-accent
                         transition-opacity duration-hover hover:opacity-80"
            >
              Inspect this evidence on the incident
            </Link>
          </section>

          {detail.data && detail.data.policy_decisions.length > 0 ? (
            <section aria-labelledby={`policy-${approval.approval_id}`}>
              <h3 id={`policy-${approval.approval_id}`} className="label-meta mb-1.5 font-semibold">
                Why a human was asked
              </h3>
              <ul className="space-y-2">
                {detail.data.policy_decisions.slice(0, 2).map((decision, index) => (
                  <li
                    key={`${decision.policy_version}-${index}`}
                    className="rounded-btn border border-hairline bg-surface-2 p-2.5"
                  >
                    <p className="text-body font-bold text-ink-primary">{decision.effect}</p>
                    <p className="mt-0.5 text-meta font-medium text-ink-secondary">
                      matched {decision.matched_rule} · policy {decision.policy_version}
                    </p>
                    {decision.reasons.length > 0 ? (
                      <ul className="mt-1 space-y-0.5">
                        {decision.reasons.map((reason) => (
                          <li key={reason} className="text-meta font-medium text-ink-tertiary">
                            {reason}
                          </li>
                        ))}
                      </ul>
                    ) : null}
                    {decision.gates.length > 0 ? (
                      <ul className="mt-1.5 flex flex-wrap gap-1.5">
                        {decision.gates.map((gate) => (
                          <li
                            key={gate.gate}
                            className={cn(
                              'rounded border px-1.5 py-0.5 text-meta font-semibold uppercase tracking-wider',
                              gate.passed
                                ? 'border-status-success/40 text-status-success'
                                : 'border-status-critical/40 text-status-critical',
                            )}
                            title={gate.reason ?? undefined}
                          >
                            {gate.gate} {gate.passed ? 'pass' : 'fail'}
                          </li>
                        ))}
                      </ul>
                    ) : null}
                  </li>
                ))}
              </ul>
            </section>
          ) : null}
        </div>
      </div>

      {/* -------------------------------------------------------- decision */}
      <footer className="mt-5 border-t border-hairline pt-4">
        <label
          htmlFor={`note-${approval.approval_id}`}
          className="label-meta block font-semibold"
        >
          Decision note (recorded in the audit trail)
        </label>
        <textarea
          id={`note-${approval.approval_id}`}
          value={note}
          onChange={(event) => setNote(event.target.value)}
          rows={2}
          maxLength={2000}
          placeholder="Why you are deciding this way. Optional for approval, expected for a rejection."
          className="mt-1.5 w-full rounded-btn border border-hairline bg-surface-2 p-2.5
                     text-body font-medium text-ink-primary placeholder:text-ink-tertiary"
        />

        <div className="mt-3 flex flex-wrap items-center justify-between gap-3">
          <span aria-live="polite" className="min-h-[1rem]">
            {busy ? <WorkingIndicator label="Recording decision…" /> : null}
          </span>

          <div className="flex flex-wrap items-center gap-2">
            <button
              type="button"
              onClick={() => decide.mutate('more_evidence')}
              disabled={busy || lapsed}
              className="inline-flex items-center gap-1.5 rounded-btn border border-line px-3 py-2
                         text-body font-semibold text-ink-secondary transition-colors duration-hover
                         hover:bg-surface-3 disabled:opacity-40"
            >
              <FileQuestion className="h-4 w-4" aria-hidden />
              Request more evidence
            </button>
            <button
              type="button"
              onClick={() => decide.mutate('rejected')}
              disabled={busy || lapsed}
              className="inline-flex items-center gap-1.5 rounded-btn border border-status-critical/40
                         px-3 py-2 text-body font-semibold text-status-critical transition-colors
                         duration-hover hover:bg-status-critical/10 disabled:opacity-40"
            >
              <ThumbsDown className="h-4 w-4" aria-hidden />
              Reject
            </button>
            <button
              type="button"
              onClick={() => setConfirmOpen(true)}
              disabled={busy || lapsed}
              className="inline-flex items-center gap-1.5 rounded-btn border border-status-success/50
                         bg-status-success/10 px-3 py-2 text-body font-bold text-status-success
                         transition-colors duration-hover hover:bg-status-success/20 disabled:opacity-40"
            >
              <ShieldCheck className="h-4 w-4" aria-hidden />
              Approve {approval.action.type}
            </button>
          </div>
        </div>
      </footer>

      <ApproveDialog
        approval={approval}
        open={confirmOpen}
        onOpenChange={setConfirmOpen}
        onConfirm={() => decide.mutate('approved')}
        pending={busy}
        note={note}
      />
    </article>
  );
}
