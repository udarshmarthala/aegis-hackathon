'use client';

import { useState } from 'react';
import * as Dialog from '@radix-ui/react-dialog';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { toast } from 'sonner';
import { Power, ShieldAlert } from 'lucide-react';
import { ApiError, NetworkError, api } from '@/lib/api';
import {
  ErrorState, SkeletonRows, SourceUnavailableState, WorkingIndicator,
} from '@/components/ui/states';
import { cn } from '@/lib/utils';
import { ActionRegistryTable } from '@/components/incident/ActionRegistryTable';

/**
 * The scope vocabulary the API actually enforces (`_VALID_SCOPES` in
 * `backend/src/aegis/api/routers/policy_admin.py`). It is mirrored rather than
 * invented: a scope the backend does not know is rejected outright, and a
 * control that appears to engage protection but never does is worse than no
 * control at all.
 */
const SCOPES = ['global', 'environment', 'action_type', 'service'] as const;
type KillSwitchScope = (typeof SCOPES)[number];

const SCOPE_LABEL: Record<KillSwitchScope, string> = {
  global: 'Everything',
  environment: 'One environment',
  action_type: 'One action type',
  service: 'One service',
};

/**
 * The target is free text at the API (`target: str`, 256 chars), so the only
 * honest thing the UI can do is say what a mistyped target costs: a switch that
 * matches nothing blocks nothing, and it still looks engaged.
 */
const SCOPE_HINT: Record<KillSwitchScope, string> = {
  global: 'Applies to every environment, service and action type. No target.',
  environment: 'The environment name exactly as Aegis records it. A name that matches nothing blocks nothing.',
  action_type: 'An action type from the registry below. A value outside the registry blocks nothing.',
  service: 'The service name exactly as it appears in topology. A name that matches nothing blocks nothing.',
};

type KillSwitchMode = 'engage' | 'release';

interface KillSwitchIntent {
  mode: KillSwitchMode;
  scope: KillSwitchScope;
  target: string;
}

/** How a switch is named in audit records and in the release confirmation. */
function identityOf(scope: KillSwitchScope, target: string): string {
  return scope === 'global' ? 'global' : `${scope}:${target}`;
}

function phraseOf(scope: KillSwitchScope, target: string): string {
  return scope === 'global'
    ? 'the global kill switch'
    : `the ${SCOPE_LABEL[scope].toLowerCase()} kill switch for ${target || '…'}`;
}

/**
 * Governance: autonomy posture, the kill switch, and the real action boundary.
 *
 * The kill switch is large and isolated, and becomes visually urgent only when
 * engaged — a control that is permanently red stops being read (UX spec 40).
 *
 * Both directions are confirmed, and release is confirmed harder than engage:
 * engaging only stops Aegis acting, while releasing hands autonomous write
 * capability back to the platform. The asymmetry in the dialog is deliberate.
 */
