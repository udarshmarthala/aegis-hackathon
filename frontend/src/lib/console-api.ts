import { request } from './api';
import type {
  ActionDetail, ActionSummary, AgentRunRow, ApprovalDecision, AuditEntry,
  Availability, AutonomyPosture, BlastRadiusView, CalibrationView, DeploymentRow,
  EvaluationRunRow, EvidenceGapRow, InstanceRow, IntegrationRow,
  InvestigationRow, Neighbourhood, OperatorTask, PatchRow, PendingApproval,
  RecurringPattern, ReliabilitySummary, SandboxRunRow, ScenarioResultRow,
  ServiceLoadRow, ServiceMetrics, ServiceRow, ToolCallRow,
} from './console-types';

/**
 * Console endpoints.
 *
 * Split from `api.ts` so the incident surfaces and the operational surfaces can
 * each be read in one sitting. Both go through the same `request` helper, so
 * timeouts, error typing and the 401 handling are identical everywhere - a
 * second fetch wrapper is how one surface quietly stops signing its requests.
 */

function qs(params: Record<string, string | number | boolean | undefined | null>): string {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value === undefined || value === null || value === '') continue;
    search.set(key, String(value));
  }
  const rendered = search.toString();
  return rendered ? `?${rendered}` : '';
}

export interface Listed<T> {
  items: T[];
  count: number;
}

