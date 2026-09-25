'use client';

import { useEffect } from 'react';
import { useRouter } from 'next/navigation';
import { Toaster } from 'sonner';
import { useAuth } from '@/components/auth/AuthProvider';

/**
 * Full-screen chrome for the war room.
 *
 * It sits outside the console shell because the sidebar and top bar would take
 * a fifth of a projector's width. The session guard is the console's own: the
 * API still authenticates every request, so this redirect is a courtesy, not a
 * boundary.
 */
export default function WarRoomLayout({ children }: { children: React.ReactNode }) {
  const { status } = useAuth();
  const router = useRouter();

  useEffect(() => {
    if (status === 'anonymous') router.replace('/');
  }, [status, router]);

  if (status !== 'authenticated') {
    return (
      <div className="flex h-screen items-center justify-center bg-canvas" role="status" aria-live="polite">
        <span className="text-body font-medium text-ink-tertiary">
          {status === 'loading' ? 'Restoring session…' : 'Redirecting to sign in…'}
        </span>
      </div>
    );
  }

  return (
    <>
      <main id="main">{children}</main>
      <Toaster
        theme="dark"
        position="bottom-right"
        toastOptions={{
          style: {
            background: 'var(--surface-2)',
            border: '1px solid var(--border-default)',
            color: 'var(--text-primary)',
            fontSize: '1rem',
          },
        }}
      />
    </>
  );
}
