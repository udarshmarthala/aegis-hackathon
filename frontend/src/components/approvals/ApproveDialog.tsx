'use client';

import * as Dialog from '@radix-ui/react-dialog';
import { ShieldAlert } from 'lucide-react';
import type { PendingApproval } from '@/lib/console-types';
import { WorkingIndicator } from '@/components/ui/states';
import { RiskTierBadge, StructuredDetail } from '@/components/approvals/detail-primitives';

/**
 * Confirmation for an approval.
 *
 * The modal exists to make the consequence unavoidable, not to add a click. It
 * restates the exact change, what it can affect and how it is reversed, and the
 * confirm button names the actual consequence rather than saying "Confirm"
 * (UX spec 39).
 *
 * Only approval is gated this way. Rejecting is reversible in the sense that
 * matters - nothing happens - so it does not need a second wall.
 */
export function ApproveDialog({
  approval,
  open,
  onOpenChange,
  onConfirm,
  pending,
  note,
}: {
  approval: PendingApproval;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onConfirm: () => void;
  pending: boolean;
  note: string;
}) {
  const consequence = `Approve ${approval.action.type} on ${approval.action.resource_id}`;

  return (
    <Dialog.Root open={open} onOpenChange={onOpenChange}>
      <Dialog.Portal>
        <Dialog.Overlay className="fixed inset-0 z-40 bg-canvas/85 backdrop-blur-[2px]" />
        <Dialog.Content
          className="card fixed left-1/2 top-1/2 z-50 max-h-[86vh] w-[min(680px,94vw)]
                     -translate-x-1/2 -translate-y-1/2 overflow-auto bg-surface-1 p-5
                     shadow-2xl focus:outline-none"
        >
          <div className="flex items-start gap-3">
            <ShieldAlert className="mt-0.5 h-5 w-5 shrink-0 text-status-warning" aria-hidden />
            <div className="min-w-0">
              <Dialog.Title className="text-h3 font-bold tracking-tight text-ink-primary">
                {consequence}
              </Dialog.Title>
              <Dialog.Description className="mt-1 text-body font-medium text-ink-secondary">
                Approval is permission, not execution. The worker re-runs the full gate chain —
                policy, authorisation, lease, verification — before anything changes.
              </Dialog.Description>
            </div>
          </div>

          <div className="mt-4 space-y-4">
            <section className="rounded-btn border border-hairline bg-surface-2 p-3">
              <h3 className="label-meta mb-2 font-semibold">The exact change</h3>
              <dl className="grid gap-2 sm:grid-cols-2">
                <div>
                  <dt className="label-meta font-semibold">Action</dt>
                  <dd className="mt-0.5 font-mono text-body font-semibold text-ink-primary">
                    {approval.action.type}
                  </dd>
                </div>
                <div>
                  <dt className="label-meta font-semibold">Target</dt>
                  <dd className="mt-0.5 break-words font-mono text-body font-semibold text-ink-primary">
                    {approval.action.resource_type}:{approval.action.resource_id}
                  </dd>
                </div>
                <div>
                  <dt className="label-meta font-semibold">Environment</dt>
                  <dd className="mt-0.5 text-body font-semibold text-ink-primary">
                    {approval.action.environment}
                  </dd>
                </div>
                <div>
                  <dt className="label-meta font-semibold">Risk tier</dt>
                  <dd className="mt-0.5">
                    <RiskTierBadge tier={approval.action.risk_tier} size="sm" />
                  </dd>
                </div>
              </dl>
            </section>

            <section>
              <h3 className="label-meta mb-1.5 font-semibold">What this can affect</h3>
              <StructuredDetail
                data={approval.blast_radius}
                emptyLabel="No blast radius was computed. The scope of this change is unknown."
                emptyTone="critical"
              />
            </section>

            <section>
              <h3 className="label-meta mb-1.5 font-semibold">How it is reversed</h3>
              <StructuredDetail
                data={approval.rollback_plan}
                emptyLabel="No rollback plan recorded. If this change goes wrong it cannot be reversed automatically."
                emptyTone="critical"
              />
            </section>

            {note.trim() ? (
              <section>
                <h3 className="label-meta mb-1.5 font-semibold">Your note, recorded in the audit trail</h3>
                <p className="whitespace-pre-wrap break-words text-body font-medium text-ink-secondary">
                  {note}
                </p>
              </section>
            ) : null}
          </div>

          <div className="mt-5 flex flex-wrap items-center justify-end gap-2">
            <span aria-live="polite">
              {pending ? <WorkingIndicator label="Recording decision…" /> : null}
            </span>
            <Dialog.Close asChild>
              <button
                type="button"
                className="rounded-btn border border-line px-3 py-2 text-body font-semibold
                           text-ink-secondary transition-colors duration-hover hover:bg-surface-3"
              >
                Cancel
              </button>
            </Dialog.Close>
            <button
              type="button"
              onClick={onConfirm}
              disabled={pending}
              className="rounded-btn border border-status-warning/50 bg-status-warning/10 px-3 py-2
                         text-body font-bold text-status-warning transition-colors duration-hover
                         hover:bg-status-warning/20 disabled:opacity-50"
            >
              {consequence}
            </button>
          </div>
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}
