import type {
  Diagnosis, EvidenceResponse, Health, HypothesesResponse,
  Incident, IncidentList, PolicyView, TimelineResponse,
} from './types';
// A deliberate import cycle: `war-room/sse` reads the token and the base URL
// from this module. Neither side touches the other at load time, only inside
// functions, so evaluation order cannot leave a binding undefined when used.
import { openStream } from './war-room/sse';

/**
 * API client.
 *
 * Two error shapes are distinguished deliberately, because the UI renders them
 * very differently: `ApiError` means the backend answered and said no;
 * `NetworkError` means we could not reach it at all. Collapsing them would let
 * "Aegis is unreachable" look like "there are no incidents".
 */
export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
    message: string,
    readonly correlationId?: string,
  ) {
    super(message);
    this.name = 'ApiError';
  }
}

export class NetworkError extends Error {
  constructor(message: string) {
    super(message);
    this.name = 'NetworkError';
  }
}

function baseUrl(): string {
  // The browser always uses the published origin.
  const published = process.env.NEXT_PUBLIC_API_BASE_URL ?? 'http://localhost:8000';
  if (typeof window !== 'undefined') return published;

  // Server-side, prefer a private address when the deployment has one - compose
  // and ECS both put the API on an internal network that is cheaper and safer to
  // reach than going back out through the public origin. Falling back to the
  // published origin rather than to a compose hostname matters: `http://api:8000`
  // baked in as the default resolves to nothing on a platform that has no such
  // network, and surfaces as an opaque 500 rather than a reachability error.
  return process.env.AEGIS_API_INTERNAL_URL ?? published;
}

/**
 * The API origin, for callers that must build a URL themselves (streams and
 * authenticated image fetches). Everything else goes through `request`.
 */
export function apiBaseUrl(): string {
  return baseUrl();
}

let authToken: string | null = null;

export function setAuthToken(token: string | null) {
  authToken = token;
}

/**
 * Subscribers notified when the API rejects our credentials.
 *
 * The API client cannot navigate - it has no router - so it publishes the fact
 * and the auth provider decides what to do. Without this, an expired token
 * leaves the console rendering stale data behind a wall of silent failures,
 * which reads to an operator as "Aegis is broken" rather than "sign in again".
 */
type UnauthorizedHandler = () => void;
const unauthorizedHandlers = new Set<UnauthorizedHandler>();

export function onUnauthorized(handler: UnauthorizedHandler): () => void {
  unauthorizedHandlers.add(handler);
  return () => {
    unauthorizedHandlers.delete(handler);
  };
}

/**
 * Exported for the streaming clients, which cannot go through `request`: a
 * long-lived SSE body is read incrementally, so it needs its own fetch, but a
 * rejected credential there must end the session exactly as it would here.
 */
export function notifyUnauthorized() {
  for (const handler of unauthorizedHandlers) {
    try {
      handler();
    } catch {
      // One bad subscriber must not stop the others being told.
    }
  }
}

export function getAuthToken(): string | null {
  if (authToken) return authToken;
  if (typeof window !== 'undefined') {
    return window.localStorage.getItem('aegis_token');
  }
  return null;
}

/**
 * The single fetch path for the whole console.
 *
 * Exported so `console-api.ts` builds on it rather than writing a second
 * wrapper: a parallel implementation is how one surface quietly stops sending
 * the bearer token, or stops honouring the timeout, without anyone noticing
 * until an incident.
 */
export async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const token = getAuthToken();
  const headers = new Headers(init.headers);
  headers.set('Content-Type', 'application/json');
  if (token) headers.set('Authorization', `Bearer ${token}`);

  // Every request is bounded. A hung backend must surface as an error state in
  // the UI, not as a spinner that never resolves.
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 20_000);

  let response: Response;
  try {
    response = await fetch(`${baseUrl()}${path}`, {
      ...init,
      headers,
      signal: controller.signal,
      cache: 'no-store',
    });
  } catch (err) {
    if (err instanceof DOMException && err.name === 'AbortError') {
      throw new NetworkError('Aegis did not respond within 20 seconds.');
    }
    throw new NetworkError('Aegis is unreachable.');
  } finally {
    clearTimeout(timeout);
  }

  if (!response.ok) {
    let code = 'UNKNOWN';
    let message = response.statusText;
    let correlationId: string | undefined;
    try {
      const body = await response.json();
      code = body?.error?.code ?? code;
      message = body?.error?.message ?? message;
      correlationId = body?.correlation_id;
    } catch {
      // Body was not JSON; the status line is all we have.
    }
    // 401 means the credential is gone or expired; 403 means it is valid but
    // insufficient. Only the first should end the session - signing a user out
    // because they lack one role would be maddening.
    if (response.status === 401) notifyUnauthorized();
    throw new ApiError(response.status, code, message, correlationId);
  }

  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

