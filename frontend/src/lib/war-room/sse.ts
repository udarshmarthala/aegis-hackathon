import { apiBaseUrl, getAuthToken, notifyUnauthorized } from '@/lib/api';

/**
 * The war-room stream client.
 *
 * `EventSource` is not used because it cannot send headers: the API
 * authenticates every request from the `Authorization` header alone, so a
 * native event source would be refused, and putting the bearer token in the
 * query string would write it into every proxy and access log on the path.
 * A `fetch` body read incrementally gives the same protocol with the header
 * intact, and lets reconnection be ours: bounded exponential backoff with
 * jitter, resuming from `Last-Event-ID` so the server replays only what was
 * missed.
 */

export interface SseFrame {
  id: string | null;
  event: string;
  data: string;
}

/**
 * Incremental parser for `text/event-stream`.
 *
 * Returns the complete frames in `buffer` and the unconsumed tail, which the
 * caller prepends to the next chunk. Kept pure so the framing rules - CRLF,
 * multi-line data, comments, a frame split across reads - are unit-tested
 * rather than discovered on stage.
 */
export function parseSse(buffer: string): { frames: SseFrame[]; rest: string } {
  const normalised = buffer.replace(/\r\n?/g, '\n');
  const blocks = normalised.split('\n\n');
  const rest = blocks.pop() ?? '';
  const frames: SseFrame[] = [];
  for (const block of blocks) {
    let id: string | null = null;
    let event = 'message';
    const data: string[] = [];
    for (const line of block.split('\n')) {
      if (!line || line.startsWith(':')) continue;
      const colon = line.indexOf(':');
      const field = colon === -1 ? line : line.slice(0, colon);
      let value = colon === -1 ? '' : line.slice(colon + 1);
      if (value.startsWith(' ')) value = value.slice(1);
      if (field === 'id') id = value;
      else if (field === 'event') event = value || 'message';
      else if (field === 'data') data.push(value);
    }
    if (data.length === 0 && id === null) continue;
    frames.push({ id, event, data: data.join('\n') });
  }
  return { frames, rest };
}

/** Backoff for the nth consecutive failure: 1 s, 2 s, 4 s … capped at 15 s, with ±20 % jitter. */
export function backoffMs(attempt: number, random: () => number = Math.random): number {
  const base = Math.min(15_000, 1_000 * 2 ** Math.max(0, attempt));
  const jitter = base * 0.2 * (random() * 2 - 1);
  return Math.round(base + jitter);
}

export type StreamStatus = 'connecting' | 'live' | 'reconnecting' | 'offline';

export interface StreamOptions {
  path: string;
  onFrame: (frame: SseFrame) => void;
  onStatus: (status: StreamStatus, detail?: string) => void;
  /** Resume point for the first connection (the initial reads' high-water mark). */
  lastEventId?: string | null;
  /** A connection that stays silent this long is treated as dead (server pings every 15 s). */
  idleTimeoutMs?: number;
}

/** Open the stream. Returns a function that closes it for good. */
export function openStream(options: StreamOptions): () => void {
  const idleTimeoutMs = options.idleTimeoutMs ?? 45_000;
  let lastEventId = options.lastEventId ?? null;
  let closed = false;
  let attempt = 0;
  let controller: AbortController | null = null;
  let retryTimer: ReturnType<typeof setTimeout> | null = null;

  const scheduleRetry = (detail: string) => {
    if (closed) return;
    options.onStatus('reconnecting', detail);
    const delay = backoffMs(attempt);
    attempt += 1;
    retryTimer = setTimeout(() => void connect(), delay);
  };

  const connect = async () => {
    if (closed) return;
    controller = new AbortController();
    const signal = controller.signal;
    let idleTimer: ReturnType<typeof setTimeout> | null = null;
    const armIdle = () => {
      if (idleTimer) clearTimeout(idleTimer);
      idleTimer = setTimeout(() => controller?.abort(), idleTimeoutMs);
    };

    options.onStatus(attempt === 0 ? 'connecting' : 'reconnecting');
    const headers = new Headers({ Accept: 'text/event-stream' });
    const token = getAuthToken();
    if (token) headers.set('Authorization', `Bearer ${token}`);
    if (lastEventId) headers.set('Last-Event-ID', lastEventId);

    try {
      armIdle();
      const response = await fetch(`${apiBaseUrl()}${options.path}`, {
        headers,
        signal,
        cache: 'no-store',
      });
      if (response.status === 401) {
        // A rejected credential will not be accepted on retry; end the session
        // the same way every other request does, and stop.
        closed = true;
        notifyUnauthorized();
        options.onStatus('offline', 'Session expired - sign in again.');
        return;
      }
      if (!response.ok || !response.body) {
        scheduleRetry(`Stream answered ${response.status}.`);
        return;
      }

      attempt = 0;
      options.onStatus('live');
      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = '';
      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        armIdle();
        buffer += decoder.decode(value, { stream: true });
        const parsed = parseSse(buffer);
        buffer = parsed.rest;
        for (const frame of parsed.frames) {
          if (frame.id) lastEventId = frame.id;
          options.onFrame(frame);
        }
      }
      scheduleRetry('The stream ended.');
    } catch (_err) {
      scheduleRetry(signal.aborted && !closed ? 'The stream went silent.' : 'Aegis is unreachable.');
    } finally {
      if (idleTimer) clearTimeout(idleTimer);
    }
  };

  void connect();

  return () => {
    closed = true;
    if (retryTimer) clearTimeout(retryTimer);
    controller?.abort();
  };
}
