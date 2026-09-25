import { afterEach, beforeEach, describe, expect, test, vi } from 'vitest';

import { ApiError, NetworkError, onUnauthorized, request, setAuthToken } from '@/lib/api';

/**
 * The API client is the single door between the console and the control plane.
 * Everything asserted here is a behaviour an operator would notice going wrong
 * during an incident - being signed out mid-triage, a hung backend rendering as
 * a spinner that never resolves, or "unreachable" reading as "nothing found".
 */

/** Subscriptions are module-level state; each test tears down its own. */
const unsubscribes: Array<() => void> = [];

function subscribe(handler: () => void): void {
  unsubscribes.push(onUnauthorized(handler));
}

function jsonResponse(body: unknown, status: number, statusText = ''): Response {
  return new Response(JSON.stringify(body), {
    status,
    statusText,
    headers: { 'Content-Type': 'application/json' },
  });
}

/** Captures what the client actually put on the wire. */
function stubFetch(response: () => Response): { init: () => RequestInit | undefined } {
  let seen: RequestInit | undefined;
  vi.stubGlobal('fetch', (_url: string, init: RequestInit) => {
    seen = init;
    return Promise.resolve(response());
  });
  return { init: () => seen };
}

beforeEach(() => {
  // `authToken` lives in module scope, so a token set by one test would
  // otherwise be sent by the next one.
  setAuthToken(null);
  window.localStorage.clear();
});

afterEach(() => {
  while (unsubscribes.length) unsubscribes.pop()?.();
  vi.useRealTimers();
});

describe('credential rejection', () => {
  test('a 401 tells every subscriber exactly once and still throws ApiError', async () => {
    // The client cannot navigate - it has no router - so the only way an
    // expired token reaches the auth provider is this notification. Losing it
    // leaves the console rendering stale data behind silent failures.
    const first = vi.fn();
    const second = vi.fn();
    subscribe(first);
    subscribe(second);
    stubFetch(() => jsonResponse({ error: { code: 'UNAUTHENTICATED', message: 'Token expired.' } }, 401));

    const error = await request('/v1/incidents').catch((e: unknown) => e);

    expect(error).toBeInstanceOf(ApiError);
    expect((error as ApiError).status).toBe(401);
    expect(first).toHaveBeenCalledTimes(1);
    expect(second).toHaveBeenCalledTimes(1);
  });

  test('a 403 does not end the session', async () => {
    // A 403 means the credential is valid but insufficient. Signing an operator
    // out because they lack one role would be maddening, and worse, it would
    // hide the actual problem behind a login screen.
    const handler = vi.fn();
    subscribe(handler);
    stubFetch(() => jsonResponse({ error: { code: 'FORBIDDEN', message: 'Approver role required.' } }, 403));

    const error = await request('/v1/approvals/x/decide').catch((e: unknown) => e);

    expect(error).toBeInstanceOf(ApiError);
    expect((error as ApiError).status).toBe(403);
    expect(handler).not.toHaveBeenCalled();
  });

  test('one throwing subscriber does not stop the others being told', async () => {
    // Sign-out is a fan-out: a header, a provider and a router may all be
    // listening. If the first to throw suppressed the rest, half the console
    // would carry on believing it is authenticated.
    const broken = vi.fn(() => {
      throw new Error('subscriber exploded');
    });
    const healthy = vi.fn();
    subscribe(broken);
    subscribe(healthy);
    stubFetch(() => jsonResponse({ error: { code: 'UNAUTHENTICATED', message: 'gone' } }, 401));

    await request('/v1/incidents').catch(() => undefined);

    expect(broken).toHaveBeenCalledTimes(1);
    expect(healthy).toHaveBeenCalledTimes(1);
  });
});

