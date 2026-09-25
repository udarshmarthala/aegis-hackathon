'use client';

import * as Dialog from '@radix-ui/react-dialog';
import { ShieldAlert } from 'lucide-react';
import { RiskTierBadge } from '@/components/approvals/detail-primitives';
import { WorkingIndicator } from '@/components/ui/states';
import type { PendingApprovalView } from '@/lib/war-room/types';
import { Kbd } from './primitives';

/**
 * The approval request, raised by an `approval_required` event.
 *
 * Built on the same Radix dialog and risk-tier badge as the console's
 * `ApproveDialog`, and decided through the same `/v1/approvals/{id}/decide`
 * call, so the war room grants nothing the approvals page would not. The
 * confidence shown is the orchestrator's derived value from the cited evidence;
 * the model's own claim never reaches this screen.
 */
export function ApprovalModal({
  approval,
  open,
  pending,
  onApprove,
  onDeny,
  onDismiss,
}: {
  approval: PendingApprovalView | null;
  open: boolean;
  pending: boolean;
  onApprove: () => void;
  onDeny: () => void;
  onDismiss: () => void;
}) {
  if (!approval) return null;
  const confidence = approval.confidence === null ? 'not derived' : `${Math.round(approval.confidence * 100)}%`;
  return (
    <Dialog.Root open={open} onOpenChange={(next) => (next ? undefined : onDismiss())}>
      <Dialog.Portal>
        <Dialog.Overlay className="fixed inset-0 z-40 bg-canvas/85 backdrop-blur-[2px]" />
        <Dialog.Content
          className="card fixed left-1/2 top-1/2 z-50 max-h-[88vh] w-[min(820px,94vw)] -translate-x-1/2
                     -translate-y-1/2 overflow-auto border-2 border-status-warning/60 bg-surface-1 p-6
                     shadow-2xl focus:outline-none"
        >
          <div className="flex items-start gap-3">
            <ShieldAlert className="mt-1 h-7 w-7 shrink-0 text-status-warning" aria-hidden />
            <div className="min-w-0">
              <Dialog.Title className="text-[1.6rem] font-bold leading-tight tracking-tight">
                Approve {approval.action_type} on {approval.target}?
              </Dialog.Title>
              <Dialog.Description className="mt-1 text-[0.95rem] text-ink-secondary">
                Approval is permission, not execution. The worker re-runs the full gate chain - policy,
                authorisation, lease, verification - before anything changes.
              </Dialog.Description>
            </div>
          </div>

          <dl className="mt-5 grid grid-cols-2 gap-4 text-[1rem]">
            <div>
              <dt className="label-meta font-semibold">Action</dt>
              <dd className="mt-0.5 font-mono text-[1.15rem] font-bold">{approval.action_type}</dd>
            </div>
            <div>
              <dt className="label-meta font-semibold">Target</dt>
              <dd className="mt-0.5 break-words font-mono text-[1.15rem] font-bold">{approval.target}</dd>
            </div>
            <div>
              <dt className="label-meta font-semibold">Derived confidence</dt>
              <dd className="tnum mt-0.5 text-[1.15rem] font-bold">{confidence}</dd>
            </div>
            <div>
              <dt className="label-meta font-semibold">Risk tier</dt>
              <dd className="mt-0.5">
                {approval.risk_tier === null ? (
                  <span className="font-bold text-status-warning">unclassified</span>
                ) : (
                  <RiskTierBadge tier={approval.risk_tier} />
                )}
              </dd>
            </div>
            <div className="col-span-2">
              <dt className="label-meta font-semibold">Reason</dt>
              <dd className="mt-0.5 whitespace-pre-wrap text-[1rem] text-ink-primary">
                {approval.reason || 'No reason recorded.'}
              </dd>
            </div>
            <div className="col-span-2">
              <dt className="label-meta font-semibold">Cited evidence</dt>
              <dd className="mt-1 flex flex-wrap gap-1.5">
                {approval.evidence_ids.length ? (
                  approval.evidence_ids.map((id) => (
                    <span key={id} className="rounded border border-edge bg-surface-3 px-1.5 py-0.5 font-mono text-[0.85rem] font-semibold">
                      {id}
                    </span>
                  ))
                ) : (
                  <span className="font-bold text-status-critical">No evidence cited.</span>
                )}
              </dd>
            </div>
          </dl>

          <div className="mt-6 flex flex-wrap items-center justify-end gap-3">
            <span aria-live="polite">{pending ? <WorkingIndicator label="Recording decision…" /> : null}</span>
            <button
              type="button"
              onClick={onDeny}
              disabled={pending}
              aria-keyshortcuts="D"
              className="rounded-btn border border-line px-4 py-2.5 text-[1rem] font-bold text-ink-primary
                         transition-colors duration-hover hover:bg-surface-3 disabled:opacity-50"
            >
              Deny <Kbd>D</Kbd>
            </button>
            <button
              type="button"
              onClick={onApprove}
              disabled={pending}
              aria-keyshortcuts="A"
              className="rounded-btn border border-status-warning/60 bg-status-warning/15 px-4 py-2.5 text-[1rem]
                         font-bold text-status-warning transition-colors duration-hover hover:bg-status-warning/25
                         disabled:opacity-50"
            >
              Approve {approval.action_type} <Kbd>A</Kbd>
            </button>
          </div>
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}
