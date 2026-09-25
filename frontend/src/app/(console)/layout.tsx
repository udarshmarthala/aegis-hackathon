'use client';

import { useEffect } from 'react';
import { useRouter } from 'next/navigation';
import { AppShell } from '@/components/shell/AppShell';
import { useAuth } from '@/components/auth/AuthProvider';

/**
 * The authenticated console.
 *
 * The guard runs client-side because the session lives in the browser: the API
 * verifies every request itself, so this redirect is a courtesy that keeps an
 * operator from staring at a page of failed requests, not a security boundary.
 * The real boundary is the bearer token the API checks on every call.
 *
 * While the session is still being read we render nothing. Showing the console
 * would flash unauthenticated content; redirecting would bounce an operator who
 * is in fact signed in.
 */
export default function ConsoleLayout({ children }: { children: React.ReactNode }) {
  const { status } = useAuth();
  const router = useRouter();

  useEffect(() => {
    if (status === 'anonymous') router.replace('/');
  }, [status, router]);

  if (status !== 'authenticated') {
    return (
      <div
        className="flex h-screen items-center justify-center bg-canvas"
        role="status"
        aria-live="polite"
      >
        <span className="text-body font-medium text-ink-tertiary">
          {status === 'loading' ? 'Restoring session…' : 'Redirecting to sign in…'}
        </span>
      </div>
    );
  }

  return (
    <AppShell>
      <div id="main">{children}</div>
    </AppShell>
  );
}