export default function PoliciesPage() {
  const qc = useQueryClient();
  const policy = useQuery({ queryKey: ['policy'], queryFn: api.policy });
  // Shares the registry cache with ActionRegistryTable below, so offering the
  // real action vocabulary as target suggestions costs no extra request.
  const registry = useQuery({ queryKey: ['action-registry'], queryFn: api.actionRegistry });

  const [intent, setIntent] = useState<KillSwitchIntent | null>(null);

  const switches = policy.data?.kill_switches;
  // The global control reads `global`, not `any_engaged`: with a scoped switch
  // engaged the old code offered "Release kill switch" and then released a
  // global switch that was never on, leaving the real one untouched.
  const globalEngaged = switches?.global ?? false;
  const anyEngaged = switches?.any_engaged ?? false;

  const scoped: KillSwitchIntent[] = [
    ...(switches?.environments ?? []).map((target) => ({
      mode: 'release' as const, scope: 'environment' as const, target,
    })),
    ...(switches?.services ?? []).map((target) => ({
      mode: 'release' as const, scope: 'service' as const, target,
    })),
    ...(switches?.action_types ?? []).map((target) => ({
      mode: 'release' as const, scope: 'action_type' as const, target,
    })),
  ];

  const actionTypes = (registry.data?.items ?? [])
    .map((item) => item.action_type)
    .filter((value): value is string => typeof value === 'string');

  const change = useMutation({
    mutationFn: (input: KillSwitchIntent & { reason: string }) =>
      input.mode === 'engage'
        ? api.engageKillSwitch(input.scope, input.target, input.reason)
        : api.releaseKillSwitch(input.scope, input.target),
    onSuccess: (_result, input) => {
      const name = identityOf(input.scope, input.target);
      if (input.mode === 'engage') {
        toast.success(`Kill switch engaged: ${name}.`, {
          description: 'No autonomous write action in scope can execute, whatever its tier or approval.',
        });
      } else {
        toast.warning(`Kill switch released: ${name}.`, {
          description: 'Autonomous action in this scope can execute again, bound only by policy.',
        });
      }
      setIntent(null);
      qc.invalidateQueries({ queryKey: ['policy'] });
    },
    // The dialog stays open and renders the failure; a kill-switch change that
    // did not happen must never disappear quietly behind a closing modal.
    onError: (error: Error) => {
      toast.error('The kill switch was not changed.', { description: error.message });
    },
  });

  function openIntent(next: KillSwitchIntent) {
    change.reset();
    setIntent(next);
  }

  if (policy.isError) {
    return (
      <div className="mx-auto max-w-[1200px] px-6 py-7">
        <ErrorState
          title="Cannot load policy"
          detail={(policy.error as Error).message}
          consequence="Autonomy posture is unknown. Aegis fails closed when it cannot read policy."
          onRetry={() => policy.refetch()}
        />
      </div>
    );
  }

  return (
    <div className="mx-auto max-w-[1200px] px-6 py-7">
      <header className="mb-6">
        <h1 className="text-h2 font-medium tracking-tight">Governance</h1>
        <p className="mt-1 text-body text-ink-secondary">
          Autonomy posture, safety controls and the complete action boundary.
        </p>
      </header>

      <section
        aria-labelledby="kill-switch"
        className={cn(
          'card mb-5 p-5 transition-colors',
          globalEngaged && 'border-status-critical/50 bg-status-critical/5',
        )}
      >
        <div className="flex flex-wrap items-center justify-between gap-4">
          <div>
            <h2
              id="kill-switch"
              className={cn(
                'flex items-center gap-2 text-body font-medium',
                globalEngaged && 'text-status-critical',
              )}
            >
              {globalEngaged ? <ShieldAlert className="h-4 w-4" aria-hidden /> : null}
              Global kill switch
            </h2>
            <p className="mt-1 max-w-xl text-meta text-ink-secondary">
              {globalEngaged
                ? 'Engaged. Every autonomous write action is blocked regardless of tier, confidence or approval. Investigation and evidence collection continue normally.'
                : 'Not engaged. Autonomous actions remain bound by tier limits, evidence quality, blast radius and rate limits.'}
            </p>
            {switches?.degraded ? (
              <p className="mt-1.5 text-meta text-status-warning">
                Policy store unreadable — Aegis is failing closed and treating all switches as engaged.
              </p>
            ) : null}
          </div>

          <button
            type="button"
            onClick={() =>
              openIntent({ mode: globalEngaged ? 'release' : 'engage', scope: 'global', target: '' })
            }
            className={cn(
              'inline-flex shrink-0 items-center gap-2 rounded-btn border px-3.5 py-2',
              'text-body transition-colors duration-hover',
              globalEngaged
                ? 'border-line text-ink-secondary hover:bg-surface-3'
                : 'border-status-critical/50 text-status-critical hover:bg-status-critical/10',
            )}
          >
            <Power className="h-4 w-4" aria-hidden />
            {globalEngaged ? 'Release kill switch' : 'Engage kill switch'}
          </button>
        </div>
      </section>

      <section aria-labelledby="scoped-switches" className="card mb-5 p-5">
        <div className="flex flex-wrap items-start justify-between gap-4">
          <div>
            <h2 id="scoped-switches" className="text-body font-medium">Scoped kill switches</h2>
            <p className="mt-1 max-w-xl text-meta text-ink-secondary">
              A narrower halt: one environment, one service or one action type, while everything
              else keeps following policy.
            </p>
          </div>
          <button
            type="button"
            onClick={() => openIntent({ mode: 'engage', scope: 'environment', target: '' })}
            className="inline-flex shrink-0 items-center gap-2 rounded-btn border border-line px-3
                       py-1.5 text-meta text-ink-secondary transition-colors duration-hover
                       hover:bg-surface-3"
          >
            <Power className="h-3.5 w-3.5" aria-hidden />
            Engage a scoped switch
          </button>
        </div>

        {scoped.length === 0 ? (
          <p className="mt-3 text-meta text-ink-tertiary">
            None engaged. No environment, service or action type is halted beyond what policy decides.
          </p>
        ) : (
          <ul className="mt-3 space-y-2">
            {scoped.map((item) => (
              <li
                key={identityOf(item.scope, item.target)}
                className="flex flex-wrap items-center justify-between gap-3 rounded-btn border
                           border-status-critical/30 bg-status-critical/5 px-3 py-2"
              >
                <span className="font-mono text-meta text-status-critical">
                  {identityOf(item.scope, item.target)}
                </span>
                <button
                  type="button"
                  onClick={() => openIntent(item)}
                  className="rounded-btn border border-line px-2.5 py-1 text-meta text-ink-secondary
                             transition-colors duration-hover hover:bg-surface-3"
                >
                  Release {identityOf(item.scope, item.target)}
                </button>
              </li>
            ))}
          </ul>
        )}
      </section>

      <div className="mb-5 grid gap-3 sm:grid-cols-3">
        <Stat
          label="Autonomy"
          value={
            globalEngaged
              ? 'Halted'
              : anyEngaged
                ? 'Partly halted'
                : policy.data?.autonomy.enabled
                  ? policy.data.autonomy.mode
                  : 'Observe only'
          }
          tone={anyEngaged ? 'critical' : undefined}
        />
        <Stat label="Allowed tiers" value={policy.data?.autonomy.allowed_tiers.join(', ') || 'none'} />
        <Stat
          label="Rate limit"
          value={`${policy.data?.autonomy.max_actions_per_hour ?? '—'} / hour`}
        />
      </div>

      {policy.isLoading ? <SkeletonRows rows={5} /> : <ActionRegistryTable />}

      {intent ? (
        <KillSwitchDialog
          // Remounting per intent clears the reason and the typed confirmation,
          // so text written for one switch can never be submitted against another.
          key={`${intent.mode}:${intent.scope}:${intent.target}`}
          intent={intent}
          actionTypes={actionTypes}
          pending={change.isPending}
          error={change.error}
          onDismiss={() => setIntent(null)}
          onConfirm={(scope, target, reason) =>
            change.mutate({ mode: intent.mode, scope, target, reason })
          }
        />
      ) : null}
    </div>
  );
}

