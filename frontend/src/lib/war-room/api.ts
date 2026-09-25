import { apiBaseUrl, getAuthToken, request } from '@/lib/api';
import { consoleApi } from '@/lib/console-api';
import type {
  ContextPoint, ControlResponse, HealthPayload, RawTreeQuery, SeqEvent, WarRoomState,
} from './types';

/**
 * War-room endpoints. Reads and demo controls go through the shared `request`
 * helper so timeouts, error typing and 401 handling match the rest of the
 * console; approvals reuse the console's own approval call rather than a copy.
 */

function incidentQuery(incidentId: string | null | undefined, extra = ''): string {
  const params = new URLSearchParams();
  if (incidentId) params.set('incident_id', incidentId);
  const rendered = params.toString();
  if (!rendered && !extra) return '';
  return `?${[rendered, extra].filter(Boolean).join('&')}`;
}

export const STREAM_PATH = '/v1/war-room/stream';

export const warRoomApi = {
  state: () => request<WarRoomState>('/v1/war-room/state'),

  events: (incidentId?: string | null, after = 0, limit = 500) =>
    request<{ events: SeqEvent[] }>(
      `/v1/war-room/events${incidentQuery(incidentId, `after=${after}&limit=${limit}`)}`,
    ),

  contextSeries: (incidentId?: string | null) =>
    request<{ points: ContextPoint[] }>(`/v1/war-room/context-series${incidentQuery(incidentId)}`),

  health: () => request<HealthPayload>('/v1/war-room/health'),

  rawtreeQueries: (incidentId?: string | null) =>
    request<{ queries: RawTreeQuery[] }>(`/v1/war-room/rawtree-queries${incidentQuery(incidentId)}`),

  inject: (scenario = 'INC-043') =>
    request<ControlResponse>('/v1/war-room/inject', {
      method: 'POST',
      body: JSON.stringify({ scenario }),
    }),

  killWorker: () => request<ControlResponse>('/v1/war-room/kill-worker', { method: 'POST' }),

  reset: () => request<ControlResponse>('/v1/war-room/reset', { method: 'POST' }),

  approve: (approvalId: string) =>
    consoleApi.decideApproval(approvalId, 'approved', 'Approved from the war room.'),

  deny: (approvalId: string) =>
    consoleApi.decideApproval(approvalId, 'rejected', 'Denied from the war room.'),
};

/**
 * The FLUX incident map for a memory card.
 *
 * Fetched as a blob because an `<img src>` cannot carry the bearer token the
 * API requires. The caller owns the returned object URL and must revoke it.
 */
export async function fetchIncidentMap(cardId: string, signal?: AbortSignal): Promise<string> {
  const headers = new Headers();
  const token = getAuthToken();
  if (token) headers.set('Authorization', `Bearer ${token}`);
  const response = await fetch(
    `${apiBaseUrl()}/v1/war-room/incident-maps/${encodeURIComponent(cardId)}`,
    { headers, signal, cache: 'no-store' },
  );
  if (!response.ok) throw new Error(`The incident map answered ${response.status}.`);
  const blob = await response.blob();
  return URL.createObjectURL(blob);
}
