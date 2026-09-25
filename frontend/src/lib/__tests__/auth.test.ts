import { afterEach, beforeEach, describe, expect, test, vi } from 'vitest';

import { getAuthToken, setAuthToken } from '@/lib/api';
import {
  IDENTITY_KEY,
  TOKEN_KEY,
  availableModes,
  clearSession,
  firebaseConfig,
  type Identity,
  readSession,
  signInWithDevToken,
  storeSession,
} from '@/lib/auth';

/**
 * The credential boundary.
 *
 * Two properties matter more than the rest. The development bypass must be
 * impossible to reach outside the local environment - a bypass that works in
 * staging is not a bypass, it is a published password. And a browser that
 * refuses storage must degrade to a shorter session rather than an unusable
 * console, because an operator in private mode is still an operator.
 */

const IDENTITY: Identity = {
  uid: 'dev-user',
  email: 'dev@localhost',
  displayName: 'Local Developer',
  photoURL: null,
  mode: 'local',
};

/** A complete Firebase web config; individual keys are removed per test. */
function stubFirebaseEnv(): void {
  vi.stubEnv('NEXT_PUBLIC_FIREBASE_API_KEY', 'key-abc');
  vi.stubEnv('NEXT_PUBLIC_FIREBASE_AUTH_DOMAIN', 'aegis.firebaseapp.com');
  vi.stubEnv('NEXT_PUBLIC_FIREBASE_PROJECT_ID', 'aegis-prod');
}

function clearFirebaseEnv(): void {
  vi.stubEnv('NEXT_PUBLIC_FIREBASE_API_KEY', undefined);
  vi.stubEnv('NEXT_PUBLIC_FIREBASE_AUTH_DOMAIN', undefined);
  vi.stubEnv('NEXT_PUBLIC_FIREBASE_PROJECT_ID', undefined);
}

beforeEach(() => {
  setAuthToken(null);
  window.localStorage.clear();
  clearFirebaseEnv();
});

afterEach(() => {
  setAuthToken(null);
});

describe('what this deployment can offer', () => {
  test('neither Firebase nor a local environment reports unconfigured, never an empty list', () => {
    // An empty array would let a caller render a sign-in page with no methods
    // on it and no explanation - the sign-in equivalent of collapsing "nothing
    // found" into "could not look".
    vi.stubEnv('NEXT_PUBLIC_AEGIS_ENV', 'production');

    expect(availableModes()).toEqual(['unconfigured']);
  });

  test('the local environment offers the development bypass', () => {
    vi.stubEnv('NEXT_PUBLIC_AEGIS_ENV', 'local');

    expect(availableModes()).toEqual(['local']);
  });

  test('an unconfigured environment does not offer the development bypass', () => {
    // The failure this guards is a deployment nobody configured: push the
    // console to a hosting platform, set no environment, and the bypass form
    // appears on a public URL. Absent configuration must grant nothing, so the
    // bypass is reachable only when something explicitly says `local`.
    vi.stubEnv('NEXT_PUBLIC_AEGIS_ENV', undefined);

    expect(availableModes()).toEqual(['unconfigured']);
  });

  test('a complete Firebase config offers Firebase even in a hardened environment', () => {
    vi.stubEnv('NEXT_PUBLIC_AEGIS_ENV', 'production');
    stubFirebaseEnv();

    expect(availableModes()).toEqual(['firebase']);
  });
});

describe('firebaseConfig', () => {
  test('a config missing any of apiKey, authDomain or projectId is no config at all', () => {
    // A partially-populated config initialises an app that fails later, at
    // sign-in, with an opaque SDK error. Returning null makes the deployment
    // mistake visible where it happens.
    for (const missing of [
      'NEXT_PUBLIC_FIREBASE_API_KEY',
      'NEXT_PUBLIC_FIREBASE_AUTH_DOMAIN',
      'NEXT_PUBLIC_FIREBASE_PROJECT_ID',
    ]) {
      stubFirebaseEnv();
      vi.stubEnv(missing, undefined);
      expect(firebaseConfig(), `${missing} absent`).toBeNull();
    }
  });

  test('all three present yields a config', () => {
    stubFirebaseEnv();

    expect(firebaseConfig()).toMatchObject({
      apiKey: 'key-abc',
      authDomain: 'aegis.firebaseapp.com',
      projectId: 'aegis-prod',
    });
  });
});

