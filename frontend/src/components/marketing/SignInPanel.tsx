'use client';

import { useEffect, useState } from 'react';
import { useRouter, useSearchParams } from 'next/navigation';
import { AlertTriangle } from 'lucide-react';
import { useAuth } from '@/components/auth/AuthProvider';
import { signInWithDevToken, signInWithEmail, signInWithGoogle } from '@/lib/auth';
import { SignInForms } from './SignInForms';

/**
 * The sign-in panel.
 *
 * Presented as an operational entry point rather than a marketing call to
 * action: this is the door to a system that can change production, so it states
 * plainly which authentication methods this deployment actually supports rather
 * than offering buttons that cannot work.
 *
 * Failure messages say what an operator can do about it. "Invalid credentials"
 * is useless at 3am; "Firebase is not configured for this deployment" tells
 * them where to look.
 */
export function SignInPanel() {
  const router = useRouter();
  const params = useSearchParams();
  const { status, modes, refresh } = useAuth();

  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [devToken, setDevToken] = useState('');
  const [busy, setBusy] = useState<null | 'google' | 'email' | 'dev'>(null);
  const [error, setError] = useState('');

  const hasFirebase = modes.includes('firebase');
  const hasLocal = modes.includes('local');
  const expired = params.get('expired') === '1';

  // An operator who is already signed in should never be asked again.
  useEffect(() => {
    if (status === 'authenticated') router.replace('/overview');
  }, [status, router]);

  async function run(kind: 'google' | 'email' | 'dev', fn: () => Promise<unknown>) {
    setBusy(kind);
    setError('');
    try {
      await fn();
      refresh();
      router.replace('/overview');
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Sign-in failed.');
    } finally {
      setBusy(null);
    }
  }

  return (
    <div className="flex h-full flex-col justify-center gap-6 px-8 py-12 xl:px-12">
      <header className="space-y-2">
        <h2 className="text-h2 font-bold tracking-tight text-ink-primary">Sign in to Aegis</h2>
        <p className="text-body font-medium leading-relaxed text-ink-secondary">
          Access the control plane. Every action you take is attributed to you
          and recorded in the audit trail.
        </p>
      </header>

      {expired && (
        <div
          role="status"
          className="flex items-start gap-2.5 rounded-card border border-status-warning/30
                     bg-status-warning/[0.07] px-3.5 py-3"
        >
          <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0 text-status-warning" aria-hidden />
          <p className="text-meta font-medium leading-relaxed text-ink-secondary">
            Your session expired and you were signed out. Nothing was lost.
          </p>
        </div>
      )}

      {error && (
        <div
          role="alert"
          className="flex items-start gap-2.5 rounded-card border border-status-critical/30
                     bg-status-critical/[0.07] px-3.5 py-3"
        >
          <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0 text-status-critical" aria-hidden />
          <p className="text-meta font-medium leading-relaxed text-ink-primary">{error}</p>
        </div>
      )}

      {!hasFirebase && !hasLocal && (
        <div role="alert" className="rounded-card border border-line bg-surface-2 px-3.5 py-3">
          <p className="text-meta font-semibold text-ink-primary">
            No sign-in method is configured
          </p>
          <p className="mt-1.5 text-meta font-medium leading-relaxed text-ink-tertiary">
            Set the <code className="font-mono text-ink-secondary">NEXT_PUBLIC_FIREBASE_*</code>{' '}
            variables and rebuild the web image, or run with{' '}
            <code className="font-mono text-ink-secondary">NEXT_PUBLIC_AEGIS_ENV=local</code>.
          </p>
        </div>
      )}

      <SignInForms
        hasFirebase={hasFirebase}
        hasLocal={hasLocal}
        busy={busy}
        email={email}
        password={password}
        devToken={devToken}
        onEmailChange={setEmail}
        onPasswordChange={setPassword}
        onDevTokenChange={setDevToken}
        onGoogle={() => void run('google', signInWithGoogle)}
        onEmail={() => void run('email', () => signInWithEmail(email, password))}
        onDev={() => void run('dev', () => signInWithDevToken(devToken))}
      />

      <p className="text-meta font-medium leading-relaxed text-ink-tertiary">
        Aegis never takes a production action without either a deterministic
        policy allowing it or a human approving it. Your identity is what makes
        that record meaningful.
      </p>
    </div>
  );
}