describe('transport failure is not a backend answer', () => {
  test('an unreachable backend throws NetworkError rather than ApiError', async () => {
    // The two are rendered very differently. Collapsing them would let "Aegis
    // is unreachable" look like "there are no incidents" - the exact confusion
    // invariant 6 exists to prevent, one layer down.
    vi.stubGlobal('fetch', () => Promise.reject(new TypeError('Failed to fetch')));

    const error = await request('/health').catch((e: unknown) => e);

    expect(error).toBeInstanceOf(NetworkError);
    expect(error).not.toBeInstanceOf(ApiError);
  });

  test('a request that outlives the 20 second budget aborts and surfaces as NetworkError', async () => {
    // Every request is bounded on purpose: a hung backend must become an error
    // state an operator can act on, never a spinner that never resolves.
    vi.useFakeTimers();
    vi.stubGlobal(
      'fetch',
      (_url: string, init: RequestInit) =>
        new Promise<Response>((_resolve, reject) => {
          init.signal?.addEventListener('abort', () => {
            reject(new DOMException('The operation was aborted.', 'AbortError'));
          });
        }),
    );

    const pending = request('/health').catch((e: unknown) => e);
    await vi.advanceTimersByTimeAsync(20_000);
    const error = await pending;

    expect(error).toBeInstanceOf(NetworkError);
    expect((error as NetworkError).message).toContain('20 seconds');
  });

  test('a request inside the budget is not aborted', async () => {
    // The guard against the opposite defect: a timeout set too eagerly would
    // cancel healthy slow queries, which on this console are the interesting
    // ones.
    vi.useFakeTimers();
    vi.stubGlobal(
      'fetch',
      (_url: string, init: RequestInit) =>
        new Promise<Response>((resolve, reject) => {
          init.signal?.addEventListener('abort', () => {
            reject(new DOMException('The operation was aborted.', 'AbortError'));
          });
          setTimeout(() => resolve(jsonResponse({ status: 'ok' }, 200)), 19_000);
        }),
    );

    const pending = request<{ status: string }>('/health');
    await vi.advanceTimersByTimeAsync(19_000);

    await expect(pending).resolves.toEqual({ status: 'ok' });
  });
});

describe('request shape', () => {
  test('the bearer token is attached when a session exists', async () => {
    setAuthToken('token-abc');
    const captured = stubFetch(() => jsonResponse({ ok: true }, 200));

    await request('/v1/policy');

    expect(new Headers(captured.init()?.headers).get('Authorization')).toBe('Bearer token-abc');
  });

  test('no Authorization header is sent when there is no session', async () => {
    // An empty `Bearer ` header is worse than none: it reads as a malformed
    // credential rather than an anonymous request, and the API answers 401
    // where it should answer 401-with-a-reason the sign-in page can use.
    const captured = stubFetch(() => jsonResponse({ ok: true }, 200));

    await request('/health');

    expect(new Headers(captured.init()?.headers).has('Authorization')).toBe(false);
  });

  test('an error body supplies the code and correlation id the audit trail needs', async () => {
    // Without the correlation id an operator cannot join what they saw to what
    // the backend logged, which is the whole premise of an auditable system.
    stubFetch(() =>
      jsonResponse(
        { error: { code: 'POLICY_BLOCKED', message: 'Kill switch engaged.' }, correlation_id: 'corr-42' },
        409,
      ),
    );

    const error = (await request('/v1/policy/kill-switch').catch((e: unknown) => e)) as ApiError;

    expect(error.code).toBe('POLICY_BLOCKED');
    expect(error.message).toBe('Kill switch engaged.');
    expect(error.correlationId).toBe('corr-42');
  });

  test('a non-JSON error body degrades to the status line instead of throwing', async () => {
    // A proxy returning an HTML 502 must still produce a usable ApiError; a
    // parse failure here would surface as an unhandled exception in the UI.
    vi.stubGlobal('fetch', () =>
      Promise.resolve(new Response('<html>Bad Gateway</html>', { status: 502, statusText: 'Bad Gateway' })),
    );

    const error = (await request('/health').catch((e: unknown) => e)) as ApiError;

    expect(error).toBeInstanceOf(ApiError);
    expect(error.status).toBe(502);
    expect(error.code).toBe('UNKNOWN');
  });

  test('a 204 resolves without trying to parse an absent body', async () => {
    vi.stubGlobal('fetch', () => Promise.resolve(new Response(null, { status: 204 })));

    await expect(request('/v1/policy/kill-switch')).resolves.toBeUndefined();
  });
});
