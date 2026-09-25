'use client';

import { createContext, useCallback, useContext, useEffect, useMemo, useState } from 'react';
import { useRouter } from 'next/navigation';
import {
  type Identity,
  availableModes,
  clearSession,
  readSession,
  signOut as doSignOut,
} from '@/lib/auth';
import { onUnauthorized } from '@/lib/api';

/**
 * Session state for the whole console.
 *
 * `status` has three values rather than a boolean because the difference
 * matters visually: while we are still reading storage we must render neither
 * the landing page nor the console, or every reload flashes a sign-in screen at
 * an operator who is already authenticated.
 */
type Status = 'loading' | 'authenticated' | 'anonymous';

interface AuthState {
  status: Status;
  identity: Identity | null;
  modes: ReturnType<typeof availableModes>;
  refresh: () => void;
  signOut: () => Promise<void>;
}

const AuthContext = createContext<AuthState | null>(null);

export function AuthProvider({ children }: { children: React.ReactNode }) {
  const router = useRouter();
  const [status, setStatus] = useState<Status>('loading');
  const [identity, setIdentity] = useState<Identity | null>(null);

  const refresh = useCallback(() => {
    const { token, identity: stored } = readSession();
    setIdentity(stored);
    setStatus(token ? 'authenticated' : 'anonymous');
  }, []);

  // Reading storage in an effect keeps the server and first client render
  // identical, which is what avoids a hydration mismatch on every page.
  useEffect(() => {
    refresh();
  }, [refresh]);

  /**
   * A 401 from any request ends the session immediately.
   *
   * An expired Firebase token otherwise leaves the console showing stale data
   * behind a series of silent failures, which reads as "Aegis is broken" rather
   * than "you need to sign in again".
   */
  useEffect(() => {
    return onUnauthorized(() => {
      clearSession();
      setIdentity(null);
      setStatus('anonymous');
      router.replace('/?expired=1');
    });
  }, [router]);

  // Sign-out in one tab must not leave another tab holding a live console.
  useEffect(() => {
    function onStorage(event: StorageEvent) {
      if (event.key === 'aegis_token') refresh();
    }
    window.addEventListener('storage', onStorage);
    return () => window.removeEventListener('storage', onStorage);
  }, [refresh]);

  const signOut = useCallback(async () => {
    await doSignOut();
    setIdentity(null);
    setStatus('anonymous');
    router.replace('/');
  }, [router]);

  const value = useMemo<AuthState>(
    () => ({ status, identity, modes: availableModes(), refresh, signOut }),
    [status, identity, refresh, signOut],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthState {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error('useAuth must be used inside <AuthProvider>');
  return ctx;
}