export const consoleApi = {
  listActions: (params: { incident_id?: string; state?: string; limit?: number } = {}) =>
    request<Listed<ActionSummary>>(`/v1/actions${qs(params)}`),

  getAction: (id: string) => request<ActionDetail>(`/v1/actions/${id}`),

  actionLeases: (id: string) =>
    request<{ resource_id: string; active_leases: Array<Record<string, unknown>> }>(
      `/v1/actions/${id}/leases`,
    ),

  pendingApprovals: (limit = 50) =>
    request<Listed<PendingApproval>>(`/v1/approvals${qs({ limit })}`),

  decideApproval: (approvalId: string, decision: ApprovalDecision, note = '') =>
    request<{ id: string; action_id: string; decision: string; work_enqueued: boolean }>(
      `/v1/approvals/${approvalId}/decide`,
      { method: 'POST', body: JSON.stringify({ decision, note }) },
    ),

  services: () => request<Availability<Listed<ServiceRow>>>('/v1/systems/services'),

  instances: (serviceId: string) =>
    request<Availability<Listed<InstanceRow>>>(
      `/v1/systems/services/${encodeURIComponent(serviceId)}/instances`,
    ),

  serviceMetrics: (serviceId: string, windowSeconds = 3600) =>
    request<ServiceMetrics>(
      `/v1/systems/services/${encodeURIComponent(serviceId)}/metrics${qs({ window_s: windowSeconds })}`,
    ),

  environmentHealth: () =>
    request<Availability<{ total: number; by_health: Record<string, number>; degraded: string[] }>>(
      '/v1/systems/health',
    ),

  neighbourhood: (serviceId: string, depth = 2) =>
    request<Availability<Neighbourhood>>(
      `/v1/graph/neighbourhood${qs({ service_id: serviceId, depth })}`,
    ),

  blastRadius: (serviceId: string, maxDepth = 3) =>
    request<Availability<BlastRadiusView>>(
      `/v1/graph/blast-radius${qs({ service_id: serviceId, max_depth: maxDepth })}`,
    ),

  causalPaths: (from: string, to: string, maxDepth = 5) =>
    request<Availability<{ paths: string[][]; count: number }>>(
      `/v1/graph/causal-paths${qs({ from_service: from, to_service: to, max_depth: maxDepth })}`,
    ),

  dependencies: (serviceId: string, maxDepth = 3) =>
    request<
      Availability<{ service_id: string; upstream: unknown[]; sharing_a_dependency: unknown[] }>
    >(`/v1/graph/dependencies${qs({ service_id: serviceId, max_depth: maxDepth })}`),

  refreshTopology: () => request<Record<string, unknown>>('/v1/graph/refresh', { method: 'POST' }),

  investigations: (limit = 50) =>
    request<Listed<InvestigationRow>>(`/v1/investigations${qs({ limit })}`),

  agentRuns: (incidentId: string) =>
    request<Listed<AgentRunRow> & { incident_id: string }>(
      `/v1/investigations/${incidentId}/runs`,
    ),

  toolCalls: (incidentId: string, limit = 200) =>
    request<Listed<ToolCallRow> & { incident_id: string }>(
      `/v1/investigations/${incidentId}/tools${qs({ limit })}`,
    ),

  evidenceGaps: (incidentId: string) =>
    request<Listed<EvidenceGapRow> & { incident_id: string; usable_evidence_count: number }>(
      `/v1/investigations/${incidentId}/gaps`,
    ),

  reliabilitySummary: (days = 7) =>
    request<ReliabilitySummary>(`/v1/reliability/summary${qs({ days })}`),

  recurringFailures: (days = 90, minOccurrences = 2) =>
    request<Availability<Listed<RecurringPattern> & { window_days: number }>>(
      `/v1/reliability/recurring${qs({ days, min_occurrences: minOccurrences })}`,
    ),

  serviceLoad: (days = 30) =>
    request<{ window_days: number; items: ServiceLoadRow[] }>(
      `/v1/reliability/services${qs({ days })}`,
    ),

  deployments: (params: { environment?: string; service_id?: string; limit?: number } = {}) =>
    request<Listed<DeploymentRow>>(`/v1/deployments${qs(params)}`),

  patches: (params: { incident_id?: string; limit?: number } = {}) =>
    request<Listed<PatchRow>>(`/v1/deployments/patches${qs(params)}`),

  patchDiff: (patchId: string) =>
    request<{
      found: boolean; id?: string; repo?: string; base_ref?: string;
      diff?: string; diff_sha256?: string; state?: string;
    }>(`/v1/deployments/patches/${patchId}/diff`),

  sandboxRuns: (params: { incident_id?: string; limit?: number } = {}) =>
    request<Listed<SandboxRunRow>>(`/v1/deployments/sandbox-runs${qs(params)}`),

  environments: () =>
    request<{
      current: string; adapter: string; runtime_available: boolean; is_production: boolean;
    }>('/v1/deployments/environments'),

  audit: (
    params: {
      event_type?: string; actor_type?: string; correlation_id?: string; limit?: number;
    } = {},
  ) => request<Listed<AuditEntry> & { write_failures: number }>(`/v1/audit${qs(params)}`),

  incidentAudit: (incidentId: string, limit = 500) =>
    request<
      Listed<AuditEntry> & {
        incident_id: string; human_decisions: number; autonomous_actions: number;
      }
    >(`/v1/audit/incident/${incidentId}${qs({ limit })}`),

  tasks: (limit = 50) =>
    request<Listed<OperatorTask> & { operator: string }>(`/v1/tasks${qs({ limit })}`),

  integrations: () =>
    request<
      Listed<IntegrationRow> & {
        circuit_breakers: Record<string, string>; audit_write_failures: number;
      }
    >('/v1/integrations'),

  autonomy: () => request<AutonomyPosture>('/v1/integrations/autonomy'),

  evaluationRuns: (limit = 25) =>
    request<Listed<EvaluationRunRow>>(`/v1/evaluation/runs${qs({ limit })}`),

  evaluationRun: (runId: string) =>
    request<EvaluationRunRow & Record<string, unknown>>(`/v1/evaluation/runs/${runId}`),

  evaluationScenarioResults: (runId: string, limit = 200) =>
    request<Listed<ScenarioResultRow>>(`/v1/evaluation/runs/${runId}/scenarios${qs({ limit })}`),

  /** Always present, even when empty - an omitted unsafe list reads as "none". */
  evaluationUnsafe: (runId: string) =>
    request<Listed<ScenarioResultRow>>(`/v1/evaluation/runs/${runId}/unsafe`),

  evaluationCalibration: (runId: string) =>
    request<CalibrationView>(`/v1/evaluation/runs/${runId}/calibration`),

  evaluationCatalogue: () =>
    request<
      Listed<{ id: string; title: string; category: string; workload: string; difficulty: string }>
    >('/v1/evaluation/scenarios'),

  evaluationCompare: (base: string, candidate: string) =>
    request<{ deltas: Record<string, number | null>; safety_regressed: boolean }>(
      `/v1/evaluation/compare${qs({ base, candidate })}`,
    ),
};
