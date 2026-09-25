'use client';

import Link from 'next/link';
import { usePathname } from 'next/navigation';
import { useQuery } from '@tanstack/react-query';
import {
  ChevronRight, Command, HelpCircle, LogOut, ShieldAlert, ShieldCheck, ShieldQuestion,
} from 'lucide-react';
import { useAuth } from '@/components/auth/AuthProvider';
import { ApiError, NetworkError, api } from '@/lib/api';
import { WorkingIndicator } from '@/components/ui/states';
import { cn } from '@/lib/utils';

/**
 * Why a read failed, in one sentence an operator can act on.
 *
 * The two error shapes are kept apart deliberately: "we could not reach Aegis"
 * and "Aegis refused" lead to different next steps.
 */
function readFailureReason(error: unknown): string {
  if (error instanceof NetworkError) return error.message;
  if (error instanceof ApiError) return `Aegis answered ${error.status} (${error.code}).`;
  return 'The read did not complete.';
}

/**
 * Breadcrumbs are derived from the path so deep graph and code drilldowns never
 * lose their trail (UX spec 89).
 */
function useBreadcrumbs() {
  const pathname = usePathname();
  const parts = pathname.split('/').filter(Boolean);
  if (parts.length === 0) return [{ href: '/overview', label: 'Home' }];
  const crumbs: Array<{ href: string; label: string }> = [];
  let acc = '';
  for (const part of parts) {
    acc += `/${part}`;
    crumbs.push({
      href: acc,
      label: part.startsWith('inc_')
        ? part.slice(0, 12).toUpperCase()
        : part.charAt(0).toUpperCase() + part.slice(1),
    });
  }
  return crumbs;
}

/**
 * Autonomy status is always visible. An operator must never have to navigate to
 * a settings page to learn whether Aegis is allowed to act.
 */
function AutonomyIndicator() {
  const { data, isLoading, isError, error, refetch } = useQuery({
    queryKey: ['policy'],
    queryFn: api.policy,
    refetchInterval: 30_000,
    retry: 1,
  });

  if (isLoading) return <WorkingIndicator label="Reading autonomy policy" />;

  // An unread policy is shown as an explicit unknown rather than as a posture.
  // Of the two ways to be wrong here, implying the kill switch has been checked
  // when it has not is by far the more dangerous.
  if (isError || !data) {
    const reason = readFailureReason(error);
    return (
      <button
        type="button"
        onClick={() => void refetch()}
        title={`${reason} Whether a kill switch is engaged cannot be determined from here. Select to retry.`}
        aria-label={`Autonomy posture unknown. ${reason} Whether a kill switch is engaged cannot be determined from here. Select to retry.`}
        className="inline-flex items-center gap-1.5 rounded-btn border border-status-warning/40
                   bg-status-warning/10 px-2 py-1 text-meta text-status-warning
                   transition-colors duration-hover hover:bg-status-warning/15"
      >
        <ShieldQuestion className="h-3.5 w-3.5" aria-hidden />
        <span aria-hidden>Autonomy unknown</span>
      </button>
    );
  }

  const killed = data.kill_switches.any_engaged;
  const enabled = data.autonomy.enabled && !killed;
  const label = killed
    ? 'Halted'
    : enabled
      ? data.autonomy.mode === 'guarded' ? 'Guarded' : 'Active'
      : 'Observe only';

  const Icon = killed ? ShieldAlert : ShieldCheck;
  return (
    <Link
      href="/policies"
      className={cn(
        'inline-flex items-center gap-1.5 rounded-btn border px-2 py-1 text-meta',
        'transition-colors duration-hover',
        killed
          ? 'border-status-critical/40 bg-status-critical/10 text-status-critical'
          : enabled
            ? 'border-line text-ink-secondary hover:bg-surface-3'
            : 'border-hairline text-ink-tertiary hover:bg-surface-2',
      )}
      title={
        killed
          ? 'A kill switch is engaged; no autonomous action can execute.'
          : `Autonomy ${data.autonomy.enabled ? 'enabled' : 'disabled'} · tiers ${
              data.autonomy.allowed_tiers.join(', ') || 'none'
            }`
      }
    >
      <Icon className="h-3.5 w-3.5" aria-hidden />
      {label}
    </Link>
  );
}