describe('the development bypass', () => {
  test('development sign-in refuses outside the local environment', async () => {
    // The pre-shared token the API accepts under AUTH_DEV_MODE must never be a
    // route into a deployed console. The backend refuses to boot production
    // with that flag set; this is the same refusal on the browser side.
    vi.stubEnv('NEXT_PUBLIC_AEGIS_ENV', 'staging');

    await expect(signInWithDevToken('a-real-looking-token')).rejects.toThrow(/only available in the local environment/i);
    expect(window.localStorage.getItem(TOKEN_KEY)).toBeNull();
    expect(getAuthToken()).toBeNull();
  });

  test('a rejected development sign-in stores nothing, even in the local environment', async () => {
    // An empty token would be stored as a credential and sent on every
    // subsequent request, turning a mistyped sign-in into a stream of 401s.
    vi.stubEnv('NEXT_PUBLIC_AEGIS_ENV', 'local');

    await expect(signInWithDevToken('   ')).rejects.toThrow(/Enter the development access token/i);
    expect(window.localStorage.getItem(TOKEN_KEY)).toBeNull();
    expect(getAuthToken()).toBeNull();
  });

  test('a successful development sign-in trims the token and arms the client immediately', async () => {
    // The in-memory token matters as much as the stored one: the first request
    // after sign-in fires before any reload, so it must already carry it.
    vi.stubEnv('NEXT_PUBLIC_AEGIS_ENV', 'local');

    const result = await signInWithDevToken('  token-xyz  ');

    expect(result.token).toBe('token-xyz');
    expect(result.identity.mode).toBe('local');
    expect(getAuthToken()).toBe('token-xyz');
    expect(window.localStorage.getItem(TOKEN_KEY)).toBe('token-xyz');
  });
});

describe('session persistence', () => {
  test('a stored session round-trips so a reload does not bounce an operator mid-incident', () => {
    storeSession('token-123', IDENTITY);
    setAuthToken(null); // simulate the fresh module state after a page reload

    const restored = readSession();

    expect(restored.token).toBe('token-123');
    expect(restored.identity).toEqual(IDENTITY);
    // Reading the session must also re-arm the client, or the first request
    // after a reload goes out unauthenticated and the console bounces anyway.
    expect(getAuthToken()).toBe('token-123');
  });

  test('a browser that refuses storage keeps the in-memory session', () => {
    // Private mode throws from setItem. The operator loses persistence across
    // reloads, which is a nuisance; losing the session outright would be a
    // lockout.
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw new DOMException('Access is denied for this document.', 'SecurityError');
    });

    expect(() => {
      storeSession('token-ephemeral', IDENTITY);
    }).not.toThrow();
    expect(getAuthToken()).toBe('token-ephemeral');
  });

  test('a browser that refuses reads yields an empty session rather than an exception', () => {
    vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => {
      throw new DOMException('Access is denied for this document.', 'SecurityError');
    });

    expect(readSession()).toEqual({ token: null, identity: null });
  });

  test('a corrupted identity does not strand the console on a parse error', () => {
    // Half-written storage is a real failure mode. It must degrade to "signed
    // out", not to an unhandled SyntaxError during hydration.
    window.localStorage.setItem(TOKEN_KEY, 'token-123');
    window.localStorage.setItem(IDENTITY_KEY, '{not json');

    expect(readSession()).toEqual({ token: null, identity: null });
  });

  test('clearing a session removes the credential from memory and from storage', () => {
    // A token left in localStorage after sign-out is readable by anything that
    // can run script on this origin.
    storeSession('token-123', IDENTITY);

    clearSession();

    expect(getAuthToken()).toBeNull();
    expect(window.localStorage.getItem(TOKEN_KEY)).toBeNull();
    expect(window.localStorage.getItem(IDENTITY_KEY)).toBeNull();
  });
});
