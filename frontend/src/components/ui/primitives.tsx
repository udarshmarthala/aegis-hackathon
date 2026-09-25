'use client';

import { cn } from '@/lib/utils';
import type { EvidenceStatus, IncidentState, Severity, TrustClass } from '@/lib/types';

/**
 * Semantic primitives.
 *
 * Components consume domain values (`severity="P1"`), never presentation values
 * (`color="#FF0000"`). The design system owns the mapping, which is what keeps
 * severity treatment consistent across every surface (UX spec section 93).
 *
 * Status is never conveyed by colour alone - each chip carries text and, where
 * it matters, a shape. That is a WCAG requirement, and it also survives the
 * 2 a.m. low-contrast-monitor test.
 */

const SEVERITY_STYLES: Record<Severity, string> = {
  P1: 'border-status-critical/40 bg-status-critical/10 text-status-critical',
  P2: 'border-status-warning/40 bg-status-warning/10 text-status-warning',
  P3: 'border-status-warning/25 bg-status-warning/5 text-status-warning/80',
  P4: 'border-line bg-surface-3 text-ink-tertiary',
};

export function SeverityBadge({ severity, className }: { severity: Severity; className?: string }) {
  return (
    <span
      className={cn(
        'inline-flex h-5 min-w-[26px] items-center justify-center rounded border px-1.5',
        'text-meta font-semibold tabular-nums',
        SEVERITY_STYLES[severity],
        className,
      )}
      title={`Severity ${severity}`}
    >
      {severity}
    </span>
  );
}

const STATE_TONE: Record<IncidentState, { dot: string; text: string; label: string }> = {
  RECEIVED:          { dot: 'bg-status-neutral', text: 'text-ink-secondary', label: 'Received' },
  TRIAGING:          { dot: 'bg-status-info',    text: 'text-status-info',   label: 'Triaging' },
  INVESTIGATING:     { dot: 'bg-status-info',    text: 'text-status-info',   label: 'Investigating' },
  DIAGNOSING:        { dot: 'bg-status-info',    text: 'text-status-info',   label: 'Diagnosing' },
  DEBUGGING:         { dot: 'bg-accent',         text: 'text-accent',        label: 'Debugging' },
  VERIFYING:         { dot: 'bg-accent',         text: 'text-accent',        label: 'Verifying' },
  AWAITING_APPROVAL: { dot: 'bg-status-warning', text: 'text-status-warning',label: 'Awaiting approval' },
  REMEDIATING:       { dot: 'bg-status-warning', text: 'text-status-warning',label: 'Remediating' },
  MONITORING:        { dot: 'bg-status-success', text: 'text-status-success',label: 'Monitoring' },
  RESOLVED:          { dot: 'bg-status-success', text: 'text-status-success',label: 'Resolved' },
  ESCALATED:         { dot: 'bg-status-critical',text: 'text-status-critical',label: 'Escalated' },
  BLOCKED:           { dot: 'bg-status-critical',text: 'text-status-critical',label: 'Blocked' },
};

const LIVE_STATES: IncidentState[] = ['TRIAGING', 'INVESTIGATING', 'DIAGNOSING', 'DEBUGGING', 'VERIFYING'];

export function StateChip({ state, className }: { state: IncidentState; className?: string }) {
  const tone = STATE_TONE[state];
  const live = LIVE_STATES.includes(state);
  return (
    <span className={cn('inline-flex items-center gap-1.5 text-body', tone.text, className)}>
      <span
        className={cn('h-1.5 w-1.5 rounded-full', tone.dot, live && 'animate-pulse-soft')}
        aria-hidden
      />
      {tone.label}
    </span>
  );
}

/**
 * Evidence status. SOURCE_UNAVAILABLE gets its own visibly different treatment
 * because "we could not look" and "we looked and found nothing" lead to
 * opposite operational conclusions.
 */
const EVIDENCE_TONE: Record<EvidenceStatus, { cls: string; label: string }> = {
  VALIDATED:          { cls: 'border-status-success/40 text-status-success', label: 'Validated' },
  UNVALIDATED:        { cls: 'border-line text-ink-secondary',               label: 'Unvalidated' },
  REFUTED:            { cls: 'border-status-critical/40 text-status-critical', label: 'Refuted' },
  SOURCE_UNAVAILABLE: { cls: 'border-status-warning/50 text-status-warning bg-status-warning/5', label: 'Source unavailable' },
};

export function EvidenceStatusChip({ status }: { status: EvidenceStatus }) {
  const tone = EVIDENCE_TONE[status];
  return (
    <span className={cn('rounded border px-1.5 py-0.5 text-meta uppercase tracking-wider', tone.cls)}>
      {tone.label}
    </span>
  );
}

const TRUST_LABEL: Record<TrustClass, string> = {
  TIER_A: 'A · machine observation',
  TIER_B: 'B · structured metadata',
  TIER_C: 'C · human authored',
  TIER_D: 'D · untrusted text',
};

export function TrustChip({ trust }: { trust: TrustClass }) {
  return (
    <span
      className={cn(
        'rounded border px-1.5 py-0.5 text-meta uppercase tracking-wider',
        trust === 'TIER_A' ? 'border-edge text-ink-secondary' : 'border-hairline text-ink-tertiary',
      )}
      title={TRUST_LABEL[trust]}
    >
      {trust.replace('TIER_', 'Tier ')}
    </span>
  );
}

/**
 * Confidence. A horizontal meter with the supporting counts beside it - never a
 * giant circular gauge, and never a number without its basis (UX spec 22).
 */
export function ConfidenceMeter({
  value,
  explain,
  className,
}: {
  value: number | null | undefined;
  explain?: string[];
  className?: string;
}) {
  const known = typeof value === 'number' && !Number.isNaN(value);
  const percent = known ? Math.round(value * 100) : 0;
  return (
    <div className={cn('space-y-1.5', className)}>
      <div className="flex items-baseline gap-2">
        <span className="tnum text-h2 font-medium">{known ? `${percent}%` : '—'}</span>
        <span className="label-meta">confidence</span>
      </div>
      <div
        className="h-1 w-full overflow-hidden rounded-full bg-surface-4"
        role="meter"
        aria-valuenow={percent}
        aria-valuemin={0}
        aria-valuemax={100}
        aria-label="Diagnosis confidence"
      >
        <div
          className={cn(
            'h-full rounded-full transition-all duration-500',
            percent >= 75 ? 'bg-status-success' : percent >= 45 ? 'bg-status-warning' : 'bg-status-critical',
          )}
          style={{ width: `${percent}%` }}
        />
      </div>
      {explain?.length ? (
        <ul className="space-y-0.5 pt-0.5">
          {explain.map((line) => (
            <li key={line} className="text-meta text-ink-tertiary">{line}</li>
          ))}
        </ul>
      ) : null}
    </div>
  );
}
