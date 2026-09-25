'use client';

import { ArrowRight, KeyRound, Loader2 } from 'lucide-react';
import { cn } from '@/lib/utils';

interface Props {
  hasFirebase: boolean;
  hasLocal: boolean;
  busy: null | 'google' | 'email' | 'dev';
  email: string;
  password: string;
  devToken: string;
  onEmailChange: (value: string) => void;
  onPasswordChange: (value: string) => void;
  onDevTokenChange: (value: string) => void;
  onGoogle: () => void;
  onEmail: () => void;
  onDev: () => void;
}

const FIELD =
  'h-11 w-full rounded-btn border border-line bg-surface-1 px-3 text-body font-medium ' +
  'text-ink-primary placeholder:text-ink-tertiary transition-colors duration-hover ' +
  'focus:border-edge focus:outline-none focus:ring-2 focus:ring-accent/30';

/**
 * The credential forms themselves.
 *
 * Split from the panel so the panel owns state and messaging while this owns
 * markup - it keeps both files short enough to read in one screen, which is the
 * whole reason anyone reviews an authentication form carefully.
 *
 * Every control is disabled while a sign-in is in flight. Two concurrent
 * attempts would race to write the session and the loser would silently
 * overwrite the winner.
 */
export function SignInForms(props: Props) {
  const {
    hasFirebase, hasLocal, busy, email, password, devToken,
    onEmailChange, onPasswordChange, onDevTokenChange, onGoogle, onEmail, onDev,
  } = props;
  const anyBusy = busy !== null;

  return (
    <div className="space-y-5">
      {hasFirebase && (
        <>
          <button
            type="button"
            onClick={onGoogle}
            disabled={anyBusy}
            className={cn(
              'flex h-11 w-full items-center justify-center gap-2.5 rounded-btn border',
              'border-line bg-surface-2 text-body font-semibold text-ink-primary',
              'transition-colors duration-hover hover:bg-surface-3',
              'disabled:cursor-not-allowed disabled:opacity-50',
            )}
          >
            {busy === 'google' ? (
              <Loader2 className="h-4 w-4 animate-spin" aria-hidden />
            ) : (
              <GoogleMark />
            )}
            Continue with Google
          </button>

          <div className="flex items-center gap-3">
            <span className="h-px flex-1 bg-hairline" />
            <span className="text-meta font-semibold uppercase tracking-wider text-ink-tertiary">
              or
            </span>
            <span className="h-px flex-1 bg-hairline" />
          </div>

          <form
            className="space-y-3"
            onSubmit={(event) => {
              event.preventDefault();
              onEmail();
            }}
          >
            <div className="space-y-1.5">
              <label htmlFor="email" className="block text-meta font-semibold text-ink-secondary">
                Work email
              </label>
              <input
                id="email"
                type="email"
                required
                autoComplete="email"
                value={email}
                disabled={anyBusy}
                onChange={(e) => onEmailChange(e.target.value)}
                placeholder="you@company.com"
                className={FIELD}
              />
            </div>
            <div className="space-y-1.5">
              <label
                htmlFor="password"
                className="block text-meta font-semibold text-ink-secondary"
              >
                Password
              </label>
              <input
                id="password"
                type="password"
                required
                autoComplete="current-password"
                value={password}
                disabled={anyBusy}
                onChange={(e) => onPasswordChange(e.target.value)}
                placeholder="••••••••••••"
                className={FIELD}
              />
            </div>
            <button
              type="submit"
              disabled={anyBusy}
              className={cn(
                'flex h-11 w-full items-center justify-center gap-2 rounded-btn',
                'bg-ink-primary text-body font-bold text-canvas',
                'transition-opacity duration-hover hover:opacity-90',
                'disabled:cursor-not-allowed disabled:opacity-50',
              )}
            >
              {busy === 'email' ? (
                <Loader2 className="h-4 w-4 animate-spin" aria-hidden />
              ) : (
                <ArrowRight className="h-4 w-4" aria-hidden />
              )}
              Continue
            </button>
          </form>
        </>
      )}

      {hasLocal && (
        <form
          className="space-y-3 rounded-card border border-hairline bg-surface-1 p-4"
          onSubmit={(event) => {
            event.preventDefault();
            onDev();
          }}
        >
          <div className="flex items-center gap-2">
            <KeyRound className="h-3.5 w-3.5 text-ink-tertiary" aria-hidden />
            <span className="text-meta font-semibold uppercase tracking-wider text-ink-tertiary">
              Local development
            </span>
          </div>
          <p className="text-meta font-medium leading-relaxed text-ink-tertiary">
            Paste <code className="font-mono text-ink-secondary">AUTH_DEV_BYPASS_TOKEN</code> from
            your <code className="font-mono text-ink-secondary">.env</code>. This is refused
            outright in production.
          </p>
          <input
            id="dev-token"
            type="password"
            value={devToken}
            disabled={anyBusy}
            onChange={(e) => onDevTokenChange(e.target.value)}
            placeholder="Development access token"
            aria-label="Development access token"
            className={cn(FIELD, 'font-mono')}
          />
          <button
            type="submit"
            disabled={anyBusy}
            className={cn(
              'flex h-10 w-full items-center justify-center gap-2 rounded-btn border',
              'border-line bg-surface-2 text-body font-semibold text-ink-primary',
              'transition-colors duration-hover hover:bg-surface-3',
              'disabled:cursor-not-allowed disabled:opacity-50',
            )}
          >
            {busy === 'dev' && <Loader2 className="h-4 w-4 animate-spin" aria-hidden />}
            Enter console
          </button>
        </form>
      )}
    </div>
  );
}

function GoogleMark() {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" aria-hidden="true">
      <path fill="#4285F4" d="M23 12.3c0-.8-.1-1.6-.2-2.3H12v4.5h6.2a5.3 5.3 0 0 1-2.3 3.5v2.9h3.7c2.2-2 3.4-5 3.4-8.6Z" />
      <path fill="#34A853" d="M12 24c3.1 0 5.7-1 7.6-2.8l-3.7-2.9c-1 .7-2.3 1.1-3.9 1.1-3 0-5.5-2-6.4-4.7H1.8v3a12 12 0 0 0 10.2 6.3Z" />
      <path fill="#FBBC05" d="M5.6 14.7a7.2 7.2 0 0 1 0-4.6v-3H1.8a12 12 0 0 0 0 10.6l3.8-3Z" />
      <path fill="#EA4335" d="M12 4.8c1.7 0 3.2.6 4.4 1.7l3.3-3.3A12 12 0 0 0 1.8 7.1l3.8 3c.9-2.7 3.4-4.7 6.4-4.7Z" />
    </svg>
  );
}
