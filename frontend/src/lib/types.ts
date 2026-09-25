/** Wire types. These mirror the backend response shapes exactly. */

export type Severity = 'P1' | 'P2' | 'P3' | 'P4';

export type IncidentState =
  | 'RECEIVED' | 'TRIAGING' | 'INVESTIGATING' | 'DIAGNOSING' | 'DEBUGGING'
  | 'VERIFYING' | 'AWAITING_APPROVAL' | 'REMEDIATING' | 'MONITORING'
  | 'RESOLVED' | 'ESCALATED' | 'BLOCKED';

/**
 * SOURCE_UNAVAILABLE is deliberately distinct from an absence of results.
 * The UI must never render the two identically (PRD section 13).
 */
export type EvidenceStatus =
  | 'UNVALIDATED' | 'VALIDATED' | 'REFUTED' | 'SOURCE_UNAVAILABLE';

export type TrustClass = 'TIER_A' | 'TIER_B' | 'TIER_C' | 'TIER_D';

export interface Incident {
  id: string;
  title: string;
  severity: Severity;
  state: IncidentState;
  environment: string;
  workload: string;
  affected_services: string[];
  suspected_origin: string | null;
  confidence: number | null;
  summary: string;
  owner: string | null;
  correlation_id: string;
  created_at: string;
  updated_at: string;
  resolved_at: string | null;
}

export interface IncidentList {
  items: Incident[];
  total_open: number;
  limit: number;
  offset: number;
}

export interface EvidenceItem {
  id: string;
  source: string;
  source_type: string;
  evidence_type: string;
  status: EvidenceStatus;
  trust_class: TrustClass;
  summary: string;
  structured_value: Record<string, unknown>;
  provenance_uri: string;
  resource_id: string | null;
  observed_at: string | null;
  retrieved_at: string;
  untrusted: boolean;
}

export interface EvidenceResponse {
  items: EvidenceItem[];
  counts: {
    total: number;
    usable: number;
    unavailable_sources: number;
    by_trust: Record<string, number>;
  };
}

export interface Hypothesis {
  id: string;
  label: string;
  statement: string;
  state: string;
  confidence: number;
  supporting: string[];
  contradicting: string[];
  missing: string[];
  predictions: Array<{ statement: string; metric: string | null; direction: string | null; tested: boolean; holds: boolean | null }>;
  affected_services: string[];
  rejected_reason: string | null;
  updated_at: string;
}

export interface HypothesesResponse {
  items: Hypothesis[];
  confidence_history: Array<{ label: string; confidence: number; recorded_at: string }>;
}

export type TimelineKind = 'STATE' | 'AI' | 'TOOL';

export interface TimelineEvent {
  kind: TimelineKind;
  at: string;
  title: string;
  detail: string | null;
  actor?: string;
  status?: string;
  ok?: boolean;
  access?: string;
  duration_ms?: number | null;
  evidence_ids?: string[];
}

export interface TimelineResponse {
  events: TimelineEvent[];
  gaps: Array<{ source: string; source_type: string; reason: string; affects: string[]; attempted_at: string }>;
}

export interface Diagnosis {
  abstained: boolean;
  statement: string;
  root_cause_category: string | null;
  confidence: number;
  supporting_evidence: string[];
  causal_path: string[];
  affected_services: string[];
  contributing_factors: string[];
  rejected_alternatives: string[];
  missing_evidence: string[];
  uncertainty: string;
}

export interface HealthComponent {
  status: string;
  hard_dependency: boolean;
  affects: string[];
  detail?: string | null;
  provider?: string;
}

export interface Health {
  status: 'healthy' | 'degraded' | 'unavailable';
  version: string;
  environment: string;
  autonomy: { enabled: boolean; mode: string; allowed_tiers: number[] };
  components: Record<string, HealthComponent>;
  circuit_breakers: Record<string, string>;
  degraded_components: string[];
}

export interface PolicyView {
  policy_version: string;
  autonomy: { enabled: boolean; mode: string; allowed_tiers: number[]; max_actions_per_hour: number };
  approval_ttl_seconds: number;
  kill_switches: {
    any_engaged: boolean;
    global: boolean;
    degraded: boolean;
    environments: string[];
    services: string[];
    action_types: string[];
  };
}
