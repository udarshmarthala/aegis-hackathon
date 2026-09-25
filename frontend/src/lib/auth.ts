import { setAuthToken } from './api';

/**
 * Authentication for the operator console.
 *
 * Two modes exist and they are deliberately not interchangeable:
 *
 * - **Firebase** is the real path. The browser signs in, receives an ID token,
 *   and the API verifies it against the project. This is the only mode allowed
 *   in staging or production.
 * - **Local development** uses the pre-shared bypass token the API accepts when
 *   `AUTH_DEV_MODE` is on. `Settings._production_hardening` refuses to boot a
 *   production API with that flag set, so this cannot silently become the
 *   production login.
 *
 * The token is mirrored into `localStorage` so a reload does not bounce the
 * operator back to the landing page mid-incident, and into the in-memory client
 * so the very first request after sign-in already carries it.
 */

export const TOKEN_KEY = 'aegis_token';
export const IDENTITY_KEY = 'aegis_identity';

export type AuthMode = 'firebase' | 'local' | 'unconfigured';

export interface Identity {
  uid: string;
  email: string | null;
  displayName: string | null;
  photoURL: string | null;
  mode: AuthMode;
}

export interface FirebaseWebConfig {
  apiKey: string;
  authDomain: string;
  projectId: string;
  storageBucket?: string;
  messagingSenderId?: string;
  appId?: string;
}

/**
 * Read the public Firebase config.
 *
 * `NEXT_PUBLIC_*` values are inlined at build time, so these must be referenced
 * as full literal property accesses - destructuring `process.env` or indexing it
 * dynamically defeats the substitution and yields undefined in the browser.
 */
export function firebaseConfig(): FirebaseWebConfig | null {
  const apiKey = process.env.NEXT_PUBLIC_FIREBASE_API_KEY;
  const authDomain = process.env.NEXT_PUBLIC_FIREBASE_AUTH_DOMAIN;
  const projectId = process.env.NEXT_PUBLIC_FIREBASE_PROJECT_ID;
  if (!apiKey || !authDomain || !projectId) return null;
  return {
    apiKey,
    authDomain,
    projectId,
    storageBucket: process.env.NEXT_PUBLIC_FIREBASE_STORAGE_BUCKET,
    messagingSenderId: process.env.NEXT_PUBLIC_FIREBASE_MESSAGING_SENDER_ID,
    appId: process.env.NEXT_PUBLIC_FIREBASE_APP_ID,
  };
}

/**
 * Whether this build may offer the dev-token sign-in bypass.
 *
 * The bypass must be opted into explicitly. An unset `NEXT_PUBLIC_AEGIS_ENV`
 * previously defaulted to `local`, which meant the one deployment most likely
 * to be misconfigured - a fresh push to a hosting platform with no environment
 * set - was also the one that published a "paste your dev token" form on a
 * public URL. Fail closed instead: absent configuration grants nothing.
 */
export function isLocalEnvironment(): boolean {
  return process.env.NEXT_PUBLIC_AEGIS_ENV === 'local';
}

/** Which sign-in methods this deployment can actually offer. */
export function availableModes(): AuthMode[] {
  const modes: AuthMode[] = [];
  if (firebaseConfig()) modes.push('firebase');
  if (isLocalEnvironment()) modes.push('local');
  return modes.length ? modes : ['unconfigured'];
}

export function storeSession(token: string, identity: Identity): void {
  setAuthToken(token);
  try {
    window.localStorage.setItem(TOKEN_KEY, token);
    window.localStorage.setItem(IDENTITY_KEY, JSON.stringify(identity));
  } catch {
    // Private mode or blocked storage. The in-memory token still works for
    // this tab, so the operator is not locked out - they will simply have to
    // sign in again after a reload.
  }
}

export function clearSession(): void {
  setAuthToken(null);
  try {
    window.localStorage.removeItem(TOKEN_KEY);
    window.localStorage.removeItem(IDENTITY_KEY);
  } catch {
    /* non-fatal */
  }
}

