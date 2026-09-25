/**
 * Response shapes for the operator console surfaces.
 *
 * Two conventions run through every type here and both exist to stop the UI
 * telling a comfortable lie:
 *
 * - `available: false` plus a `reason` means Aegis could not consult the source.
 *   It is never rendered as an empty list, because "no services" and "I cannot
 *   see your environment" lead an operator to opposite actions.
 * - `degraded` means the answer is real but partial. A degraded search that
 *   rendered as a complete one would hide the fact that half the corpus was
 *   unreachable.
 */

export interface Unavailable {
  available: false;
  reason: string;
}

export type Availability<T> = (T & { available: true; reason: string }) | Unavailable;

export function isAvailable<T>(value: Availability<T>): value is T & {
  available: true;
  reason: string;
} {
  return value.available;
}

/* ---------------------------------------------------------------- actions -- */

export interface ActionSummary {
  id: string;
  incident_id: string;
  action_type: string;
  state: string;
  risk_tier: number;
  executable: boolean;
  resource_id: string;
  service_id: string | null;
  environment: string;
  reason: string;
  created_at: string;
  executed_at: string | null;
  completed_at: string | null;
  error: string | null;
}

export interface GateResultView {
  gate: string;
  passed: boolean;
  reason?: string;
}

export interface PolicyDecisionView {
  effect: 'ALLOW' | 'REQUIRE_HUMAN' | 'BLOCK';
  risk_tier: number;
  matched_rule: string;
  reasons: string[];
  gates: GateResultView[];
  policy_version: string;
  context_snapshot: Record<string, unknown>;
  decided_at: string;
}

export interface VerificationClaimView {
  claim: string;
  test_kind: string;
  outcome: 'PASS' | 'FAIL' | 'INCONCLUSIVE' | 'UNAVAILABLE';
  before_value: number | null;
  after_value: number | null;
  threshold: number | null;
  detail: string;
  observed_at: string;
}

export interface VerificationRunView {
  id: string;
  kind: string;
  passed: boolean;
  verdict: string;
  notes: string;
  started_at: string;
  completed_at: string;
}

export interface ActionDetail {
  action: ActionSummary;
  arguments: Record<string, unknown>;
  supporting_evidence: string[];
  expected_effect: Record<string, unknown>;
  blast_radius: Record<string, unknown>;
  rollback_plan: Record<string, unknown> | null;
  verification_plan: Record<string, unknown>;
  idempotency_key: string;
  result: Record<string, unknown> | null;
  policy_decisions: PolicyDecisionView[];
  verifications: VerificationRunView[];
  verification_claims: VerificationClaimView[];
  approval: {
    id: string;
    state: string;
    decided_by?: string | null;
    decided_at?: string | null;
    note?: string;
    expires_at?: string;
  } | null;
  audit: AuditEntry[];
}

/* -------------------------------------------------------------- approvals -- */

export interface PendingApproval {
  approval_id: string;
  action_id: string;
  incident_id: string;
  requested_at: string;
  expires_at: string;
  action: {
    type: string;
    risk_tier: number;
    resource_type: string;
    resource_id: string;
    service_id: string | null;
    environment: string;
    reason: string;
  };
  incident: { title: string; severity: string; confidence: number | null };
  diagnosis: { statement: string | null; confidence: number | null };
  blast_radius: Record<string, unknown>;
  rollback_plan: Record<string, unknown> | null;
  verification_plan: Record<string, unknown>;
  expected_effect: Record<string, unknown>;
  supporting_evidence: string[];
}

export type ApprovalDecision = 'approved' | 'rejected' | 'more_evidence';

/* ---------------------------------------------------------------- systems -- */

export interface ServiceRow {
  service_id: string;
  name: string;
  environment: string;
  workload: string;
  health: 'healthy' | 'degraded' | 'critical' | 'unknown';
  version: string | null;
  desired_instances: number;
  ready_instances: number;
  degraded: boolean;
  error_rate: number | null;
  latency_p99_ms: number | null;
  owner_team: string | null;
}

export interface InstanceRow {
  instance_id: string;
  name: string;
  state: string;
  health: string;
  image: string | null;
  version: string | null;
  restart_count: number;
  started_at: string | null;
}

export interface MetricSignal {
  available: boolean;
  reason?: string;
  points?: Array<{ t: number; v: number }>;
  latest?: number | null;
  empty?: boolean;
}

export interface ServiceMetrics {
  service_id: string;
  window_s: number;
  signals: Record<'error_rate' | 'latency_p99' | 'request_rate', MetricSignal>;
}

/* ------------------------------------------------------------------ graph -- */

export interface GraphNode {
  id: string;
  label: string;
  kind?: string;
  environment?: string;
  [key: string]: unknown;
}

export interface GraphEdge {
  source: string;
  target: string;
  type: string;
  [key: string]: unknown;
}

export interface Neighbourhood {
  nodes: GraphNode[];
  edges: GraphEdge[];
  root: string;
  depth: number;
}

export interface BlastRadiusView {
  service_id: string;
  directly_affected: string[];
  downstream: string[];
  customer_facing: boolean;
  size: number;
  estimated_request_share: number;
}