export const api = {
  health: () => request<Health>('/health'),
  policy: () => request<PolicyView>('/v1/policy'),
  actionRegistry: () => request<{ items: Array<Record<string, unknown>> }>('/v1/policy/actions'),

  listIncidents: (params: {
    state?: string[]; severity?: string[]; environment?: string;
    limit?: number; offset?: number;
  } = {}) => {
    const q = new URLSearchParams();
    params.state?.forEach((s) => q.append('state', s));
    params.severity?.forEach((s) => q.append('severity', s));
    if (params.environment) q.set('environment', params.environment);
    if (params.limit) q.set('limit', String(params.limit));
    if (params.offset) q.set('offset', String(params.offset));
    const qs = q.toString();
    return request<IncidentList>(`/v1/incidents${qs ? `?${qs}` : ''}`);
  },

  getIncident: (id: string) => request<Incident>(`/v1/incidents/${id}`),
  getEvidence: (id: string) => request<EvidenceResponse>(`/v1/incidents/${id}/evidence`),
  getTimeline: (id: string) => request<TimelineResponse>(`/v1/incidents/${id}/timeline`),
  getHypotheses: (id: string) => request<HypothesesResponse>(`/v1/incidents/${id}/hypotheses`),
  getDiagnosis: (id: string) => request<Diagnosis | null>(`/v1/incidents/${id}/diagnosis`),

  reinvestigate: (id: string, reason = '') =>
    request<{ scheduled: boolean; reason: string }>(`/v1/incidents/${id}/reinvestigate`, {
      method: 'POST',
      body: JSON.stringify({ reason }),
    }),

  resolve: (id: string, reason: string) =>
    request<Incident>(`/v1/incidents/${id}/resolve`, {
      method: 'POST',
      body: JSON.stringify({ reason }),
    }),

  engageKillSwitch: (scope: string, target: string, reason: string) =>
    request<{ engaged: boolean }>('/v1/policy/kill-switch', {
      method: 'POST',
      body: JSON.stringify({ scope, target, reason }),
    }),

  releaseKillSwitch: (scope: string, target = '') =>
    request<{ engaged: boolean }>(
      `/v1/policy/kill-switch?scope=${encodeURIComponent(scope)}&target=${encodeURIComponent(target)}`,
      { method: 'DELETE' },
    ),
};

/** The incident-stream events a caller is told about; `ping` and the rest are not. */
const INCIDENT_EVENT_TYPES: ReadonlySet<string> = new Set([
  'snapshot', 'phase', 'evidence', 'hypotheses', 'diagnosis',
  'action_proposed', 'finished', 'update',
]);

/**
 * Live incident stream. Returns a cleanup function.
 *
 * Built on the war room's fetch-based client rather than a native
 * `EventSource`, which cannot send headers. The API authenticates from the
 * `Authorization` header alone, so an event source was refused with 401 on
 * every deployment - and the fix is not a query-string token, which would be
 * written into every proxy and access log on the path. The shared client also
 * keeps what `EventSource` gave for free: reconnection with backoff, resuming
 * from `Last-Event-ID`, and a 401 ending the session like any other request.
 */
export function subscribeIncident(
  incidentId: string,
  onEvent: (type: string, data: unknown) => void,
): () => void {
  return openStream({
    path: `/v1/incidents/${encodeURIComponent(incidentId)}/stream`,
    onFrame: (frame) => {
      if (!INCIDENT_EVENT_TYPES.has(frame.event)) return;
      try {
        onEvent(frame.event, JSON.parse(frame.data));
      } catch {
        // A malformed frame - or a throwing subscriber - is dropped here. Left
        // to reach the stream reader, it would tear down a healthy connection.
      }
    },
    // Connection state is not part of this function's contract; the page
    // reconciles from its queries whether or not the stream is live.
    onStatus: () => {},
  });
}