export function TopBar({ onOpenPalette }: { onOpenPalette: () => void }) {
  const crumbs = useBreadcrumbs();
  const {
    data: health, isLoading: healthLoading, error: healthError, refetch: refetchHealth,
  } = useQuery({
    queryKey: ['health'],
    queryFn: api.health,
    refetchInterval: 30_000,
    retry: 1,
  });

  return (
    <header className="flex h-[54px] shrink-0 items-center justify-between gap-4
                       border-b border-hairline bg-canvas px-5">
      <nav aria-label="Breadcrumb" className="flex min-w-0 items-center gap-1 text-body">
        {crumbs.map((crumb, i) => (
          <span key={crumb.href} className="flex min-w-0 items-center gap-1">
            {i > 0 && <ChevronRight className="h-3 w-3 shrink-0 text-ink-tertiary" aria-hidden />}
            <Link
              href={crumb.href}
              className={cn(
                'truncate transition-colors duration-hover',
                i === crumbs.length - 1
                  ? 'text-ink-primary'
                  : 'text-ink-tertiary hover:text-ink-secondary',
              )}
            >
              {crumb.label}
            </Link>
          </span>
        ))}
      </nav>

      <div className="flex shrink-0 items-center gap-2.5">
        <button
          type="button"
          onClick={onOpenPalette}
          className="inline-flex items-center gap-2 rounded-btn border border-line bg-surface-2
                     px-2.5 py-1 text-meta text-ink-tertiary
                     transition-colors duration-hover hover:bg-surface-3 hover:text-ink-secondary"
          aria-label="Open command palette"
        >
          <Command className="h-3 w-3" aria-hidden />
          Search
          <kbd className="rounded border border-line px-1 font-mono text-[10px]">Ctrl K</kbd>
        </button>

        {healthLoading ? (
          <WorkingIndicator label="Reading integration health" />
        ) : health ? (
          <Link
            href="/settings"
            className="inline-flex items-center gap-1.5 text-meta text-ink-tertiary
                       transition-colors duration-hover hover:text-ink-secondary"
            title={
              health.degraded_components.length
                ? `Degraded: ${health.degraded_components.join(', ')}`
                : 'All integrations healthy'
            }
          >
            <span
              className={cn(
                'h-1.5 w-1.5 rounded-full',
                health.status === 'healthy'
                  ? 'bg-status-success'
                  : health.status === 'degraded'
                    ? 'bg-status-warning'
                    : 'bg-status-critical',
              )}
              aria-hidden
            />
            {health.status === 'healthy'
              ? 'Systems nominal'
              : `${health.degraded_components.length} degraded`}
          </Link>
        ) : (
          // An absent pill would read as "healthy, nothing to report". The
          // failure has to occupy the same space the verdict would have.
          <button
            type="button"
            onClick={() => void refetchHealth()}
            title={`${readFailureReason(healthError)} Select to retry.`}
            aria-label={`Integration health unknown. ${readFailureReason(healthError)} This is not a report that every integration is healthy. Select to retry.`}
            className="inline-flex items-center gap-1.5 text-meta text-status-warning
                       transition-colors duration-hover hover:text-status-warning/80"
          >
            <HelpCircle className="h-3 w-3" aria-hidden />
            <span aria-hidden>Health unknown</span>
          </button>
        )}

        {health ? (
          // "—" and "unknown" are different claims: one is a backend that
          // reported no environment name, the other is an unread source.
          <span
            className="text-meta uppercase tracking-wider text-ink-tertiary"
            title={health.environment ? undefined : 'Aegis reports no environment name.'}
          >
            {health.environment || '—'}
          </span>
        ) : healthLoading ? (
          <span className="text-meta uppercase tracking-wider text-ink-tertiary">
            <span aria-hidden>env …</span>
            <span className="sr-only">Reading environment</span>
          </span>
        ) : (
          <span className="text-meta uppercase tracking-wider text-status-warning">
            <span aria-hidden>unknown</span>
            <span className="sr-only">
              Environment unknown — the health endpoint could not be read.
            </span>
          </span>
        )}

        <AutonomyIndicator />
        <AccountMenu />
      </div>
    </header>
  );
}

/**
 * Who is signed in, and the way out.
 *
 * The identity is shown rather than hidden behind a menu because every action
 * in the audit log is attributed to it. An operator about to approve a
 * production change should be able to see, without clicking, which account is
 * about to be recorded as having approved it.
 */
function AccountMenu() {
  const { identity, signOut } = useAuth();
  if (!identity) return null;

  const label = identity.displayName || identity.email || identity.uid;
  const fullLabel = `${label}${identity.mode === 'local' ? ' (local development session)' : ''}`;
  return (
    <div className="flex items-center gap-2 border-l border-hairline pl-2.5">
      {/* The visible label is truncated and its tooltip is mouse-only, so the
          untruncated identity — the one an approval is recorded against — is
          announced rather than left to a hover a keyboard user cannot reach. */}
      <span
        className="max-w-[160px] truncate text-meta font-semibold text-ink-secondary"
        title={fullLabel}
        aria-hidden
      >
        {label}
      </span>
      <span className="sr-only">Signed in as {fullLabel}</span>
      <button
        type="button"
        onClick={() => void signOut()}
        aria-label="Sign out"
        title="Sign out"
        className="rounded-btn p-1 text-ink-tertiary transition-colors duration-hover
                   hover:bg-surface-3 hover:text-ink-primary"
      >
        <LogOut className="h-3.5 w-3.5" aria-hidden />
      </button>
    </div>
  );
}
