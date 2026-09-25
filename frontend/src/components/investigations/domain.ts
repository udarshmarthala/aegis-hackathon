import type { IncidentState, Severity } from '@/lib/types';

/**
 * The investigation endpoints type severity and state as plain strings, since
 * they come straight out of stored rows. Narrowing happens here rather than
 * with a cast: a value the design system has no treatment for is rendered as
 * text instead of being forced into a badge that would misstate it.
 */

const SEVERITIES: readonly Severity[] = ['P1', 'P2', 'P3', 'P4'];

const STATES: readonly IncidentState[] = [
  'RECEIVED', 'TRIAGING', 'INVESTIGATING', 'DIAGNOSING', 'DEBUGGING', 'VERIFYING',
  'AWAITING_APPROVAL', 'REMEDIATING', 'MONITORING', 'RESOLVED', 'ESCALATED', 'BLOCKED',
];

export function asSeverity(value: string): Severity | null {
  return SEVERITIES.find((severity) => severity === value.toUpperCase()) ?? null;
}

export function asIncidentState(value: string): IncidentState | null {
  return STATES.find((state) => state === value.toUpperCase()) ?? null;
}

export type StatusTone = 'success' | 'critical' | 'info' | 'warning' | 'muted';

export function runStatusTone(status: string): StatusTone {
  const upper = status.toUpperCase();
  if (upper.includes('FAIL') || upper.includes('ERROR')) return 'critical';
  if (upper.includes('SUCCESS') || upper.includes('COMPLETE') || upper.includes('OK')) {
    return 'success';
  }
  if (upper.includes('RUN') || upper.includes('START') || upper.includes('PENDING')) return 'info';
  if (upper.includes('TIMEOUT') || upper.includes('PARTIAL') || upper.includes('ABSTAIN')) {
    return 'warning';
  }
  return 'muted';
}

export const STATUS_TONE_CLASS: Record<StatusTone, string> = {
  success: 'border-status-success/40 bg-status-success/10 text-status-success',
  critical: 'border-status-critical/40 bg-status-critical/10 text-status-critical',
  info: 'border-status-info/40 bg-status-info/10 text-status-info',
  warning: 'border-status-warning/40 bg-status-warning/10 text-status-warning',
  muted: 'border-line bg-surface-3 text-ink-secondary',
};

export const STATUS_DOT_CLASS: Record<StatusTone, string> = {
  success: 'bg-status-success',
  critical: 'bg-status-critical',
  info: 'bg-status-info',
  warning: 'bg-status-warning',
  muted: 'bg-status-neutral',
};
