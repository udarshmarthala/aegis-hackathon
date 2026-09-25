'use client';

import { ShieldAlert, ShieldCheck } from 'lucide-react';
import { cn, formatDuration } from '@/lib/utils';
import type { AutonomyPosture } from '@/lib/console-types';

/**
 * Autonomy posture, read-only.
 *
 * `degraded` is given its own line because it is the most important word on
 * this page: it means the policy store could not be read and Aegis is failing
 * closed. An operator seeing "kill switch engaged" without that context would
 * go looking for the person who engaged it.
 */
export function AutonomyPanel({ posture }: { posture: AutonomyPosture }) {
  const kill = posture.kill_switch;
  const engaged = kill.any_engaged;

  return (
    <div className="space-y-4">
      <div
        className={cn(
          'rounded-card border p-4',
          kill.degraded
            ? 'border-status-warning/50 bg-status-warning/5'
            : engaged
              ? 'border-status-critical/50 bg-status-critical/5'
              : 'border-hairline bg-surface-2',
        )}
      >
        <div className="flex items-start gap-3">
          {engaged ? (
            <ShieldAlert
              className={cn(
                'mt-0.5 h-4 w-4 shrink-0',
                kill.degraded ? 'text-status-warning' : 'text-status-critical',
              )}
              aria-hidden
            />
          ) : (
            <ShieldCheck className="mt-0.5 h-4 w-4 shrink-0 text-status-success" aria-hidden />
          )}
          <div className="space-y-1.5">
            <p
              className={cn(
                'text-body font-semibold',
                kill.degraded
                  ? 'text-status-warning'
                  : engaged
                    ? 'text-status-critical'
                    : 'text-ink-primary',
              )}
            >
              {kill.degraded
                ? 'Kill switch degraded — failing closed'
                : engaged
                  ? 'Kill switch engaged'
                  : 'Kill switch clear'}
            </p>
            <p className="max-w-3xl text-meta font-medium text-ink-secondary">
              {kill.degraded
                ? 'The policy store could not be read, so Aegis is treating every switch as engaged. No autonomous write will execute until the store answers again. Nobody engaged this — it is the fail-closed default.'
                : engaged
                  ? 'At least one switch is engaged. Autonomous writes in scope are blocked regardless of tier, confidence or approval.'
                  : 'No switch is engaged. Autonomous actions remain bound by tier limits, evidence quality, blast radius and rate limits.'}
            </p>
            {kill.reason ? (
              <p className="text-meta font-medium text-ink-tertiary">Reason: {kill.reason}</p>
            ) : null}
            <dl className="grid gap-2 pt-1 sm:grid-cols-3">
              <Scope label="Global" values={kill.global ? ['engaged'] : []} />
              <Scope label="Environments" values={kill.environments} />
              <Scope label="Action types" values={kill.action_types} />
              <Scope label="Services" values={kill.services} />
            </dl>
          </div>
        </div>
      </div>

      <dl className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
        <Field
          label="Autonomy"
          value={posture.autonomy_enabled ? 'Enabled' : 'Disabled'}
          tone={posture.autonomy_enabled ? 'default' : 'muted'}
        />
        <Field label="Mode" value={posture.autonomy_mode} />
        <Field
          label="Allowed tiers"
          value={posture.allowed_tiers.length === 0 ? 'None' : posture.allowed_tiers.join(', ')}
        />
        <Field label="Environment" value={posture.environment} />
        <Field label="Rate limit" value={`${posture.max_actions_per_hour} actions / hour`} />
        <Field label="Approval TTL" value={formatDuration(posture.approval_ttl_seconds * 1000)} />
        <Field label="Lease TTL" value={formatDuration(posture.lease_ttl_seconds * 1000)} />
      </dl>
    </div>
  );
}

function Scope({ label, values }: { label: string; values: string[] }) {
  return (
    <div>
      <dt className="label-meta font-semibold">{label}</dt>
      <dd className="mt-0.5 text-meta font-semibold text-ink-secondary">
        {values.length === 0 ? 'none' : values.join(', ')}
      </dd>
    </div>
  );
}

function Field({
  label,
  value,
  tone = 'default',
}: {
  label: string;
  value: string;
  tone?: 'default' | 'muted';
}) {
  return (
    <div className="card p-3.5">
      <dt className="label-meta font-semibold">{label}</dt>
      <dd
        className={cn(
          'mt-1 text-body font-semibold',
          tone === 'muted' ? 'text-ink-tertiary' : 'text-ink-primary',
        )}
      >
        {value}
      </dd>
    </div>
  );
}