export function readSession(): { token: string | null; identity: Identity | null } {
  if (typeof window === 'undefined') return { token: null, identity: null };
  try {
    const token = window.localStorage.getItem(TOKEN_KEY);
    const raw = window.localStorage.getItem(IDENTITY_KEY);
    const identity = raw ? (JSON.parse(raw) as Identity) : null;
    if (token) setAuthToken(token);
    return { token, identity };
  } catch {
    return { token: null, identity: null };
  }
}

/**
 * Sign in with Firebase.
 *
 * The SDK is imported dynamically so a deployment with no Firebase config never
 * ships or parses it, and the landing page stays fast for the common case where
 * an operator is only reading the marketing content.
 */
export async function signInWithGoogle(): Promise<{ token: string; identity: Identity }> {
  const config = firebaseConfig();
  if (!config) {
    throw new Error('Firebase is not configured for this deployment.');
  }
  const [{ initializeApp, getApps, getApp }, auth] = await Promise.all([
    import('firebase/app'),
    import('firebase/auth'),
  ]);
  const app = getApps().length ? getApp() : initializeApp(config);
  const provider = new auth.GoogleAuthProvider();
  const client = auth.getAuth(app);
  const credential = await auth.signInWithPopup(client, provider);
  const token = await credential.user.getIdToken();
  const identity: Identity = {
    uid: credential.user.uid,
    email: credential.user.email,
    displayName: credential.user.displayName,
    photoURL: credential.user.photoURL,
    mode: 'firebase',
  };
  storeSession(token, identity);
  return { token, identity };
}

export async function signInWithEmail(
  email: string,
  password: string,
): Promise<{ token: string; identity: Identity }> {
  const config = firebaseConfig();
  if (!config) {
    throw new Error('Firebase is not configured for this deployment.');
  }
  const [{ initializeApp, getApps, getApp }, auth] = await Promise.all([
    import('firebase/app'),
    import('firebase/auth'),
  ]);
  const app = getApps().length ? getApp() : initializeApp(config);
  const client = auth.getAuth(app);
  const credential = await auth.signInWithEmailAndPassword(client, email, password);
  const token = await credential.user.getIdToken();
  const identity: Identity = {
    uid: credential.user.uid,
    email: credential.user.email,
    displayName: credential.user.displayName,
    photoURL: credential.user.photoURL,
    mode: 'firebase',
  };
  storeSession(token, identity);
  return { token, identity };
}

/**
 * Local development sign-in.
 *
 * The operator pastes the value of `AUTH_DEV_BYPASS_TOKEN`. It is never bundled
 * into the page: shipping a working credential inside the JavaScript would make
 * the console readable by anyone who can load it, which is exactly the property
 * a bypass must not have.
 */
export async function signInWithDevToken(
  token: string,
): Promise<{ token: string; identity: Identity }> {
  const trimmed = token.trim();
  if (!trimmed) throw new Error('Enter the development access token.');
  if (!isLocalEnvironment()) {
    throw new Error('Development sign-in is only available in the local environment.');
  }
  const identity: Identity = {
    uid: 'dev-user',
    email: 'dev@localhost',
    displayName: 'Local Developer',
    photoURL: null,
    mode: 'local',
  };
  storeSession(trimmed, identity);
  return { token: trimmed, identity };
}

/** Sign out of Firebase too, so the next sign-in is a real one. */
export async function signOut(): Promise<void> {
  const identity = readSession().identity;
  clearSession();
  if (identity?.mode !== 'firebase') return;
  try {
    const [{ getApps, getApp }, auth] = await Promise.all([
      import('firebase/app'),
      import('firebase/auth'),
    ]);
    if (!getApps().length) return;
    await auth.signOut(auth.getAuth(getApp()));
  } catch {
    // The local session is already gone, which is what protects this browser.
    // A failed remote sign-out is worth ignoring rather than blocking on.
  }
}
