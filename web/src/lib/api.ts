/** Typed HTTP client for the SIO API. Requests are same-origin via the Vite proxy. */

import * as session from "./session";
import type {
  Alert,
  Decision,
  Entity,
  Forecast,
  HealthStatus,
  Mission,
  ReplayPlan,
  SioEvent,
  Zone,
} from "../types";

const BASE = "/api";

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
    readonly url: string,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

async function request<T>(path: string, init?: RequestInit, retrying = false): Promise<T> {
  const url = `${BASE}${path}`;
  // Every request carries a principal. `ensure()` reuses a valid session, so this is a no-op after the
  // first call — and concurrent callers share one mint rather than each getting their own.
  await session.ensure();
  const response = await fetch(url, {
    ...init,
    headers: {
      "Content-Type": "application/json",
      ...session.headers(),
      ...(init?.headers ?? {}),
    },
  });
  // A 401 means the token expired or the secret changed. Retry ONCE with a fresh one: a loop here would
  // turn a misconfigured secret into an infinite request storm against the token endpoint.
  if (response.status === 401 && !retrying) {
    session.clear();
    return request<T>(path, init, true);
  }
  if (!response.ok) {
    // Surface the server's message: the API returns a reason for every denial, and hiding it
    // behind a generic "request failed" is how governance decisions become unexplainable.
    let detail = response.statusText;
    try {
      const body = (await response.json()) as { detail?: string };
      if (body.detail) detail = body.detail;
    } catch {
      /* body was not json */
    }
    throw new ApiError(detail, response.status, url);
  }
  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

function query(params: Record<string, string | number | boolean | undefined>): string {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value !== undefined && value !== "") search.set(key, String(value));
  }
  const text = search.toString();
  return text ? `?${text}` : "";
}

export const api = {
  health: () => request<HealthStatus>("/health"),

  entities: (
    params: {
      type?: string;
      zone_id?: string;
      limit?: number;
      since?: string;
      active_within_s?: number;
      include_static?: boolean;
    } = {},
  ) =>
    request<Entity[]>(`/entities${query(params)}`),

  entity: (entityId: string) => request<Entity>(`/entities/${encodeURIComponent(entityId)}`),

  events: (params: { type?: string; severity?: string; limit?: number; since?: string } = {}) =>
    request<SioEvent[]>(`/events${query(params)}`),

  timeline: (params: { from?: string; to?: string; limit?: number } = {}) =>
    request<SioEvent[]>(`/timeline${query(params)}`),

  worldAt: (ts: string, presenceWindowS?: number) =>
    request<{
      entities: Entity[];
      ts: string;
      count: number;
      counts: { total: number; movers: number; static: number; in_zones: number };
      presence_window_s: number;
    }>(`/world/at${query({ ts, presence_window_s: presenceWindowS })}`),

  timelineBounds: () =>
    request<{
      start: string | null;
      end: string | null;
      span_s: number;
      first_event: string | null;
      last_event: string | null;
    }>("/timeline/bounds"),

  timelineDensity: (params: { from?: string; to?: string; buckets?: number } = {}) =>
    request<{
      from: string;
      to: string;
      bucket_s: number;
      buckets: number;
      counts: number[];
      severe: number[];
      total: number;
    }>(`/timeline/density${query(params)}`),

  planReplay: (params: { from?: string; to?: string; speed?: number; step_s?: number } = {}) =>
    request<ReplayPlan>(`/replay${query(params)}`, { method: "POST" }),

  cancelReplay: (replayId: string) =>
    request<{ cancelled: boolean }>(`/replay/${encodeURIComponent(replayId)}`, { method: "DELETE" }),

  nearby: (params: { lat: number; lon: number; radius_m: number; type?: string }) =>
    request<Entity[]>(`/spatial/nearby${query(params)}`),

  zones: () => request<Zone[]>("/spatial/zones"),

  alerts: (params: { state?: string; limit?: number } = {}) =>
    request<Alert[]>(`/alerts${query(params)}`),

  alertInbox: (params: { state?: string; grouped?: boolean; limit?: number } = {}) =>
    request<{
      alerts: Alert[];
      groups?: Array<{ kind: string; count: number; max_score: number; alerts: Alert[] }>;
      open?: number;
      escalated?: number;
    }>(`/alerts${query({ grouped: true, ...params })}`),

  acknowledgeAlert: (alertId: string, note?: string) =>
    request<Alert>(`/alerts/${encodeURIComponent(alertId)}/ack`, {
      method: "POST",
      body: JSON.stringify({ ack_by: "operator", note }),
    }),

  resolveAlert: (alertId: string, note?: string) =>
    request<Alert>(`/alerts/${encodeURIComponent(alertId)}/resolve`, {
      method: "POST",
      body: JSON.stringify({ resolved_by: "operator", note }),
    }),

  escalateAlert: (alertId: string, reason = "escalated by hand") =>
    request<Alert>(`/alerts/${encodeURIComponent(alertId)}/escalate${query({ reason })}`, {
      method: "POST",
    }),

  decisions: (params: { approval?: string; limit?: number } = {}) =>
    request<{ decisions: Decision[] }>(`/decisions${query(params)}`),

  approveDecision: (decisionId: string, optionId?: string, note?: string) =>
    request<{ decision_id: string; approval: string; chosen?: string | null }>(
      `/decisions/${encodeURIComponent(decisionId)}/approve`,
      {
        method: "POST",
        body: JSON.stringify({ option_id: optionId, approved_by: "operator", note }),
      },
    ),

  rejectDecision: (decisionId: string, reason?: string) =>
    request<{ decision_id: string; approval: string }>(
      `/decisions/${encodeURIComponent(decisionId)}/reject`,
      { method: "POST", body: JSON.stringify({ rejected_by: "operator", reason }) },
    ),

  forecasts: (params: { target?: string; limit?: number } = {}) =>
    request<{ forecasts: Forecast[] }>(`/forecasts${query(params)}`),

  latestForecasts: () =>
    request<{
      forecasts: Record<
        string,
        {
          target: string;
          zone_id?: string | null;
          entity_id?: string | null;
          model: string;
          confidence: number;
          interval_level: number;
          horizon_s: number;
          summary?: string | null;
          why: string[];
          points: Array<{ ts: string; value: number; lo?: number | null; hi?: number | null }>;
        }
      >;
    }>("/forecasts/latest"),

  workflowRuns: (limit = 20) =>
    request<{
      runs: number;
      by_playbook?: Record<string, number>;
      suppressed_by_cooldown: number;
      recent: Array<{
        run_id: string;
        playbook: string;
        status: string;
        progress: number;
        started: string;
        steps: Array<{ name: string; status: string; attempts: number }>;
      }>;
    }>(`/workflow/runs${query({ limit })}`),

  missions: () => request<Mission[]>("/missions"),

  ask: (question: string) =>
    request<{
      question: string;
      answer: string;
      confidence: number;
      explanation: import("../types").Explanation;
      trace: {
        model: string;
        total_ms: number;
        tools_used: string[];
        used_fallback: boolean;
        degraded: string[];
        steps: Array<{ index: number; kind: string; tool?: string | null; detail: string; latency_ms: number; ok: boolean }>;
      };
      elapsed_ms: number;
    }>("/copilot/ask", { method: "POST", body: JSON.stringify({ question }) }),
};

export const mediaUrl = (key: string) => `/media/${key}`;