/**
 * Confirmation for a kill-switch change.
 *
 * Same idiom as ApproveDialog: a Radix dialog that states the consequence in
 * plain words and whose confirm button names that consequence rather than
 * saying "Confirm" (UX spec 39). Two things are added because this control is
 * platform-wide rather than per-action: the audit reason is typed by the
 * operator instead of being a constant, and release additionally requires the
 * switch identity to be typed out, because release is the direction that
 * restores autonomous write capability.
 */
function KillSwitchDialog({
  intent,
  actionTypes,
  pending,
  error,
  onDismiss,
  onConfirm,
}: {
  intent: KillSwitchIntent;
  actionTypes: string[];
  pending: boolean;
  error: Error | null;
  onDismiss: () => void;
  onConfirm: (scope: KillSwitchScope, target: string, reason: string) => void;
}) {
  const releasing = intent.mode === 'release';
  const [scope, setScope] = useState<KillSwitchScope>(intent.scope);
  const [target, setTarget] = useState(intent.target);
  const [reason, setReason] = useState('');
  const [typed, setTyped] = useState('');

  const trimmedTarget = target.trim();
  const identity = identityOf(scope, trimmedTarget);
  const consequence = `${releasing ? 'Release' : 'Engage'} ${phraseOf(scope, trimmedTarget)}`;

  const targetOk = scope === 'global' || trimmedTarget.length > 0;
  const identityTyped = !releasing || typed.trim() === identity;
  const canSubmit = !pending && reason.trim().length > 0 && targetOk && identityTyped;

  return (
    <Dialog.Root
      open
      onOpenChange={(next) => {
        // Escape and overlay clicks still dismiss, but not mid-flight: the
        // request is already with the API and its outcome must be shown.
        if (!next && !pending) onDismiss();
      }}
    >
      <Dialog.Portal>
        <Dialog.Overlay className="fixed inset-0 z-40 bg-canvas/85 backdrop-blur-[2px]" />
        <Dialog.Content
          className="card fixed left-1/2 top-1/2 z-50 max-h-[86vh] w-[min(620px,94vw)]
                     -translate-x-1/2 -translate-y-1/2 overflow-auto bg-surface-1 p-5
                     shadow-2xl focus:outline-none"
        >
          <div className="flex items-start gap-3">
            <ShieldAlert
              className={cn(
                'mt-0.5 h-5 w-5 shrink-0',
                releasing ? 'text-status-critical' : 'text-status-warning',
              )}
              aria-hidden
            />
            <div className="min-w-0">
              <Dialog.Title className="text-h3 font-bold tracking-tight text-ink-primary">
                {consequence}
              </Dialog.Title>
              <Dialog.Description className="mt-1 text-body font-medium text-ink-secondary">
                {releasing
                  ? scope === 'global'
                    ? 'Aegis may execute write actions across the whole platform again without asking a human, within tier, confidence and rate limits, from the moment you confirm.'
                    : 'Aegis may execute write actions in this scope again without asking a human, within tier, confidence and rate limits, from the moment you confirm.'
                  : 'Every autonomous write action in scope stops, whatever its risk tier, its confidence or an approval already granted. Investigation, evidence collection and human-run actions continue.'}
              </Dialog.Description>
            </div>
          </div>

          <form
            className="mt-4 space-y-4"
            onSubmit={(event) => {
              event.preventDefault();
              if (canSubmit) onConfirm(scope, trimmedTarget, reason.trim());
            }}
          >
            <section className="space-y-3 rounded-btn border border-hairline bg-surface-2 p-3">
              <h3 className="label-meta font-semibold">What this covers</h3>

              {releasing ? (
                <p className="font-mono text-body font-semibold text-ink-primary">{identity}</p>
              ) : (
                <>
                  <div>
                    <label htmlFor="ks-scope" className="label-meta block font-semibold">
                      Scope
                    </label>
                    <select
                      id="ks-scope"
                      value={scope}
                      onChange={(event) => {
                        setScope(event.target.value as KillSwitchScope);
                        setTarget('');
                      }}
                      className="mt-1 w-full rounded-btn border border-hairline bg-surface-1 p-2
                                 text-body font-medium text-ink-primary"
                    >
                      {SCOPES.map((value) => (
                        <option key={value} value={value}>
                          {SCOPE_LABEL[value]} ({value})
                        </option>
                      ))}
                    </select>
                    <p className="mt-1 text-meta text-ink-tertiary">{SCOPE_HINT[scope]}</p>
                  </div>

                  {scope === 'global' ? null : (
                    <div>
                      <label htmlFor="ks-target" className="label-meta block font-semibold">
                        Target
                      </label>
                      <input
                        id="ks-target"
                        value={target}
                        onChange={(event) => setTarget(event.target.value)}
                        maxLength={256}
                        list={scope === 'action_type' ? 'ks-target-options' : undefined}
                        autoComplete="off"
                        className="mt-1 w-full rounded-btn border border-hairline bg-surface-1 p-2
                                   font-mono text-body font-medium text-ink-primary"
                      />
                      {scope === 'action_type' ? (
                        <datalist id="ks-target-options">
                          {actionTypes.map((value) => (
                            <option key={value} value={value} />
                          ))}
                        </datalist>
                      ) : null}
                    </div>
                  )}
                </>
              )}
            </section>

            <div>
              <label htmlFor="ks-reason" className="label-meta block font-semibold">
                {releasing
                  ? 'Why you are releasing it (required)'
                  : 'Why you are engaging it (recorded in the audit trail)'}
              </label>
              <textarea
                id="ks-reason"
                value={reason}
                onChange={(event) => setReason(event.target.value)}
                rows={3}
                maxLength={500}
                placeholder="The reason a reader of the audit log a month from now would need."
                className="mt-1.5 w-full rounded-btn border border-hairline bg-surface-2 p-2.5
                           text-body font-medium text-ink-primary placeholder:text-ink-tertiary"
              />
              <p className="mt-1 text-meta text-ink-tertiary">
                {releasing
                  ? 'The release endpoint records who released the switch, not why, so this reason is not transmitted — state it here deliberately and record it on the incident as well.'
                  : 'Stored against your account on the kill switch and in the audit log.'}
              </p>
            </div>

            {releasing ? (
              <div>
                <label htmlFor="ks-confirm" className="label-meta block font-semibold">
                  Type <span className="font-mono">{identity}</span> to confirm
                </label>
                <input
                  id="ks-confirm"
                  value={typed}
                  onChange={(event) => setTyped(event.target.value)}
                  autoComplete="off"
                  className="mt-1.5 w-full rounded-btn border border-hairline bg-surface-2 p-2.5
                             font-mono text-body font-medium text-ink-primary"
                />
              </div>
            ) : null}

            {error instanceof NetworkError ? (
              <SourceUnavailableState
                source="Aegis API"
                reason={error.message}
                consequence="Nothing changed. The posture on this page is the last one Aegis could read, not necessarily the current one."
              />
            ) : error ? (
              <ErrorState
                title={
                  error instanceof ApiError
                    ? `Aegis refused the change (${error.status} ${error.code})`
                    : 'The change was not applied'
                }
                detail={error.message}
                consequence={
                  releasing
                    ? 'The kill switch is still engaged. Autonomous action remains halted.'
                    : 'The kill switch is not engaged. Autonomous action is still possible.'
                }
              />
            ) : null}

            <div className="flex flex-wrap items-center justify-end gap-2">
              <span aria-live="polite">
                {pending ? <WorkingIndicator label="Applying change…" /> : null}
              </span>
              <Dialog.Close asChild>
                <button
                  type="button"
                  disabled={pending}
                  className="rounded-btn border border-line px-3 py-2 text-body font-semibold
                             text-ink-secondary transition-colors duration-hover hover:bg-surface-3
                             disabled:opacity-50"
                >
                  Cancel
                </button>
              </Dialog.Close>
              <button
                type="submit"
                disabled={!canSubmit}
                className={cn(
                  'rounded-btn border px-3 py-2 text-body font-bold transition-colors duration-hover',
                  'disabled:opacity-50',
                  releasing
                    ? 'border-status-critical/50 bg-status-critical/10 text-status-critical hover:bg-status-critical/20'
                    : 'border-status-warning/50 bg-status-warning/10 text-status-warning hover:bg-status-warning/20',
                )}
              >
                {consequence}
              </button>
            </div>
          </form>
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}

function Stat({ label, value, tone }: { label: string; value: string; tone?: 'critical' }) {
  return (
    <div className="card p-3.5">
      <p className="label-meta">{label}</p>
      <p className={cn('mt-1 text-body', tone === 'critical' ? 'text-status-critical' : 'text-ink-primary')}>
        {value}
      </p>
    </div>
  );
}
