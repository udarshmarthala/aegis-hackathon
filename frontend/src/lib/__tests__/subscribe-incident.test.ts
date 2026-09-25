import { afterEach, beforeEach, describe, expect, test, vi } from 'vitest';

import { onUnauthorized, setAuthToken, subscribeIncident } from '@/lib/api';

/**
 * The incident page's live stream.
 *
 * It used a native `EventSource` with the token in the query string, which the
 * API never reads, so every deployment answered 401 and the page silently fell
 * back to stale data. These tests pin the replacement: the credential travels
 * in the `Authorization` header and never in the URL, and reconnection still
 * resumes from `Last-Event-ID` the way `EventSource` did.
 */

interface Call {
  url: string;
  headers: Headers;
  signal: AbortSignal;
}

const cleanups: Array<() => void> = [];

function sse(...frames: string[]): Response {
  return new Response(frames.join(''), {
    status: 200,
    headers: { 'Content-Type': 'text/event-stream' },
  });
}

/** A connection that stays open until it is aborted. */
function hanging(signal: AbortSignal): Promise<Response> {
  return new Promise((_resolve, reject) => {
    signal.addEventListener('abort', () => reject(new DOMException('aborted', 'AbortError')));
  });
}

/** Serves the queued responses in order, then holds the connection open. */
function stubFetch(...responses: Array<() => Response>): Call[] {
  const calls: Call[] = [];
  vi.stubGlobal('fetch', (url: string, init: RequestInit) => {
    const signal = init.signal as AbortSignal;
    calls.push({ url, headers: new Headers(init.headers), signal });
    const next = responses.shift();
    return next ? Promise.resolve(next()) : hanging(signal);
  });
  return calls;
}

/** The nth connection attempt, failing loudly rather than reading undefined. */
function nth(calls: Call[], index: number): Call {
  const call = calls[index];
  if (!call) throw new Error(`no connection attempt #${index}`);
  return call;
}

function subscribe(onEvent: (type: string, data: unknown) => void): void {
  cleanups.push(subscribeIncident('inc_01LIVE', onEvent));
}

beforeEach(() => {
  setAuthToken(null);
  window.localStorage.clear();
});

afterEach(() => {
  while (cleanups.length) cleanups.pop()?.();
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe('subscribeIncident', () => {
  test('sends the bearer token as a header and never in the URL', async () => {
    setAuthToken('tok_secret');
    const calls = stubFetch();

    subscribe(() => {});

    await vi.waitFor(() => expect(calls).toHaveLength(1));
    const call = nth(calls, 0);
    expect(call.url).toMatch(/\/v1\/incidents\/inc_01LIVE\/stream$/);
    expect(call.url).not.toContain('tok_secret');
    expect(call.url).not.toContain('access_token');
    expect(call.headers.get('Authorization')).toBe('Bearer tok_secret');
    expect(call.headers.get('Accept')).toBe('text/event-stream');
  });

  test('delivers incident events as parsed JSON and ignores pings and unknown events', async () => {
    stubFetch(() =>
      sse(
        ': keepalive\n\n',
        'id: 1\nevent: snapshot\ndata: {"state":"INVESTIGATING"}\n\n',
        'event: ping\ndata: {}\n\n',
        'id: 2\nevent: something_new\ndata: {"x":1}\n\n',
        'id: 3\nevent: evidence\ndata: {"id":"ev_1"}\n\n',
      ),
    );
    const seen: Array<[string, unknown]> = [];

    subscribe((type, data) => seen.push([type, data]));

    await vi.waitFor(() => expect(seen).toHaveLength(2));
    expect(seen).toEqual([
      ['snapshot', { state: 'INVESTIGATING' }],
      ['evidence', { id: 'ev_1' }],
    ]);
  });

  test('a malformed frame or a throwing subscriber does not drop the connection', async () => {
    const calls = stubFetch(() =>
      sse(
        'id: 1\nevent: phase\ndata: {not json\n\n',
        'id: 2\nevent: phase\ndata: {"phase":"boom"}\n\n',
        'id: 3\nevent: diagnosis\ndata: {"ok":true}\n\n',
      ),
    );
    const seen: string[] = [];

    subscribe((type, data) => {
      if ((data as { phase?: string }).phase === 'boom') throw new Error('subscriber exploded');
      seen.push(type);
    });

    await vi.waitFor(() => expect(seen).toEqual(['diagnosis']));
    // Only the one connection: nothing above was treated as a transport failure.
    expect(calls).toHaveLength(1);
  });

  test('reconnects with backoff and resumes from Last-Event-ID', async () => {
    vi.useFakeTimers();
    const calls = stubFetch(() => sse('id: 7\nevent: phase\ndata: {"phase":"triage"}\n\n'));
    const seen: string[] = [];

    subscribe((type) => seen.push(type));

    await vi.waitFor(() => expect(seen).toEqual(['phase']));
    // The body ended; the next attempt waits out the first backoff step.
    await vi.advanceTimersByTimeAsync(1_500);
    await vi.waitFor(() => expect(calls).toHaveLength(2));
    expect(nth(calls, 0).headers.get('Last-Event-ID')).toBeNull();
    expect(nth(calls, 1).headers.get('Last-Event-ID')).toBe('7');
  });

  test('a rejected credential ends the session once and is not retried', async () => {
    vi.useFakeTimers();
    const calls = stubFetch(() => new Response('', { status: 401 }));
    const unauthorized = vi.fn();
    cleanups.push(onUnauthorized(unauthorized));

    subscribe(() => {});

    await vi.waitFor(() => expect(unauthorized).toHaveBeenCalledTimes(1));
    await vi.advanceTimersByTimeAsync(60_000);
    expect(calls).toHaveLength(1);
  });

  test('the cleanup aborts the connection and stops reconnection', async () => {
    vi.useFakeTimers();
    const calls = stubFetch();

    const stop = subscribeIncident('inc_01LIVE', () => {});
    await vi.waitFor(() => expect(calls).toHaveLength(1));
    stop();

    expect(nth(calls, 0).signal.aborted).toBe(true);
    await vi.advanceTimersByTimeAsync(60_000);
    expect(calls).toHaveLength(1);
  });
});
