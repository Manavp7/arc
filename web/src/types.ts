/**
 * TypeScript mirrors of the `sio_schemas` contracts.
 *
 * Hand-written rather than generated for now: the API is the only consumer, the shapes are
 * small, and a generated client would add a build step before there is an OpenAPI document to
 * generate from. Phase 6 replaces this with a generated SDK (`sdk/ts`), at which point this
 * file becomes re-exports.
 *
 * Field names follow the wire format, so `class` (not `class_name`) and `from`/`to`.
 */

export type Severity = "info" | "low" | "medium" | "high" | "critical";
export type AlertState = "open" | "acknowledged" | "escalated" | "resolved" | "suppressed";
export type ApprovalState = "not_required" | "pending" | "approved" | "rejected" | "expired";
export type RunStatus = "pending" | "running" | "completed" | "failed" | "cancelled" | "compensated";

export interface Geo {
  lat: number;
  lon: number;
  alt?: number | null;
  crs?: string;
}

export interface Velocity {
  north: number;
  east: number;
  up: number;
}

export interface EntityState {
  ts: string;
  geo?: Geo | null;
  velocity?: Velocity | null;
  heading_deg?: number | null;
  zone_id?: string | null;
  h3_cell?: string | null;
  confidence: number;
}

export interface Provenance {
  source_id: string;
  modality: string;
  ts: string;
  confidence: number;
  weight: number;
  note?: string | null;
}

export interface Entity {
  entity_id: string;
  tenant_id: string;
  type: string;
  label?: string | null;
  attributes: Record<string, unknown>;
  state: EntityState;
  provenance: Provenance[];
  first_seen: string;
  last_seen: string;
  confidence: number;
  track_ids: string[];
  is_static: boolean;
}

export interface EvidenceRef {
  kind: string;
  ref: string;
  ts?: string | null;
  source_id?: string | null;
  score?: number | null;
  note?: string | null;
}

export interface TimelineEntry {
  ts: string;
  kind: string;
  summary: string;
  ref?: string | null;
}

export interface Alternative {
  hypothesis: string;
  confidence: number;
  why_not?: string | null;
}

/** The evidence bundle attached to every assertion SIO makes (PRD M20). */
export interface Explanation {
  summary?: string | null;
  evidence: EvidenceRef[];
  confidence: number;
  sources: string[];
  timeline: TimelineEntry[];
  related_entities: string[];
  alternatives: Alternative[];
  degraded: boolean;
  notes: string[];
}

export interface SioEvent {
  event_id: string;
  type: string;
  severity: Severity;
  entities: string[];
  geo?: Geo | null;
  zone_id?: string | null;
  ts: string;
  detected_ts: string;
  evidence: EvidenceRef[];
  confidence: number;
  explanation: Explanation;
  rule_id?: string | null;
  attributes: Record<string, unknown>;
  source_ids: string[];
}

export interface Alert {
  alert_id: string;
  title: string;
  event_ids: string[];
  entity_ids: string[];
  severity: Severity;
  score: number;
  group_key: string;
  count: number;
  state: AlertState;
  ts: string;
  last_ts: string;
  geo?: Geo | null;
  zone_id?: string | null;
  ack_by?: string | null;
  assignee?: string | null;
  urgency_reason?: string | null;
  explanation: Explanation;
}

export interface DecisionOption {
  option_id: string;
  action: string;
  target_entity_id?: string | null;
  params: Record<string, unknown>;
  score: number;
  expected_effect: string;
  expected_metrics: Record<string, number>;
  cost: number;
  risk: number;
  feasible: boolean;
  rejection_reason?: string | null;
}

export interface Decision {
  decision_id: string;
  trigger_event?: string | null;
  ts: string;
  options: DecisionOption[];
  chosen?: string | null;
  rationale: string;
  expected_effect: string;
  confidence: number;
  explanation: Explanation;
  proposed_by: string;
  approval: ApprovalState;
  approved_by?: string | null;
}

export interface ForecastPoint {
  ts: string;
  value: number;
  lo?: number | null;
  hi?: number | null;
}

export interface Forecast {
  forecast_id: string;
  target: string;
  entity_id?: string | null;
  zone_id?: string | null;
  ts: string;
  horizon_s: number;
  points: ForecastPoint[];
  geo_points: Geo[];
  model_name: string;
  confidence: number;
  interval_level: number;
}

export interface MissionObjective {
  objective_id: string;
  description: string;
  done: boolean;
  progress: number;
}

export interface Mission {
  mission_id: string;
  name: string;
  description?: string | null;
  state: "draft" | "active" | "paused" | "completed" | "aborted";
  objectives: MissionObjective[];
  assignees: string[];
  resources: string[];
  created_ts: string;
  updated_ts: string;
}

export interface WorkflowStep {
  step_id: string;
  name: string;
  status: RunStatus;
  started_ts?: string | null;
  finished_ts?: string | null;
  attempts: number;
  error?: string | null;
}

export interface WorkflowRun {
  run_id: string;
  playbook: string;
  status: RunStatus;
  trigger_event?: string | null;
  steps: WorkflowStep[];
  started_ts: string;
  finished_ts?: string | null;
}

/** Site geometry: gates, docks, lanes, restricted areas. */
export interface Zone {
  zone_id: string;
  name: string;
  kind: string;
  restricted: boolean;
  geometry: GeoJSON.Polygon;
}

/** Envelope pushed over SSE. `kind` selects the payload type. */
export interface StreamMessage {
  id: string;
  topic: string;
  kind: string;
  ts: string;
  trace_id: string;
  payload: unknown;
}

export interface HealthStatus {
  service: string;
  status: string;
  version: string;
  schema_version: string;
  uptime_s: number;
  checks: Record<string, string>;
  consumed: number;
  produced: number;
  errors: number;
  lag: Record<string, number>;
  adapters: Record<string, string>;
}