/* --------------------------------------------------------- investigations -- */

export interface InvestigationRow {
  incident_id: string;
  title: string;
  severity: string;
  state: string;
  environment: string;
  confidence: number | null;
  agent_runs: number;
  failed_runs: number;
  total_duration_ms: number;
  created_at: string;
  last_activity: string | null;
}

export interface AgentRunRow {
  id: string;
  agent_role: string;
  status: string;
  model: string | null;
  provider: string | null;
  prompt_version: string | null;
  task: string;
  summary: string;
  evidence_ids: string[];
  duration_ms: number | null;
  error: string | null;
  started_at: string | null;
  finished_at: string | null;
}

export interface ToolCallRow {
  id: string;
  agent_run_id: string | null;
  tool_name: string;
  status: string;
  duration_ms: number | null;
  arguments: Record<string, unknown> | null;
  error: string | null;
  created_at: string;
}

export interface EvidenceGapRow {
  id: string;
  source: string;
  source_type: string;
  summary: string;
  reason: string;
  retrieved_at: string;
}

/* ------------------------------------------------------------ reliability -- */

export interface ReliabilitySummary {
  window_days: number;
  incidents: {
    total: number;
    resolved: number;
    open: number;
    p1: number;
    mttr_seconds: number | null;
  };
  actions: Record<string, number>;
  verification: Record<string, number>;
}

export interface RecurringPattern {
  fingerprint: string;
  occurrences: number;
  services: string[];
  cause_category: string;
  symptom: string;
  first_seen: string | null;
  last_seen: string | null;
}

export interface ServiceLoadRow {
  service_id: string;
  incidents: number;
  p1: number;
  last_incident: string;
}

/* ------------------------------------------------------------ deployments -- */

export interface DeploymentRow {
  id: string;
  incident_id: string | null;
  action_id: string | null;
  patch_id: string | null;
  environment: string;
  service_id: string;
  from_version: string | null;
  to_version: string | null;
  strategy: string;
  state: string;
  verification_verdict: string | null;
  error: string | null;
  started_at: string;
  finished_at: string | null;
}

export interface PatchRow {
  id: string;
  incident_id: string;
  repo: string;
  base_ref: string;
  summary: string;
  rationale: string;
  files_changed: string[];
  lines_added: number;
  lines_removed: number;
  state: string;
  pull_request_url: string | null;
  // Tri-state: null means no sandbox run is linked yet. A patch that was
  // never tested must not render as one whose tests failed.
  reproduced: boolean | null;
  tests_passed: boolean | null;
  created_at: string;
}

export interface SandboxRunRow {
  id: string;
  incident_id: string | null;
  action_id: string | null;
  purpose: string;
  image: string;
  repo: string | null;
  base_ref: string | null;
  command: string;
  exit_code: number | null;
  timed_out: boolean;
  killed: boolean;
  duration_ms: number | null;
  stdout_excerpt: string;
  stderr_excerpt: string;
  network: string;
  resource_limits: Record<string, unknown>;
  started_at: string;
  finished_at: string | null;
}

/* ------------------------------------------------------- audit and tasks -- */

export interface AuditEntry {
  id?: number;
  incident_id?: string | null;
  actor: string;
  actor_type: 'human' | 'agent' | 'system';
  event_type: string;
  resource_type?: string | null;
  resource_id?: string | null;
  detail: Record<string, unknown>;
  correlation_id?: string | null;
  created_at: string;
}

export interface OperatorTask {
  kind: 'approval' | 'escalation' | 'blocked_incident';
  priority: number;
  title: string;
  incident_id: string;
  action_id?: string;
  approval_id?: string;
  severity: string;
  due_at: string | null;
  context: string;
}

/* ----------------------------------------------------------- integrations -- */

export interface IntegrationRow {
  name: string;
  configured: boolean;
  reason: string;
  healthy: boolean | null;
  health_reason: string;
  required?: boolean;
}

export interface AutonomyPosture {
  autonomy_enabled: boolean;
  autonomy_mode: string;
  allowed_tiers: number[];
  max_actions_per_hour: number;
  approval_ttl_seconds: number;
  lease_ttl_seconds: number;
  environment: string;
  kill_switch: {
    any_engaged: boolean;
    global: boolean;
    degraded: boolean;
    reason: string;
    environments: string[];
    action_types: string[];
    services: string[];
  };
}

/* ------------------------------------------------------------- evaluation -- */

export interface EvaluationRunRow {
  id: string;
  suite: string;
  ablation: string | null;
  status: string;
  scenarios_total: number;
  scenarios_scored: number;
  harness_failures: number;
  started_at: string;
  finished_at: string | null;
  metrics: Record<string, number | null>;
}

export interface ScenarioResultRow {
  scenario_id: string;
  title: string;
  category: string;
  passed: boolean | null;
  failure_class: string | null;
  harness_failure: boolean;
  metrics: Record<string, number | null>;
  duration_ms: number | null;
}

export interface CalibrationView {
  brier_score: number | null;
  expected_calibration_error: number | null;
  bins: Array<{ lower: number; upper: number; count: number; accuracy: number | null;
                mean_confidence: number | null }>;
}
