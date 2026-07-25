/** Typed HTTP client for the SIO API. Requests are same-origin via the Vite proxy. */

import * as session from "./session";
import type {
  Alert,
  Decision,
  Entity,
  Forecast,
  HealthStatus,
  ReplayPlan,
  SioEvent,
  Zone,
} from "../types";

const BASE = "/api";

/** A structured refusal, as the services express them. */
export interface RefusalDetail {
  message?: string;
  fix?: string;
  outstanding?: string[];
  problems?: { where: string; message: string; fix: string | null }[];
  legal_transitions?: string[];
  state?: string;
}

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
    readonly url: string,
    /**
     * The server's structured detail, when it sent one.
     *
     * The first version typed the body as `{ detail?: string }` and used it only as the Error message, so a
     * refusal expressed as an OBJECT — which is every refusal carrying a `fix`, an `outstanding` list or the
     * legal transitions — was flattened to `[object Object]` or lost. Combined with the gateway dropping
     * structured details entirely, an operator refused by the mission state machine saw `missions returned
     * 409` and nothing else, while the service had carefully written three fields explaining what to do.
     */
    readonly detail?: RefusalDetail | string,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

/**
 * Turn a caught error into the most useful sentence available.
 *
 * Shared rather than reimplemented per panel, because the parts worth keeping — the message, the fix, and what
 * is outstanding — are the parts each panel would otherwise drop on its own.
 */
export function explainError(error: unknown): string {
  if (!(error instanceof ApiError)) {
    return error instanceof Error ? error.message : String(error);
  }
  const detail = error.detail;
  if (!detail || typeof detail === "string") return error.message;
  const outstanding = detail.outstanding?.length
    ? ` (${detail.outstanding.join(", ")})`
    : "";
  const problems = detail.problems?.length
    ? ` ${detail.problems.map((problem) => `${problem.where}: ${problem.message}`).join("; ")}`
    : "";
  const legal = detail.legal_transitions?.length
    ? ` Legal from here: ${detail.legal_transitions.join(", ")}.`
    : "";
  const head = detail.message ?? error.message;
  return `${head}${outstanding}${problems}${detail.fix ? ` — ${detail.fix}` : ""}${legal}`;
}

async function request<T>(
  path: string,
  init?: RequestInit,
  retrying = false,
): Promise<T> {
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
    let message = response.statusText;
    let structured: RefusalDetail | string | undefined;
    try {
      const body = (await response.json()) as {
        detail?: RefusalDetail | string;
      };
      if (typeof body.detail === "string") {
        message = body.detail;
        structured = body.detail;
      } else if (body.detail && typeof body.detail === "object") {
        // Keep the object AND derive a readable message from it, so a caller that only looks at
        // `error.message` still gets a sentence rather than `[object Object]`.
        structured = body.detail;
        message = body.detail.message ?? message;
      }
    } catch {
      /* body was not json */
    }
    throw new ApiError(message, response.status, url, structured);
  }
  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

function query(
  params: Record<string, string | number | boolean | undefined>,
): string {
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
  ) => request<Entity[]>(`/entities${query(params)}`),

  entity: (entityId: string) =>
    request<Entity>(`/entities/${encodeURIComponent(entityId)}`),

  events: (
    params: {
      type?: string;
      severity?: string;
      limit?: number;
      since?: string;
    } = {},
  ) => request<SioEvent[]>(`/events${query(params)}`),

  timeline: (params: { from?: string; to?: string; limit?: number } = {}) =>
    request<SioEvent[]>(`/timeline${query(params)}`),

  worldAt: (ts: string, presenceWindowS?: number) =>
    request<{
      entities: Entity[];
      ts: string;
      count: number;
      counts: {
        total: number;
        movers: number;
        static: number;
        in_zones: number;
      };
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

  timelineDensity: (
    params: { from?: string; to?: string; buckets?: number } = {},
  ) =>
    request<{
      from: string;
      to: string;
      bucket_s: number;
      buckets: number;
      counts: number[];
      severe: number[];
      total: number;
    }>(`/timeline/density${query(params)}`),

  planReplay: (
    params: {
      from?: string;
      to?: string;
      speed?: number;
      step_s?: number;
    } = {},
  ) => request<ReplayPlan>(`/replay${query(params)}`, { method: "POST" }),

  cancelReplay: (replayId: string) =>
    request<{ cancelled: boolean }>(`/replay/${encodeURIComponent(replayId)}`, {
      method: "DELETE",
    }),

  nearby: (params: {
    lat: number;
    lon: number;
    radius_m: number;
    type?: string;
  }) => request<Entity[]>(`/spatial/nearby${query(params)}`),

  zones: () => request<Zone[]>("/spatial/zones"),

  /**
   * Cameras with their fields of view.
   *
   * The FOV has been in the `sources` table since Phase 0 and nothing returned it — `cameras_covering` answers
   * "which cameras see this zone" without handing back the geometry. So the data for coverage and blind-spot
   * analysis was present and unreachable from outside the database until the 3D twin needed it.
   */
  cameras: () =>
    request<
      {
        source_id: string;
        label: string | null;
        lat: number;
        lon: number;
        fov: { type: string; coordinates: number[][][] } | null;
      }[]
    >("/spatial/cameras"),

  alerts: (params: { state?: string; limit?: number } = {}) =>
    request<Alert[]>(`/alerts${query(params)}`),

  alertInbox: (
    params: { state?: string; grouped?: boolean; limit?: number } = {},
  ) =>
    request<{
      alerts: Alert[];
      groups?: Array<{
        kind: string;
        count: number;
        max_score: number;
        alerts: Alert[];
      }>;
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
    request<Alert>(
      `/alerts/${encodeURIComponent(alertId)}/escalate${query({ reason })}`,
      {
        method: "POST",
      },
    ),

  decisions: (params: { approval?: string; limit?: number } = {}) =>
    request<{ decisions: Decision[] }>(`/decisions${query(params)}`),

  approveDecision: (decisionId: string, optionId?: string, note?: string) =>
    request<{ decision_id: string; approval: string; chosen?: string | null }>(
      `/decisions/${encodeURIComponent(decisionId)}/approve`,
      {
        method: "POST",
        body: JSON.stringify({
          option_id: optionId,
          approved_by: "operator",
          note,
        }),
      },
    ),

  rejectDecision: (decisionId: string, reason?: string) =>
    request<{ decision_id: string; approval: string }>(
      `/decisions/${encodeURIComponent(decisionId)}/reject`,
      {
        method: "POST",
        body: JSON.stringify({ rejected_by: "operator", reason }),
      },
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
          points: Array<{
            ts: string;
            value: number;
            lo?: number | null;
            hi?: number | null;
          }>;
        }
      >;
    }>("/forecasts/latest"),

  analyticsSummary: (hours = 24) =>
    request<{
      window_hours: number;
      generated_at: string;
      counts: Record<string, number>;
      dwell: {
        overall: {
          count: number;
          mean: number;
          percentiles: Record<string, number>;
          histogram: Array<{
            from: number;
            to: number | null;
            count: number;
            share: number;
          }>;
          shape: string;
        };
        by_zone: Record<string, never>;
        open_visits_excluded: number;
      };
      throughput: {
        totals: Record<string, number>;
        entries_per_hour: number;
        smoothing: string;
      };
      utilisation: {
        zones: Array<{ zone_id: string; visits: number; utilisation: number }>;
      };
      risk: {
        score: number;
        band: string;
        drivers: string[];
        formula: string;
        terms: Record<
          string,
          {
            normalised: number;
            weight: number;
            contributes: number;
            why: string;
          }
        >;
      };
    }>(`/analytics/summary${query({ hours })}`),

  analyticsHeatmap: (hours = 6, resolution = 11) =>
    request<{
      resolution: number;
      edge_length_m: number;
      cells: Array<{
        h3: string;
        lat: number;
        lon: number;
        observations: number;
        entities: number;
        zone_id: string | null;
        types: Record<string, number>;
        // The hexagon's vertices as [lon, lat]. Sent by the server so the browser needs no H3 library.
        boundary: Array<[number, number]>;
      }>;
      total_observations: number;
      max_observations: number;
      measures?: string;
      suppressed: { cells: number; observations: number; why: string };
    }>(`/analytics/heatmap${query({ hours, resolution })}`),

  analyticsReport: (hours = 24) =>
    request<{ markdown: string }>(`/analytics/report${query({ hours })}`),

  // --- missions (M17) ---------------------------------------------------------------------------
  missions: (state?: string) =>
    request<{ missions: unknown[] }>(`/missions${query({ state })}`),

  mission: (missionId: string) =>
    request<unknown>(`/missions/${encodeURIComponent(missionId)}`),

  createMission: (body: {
    name: string;
    zone_id?: string | null;
    commander?: string | null;
    objectives?: { description: string; zone_id?: string | null }[];
  }) =>
    request<unknown>("/missions", {
      method: "POST",
      body: JSON.stringify(body),
    }),

  /**
   * Move a mission through its lifecycle.
   *
   * `force` is a parameter rather than a separate endpoint because it is the same act with an override, and the
   * service records the override in the comms log naming what was outstanding.
   */
  missionState: (missionId: string, to: string, force = false) =>
    request<unknown>(
      `/missions/${encodeURIComponent(missionId)}/state${query({ to, by: "console", force })}`,
      { method: "POST" },
    ),

  assignResource: (missionId: string, resourceId: string, role?: string) =>
    request<unknown>(
      `/missions/${encodeURIComponent(missionId)}/resources${query({
        resource_id: resourceId,
        by: "console",
        role,
      })}`,
      { method: "POST" },
    ),

  releaseResource: (missionId: string, resourceId: string) =>
    request<unknown>(
      `/missions/${encodeURIComponent(missionId)}/resources/${encodeURIComponent(resourceId)}${query(
        {
          by: "console",
        },
      )}`,
      { method: "DELETE" },
    ),

  completeObjective: (missionId: string, objectiveId: string, done: boolean) =>
    request<unknown>(
      `/missions/${encodeURIComponent(missionId)}/objectives/${encodeURIComponent(objectiveId)}${query(
        {
          done,
          by: "console",
        },
      )}`,
      { method: "POST" },
    ),

  addComm: (missionId: string, body: string, kind = "message") =>
    request<unknown>(`/missions/${encodeURIComponent(missionId)}/comms`, {
      method: "POST",
      body: JSON.stringify({ body, kind, author: "console" }),
    }),

  /** The mission's replay window, with a ready-made URL. */
  missionReplay: (missionId: string) =>
    request<{
      name: string;
      from: string;
      to: string;
      live: boolean;
      replay_url: string;
    }>(`/missions/${encodeURIComponent(missionId)}/replay`),

  /**
   * The no-code builder's vocabulary: what the engine can actually run.
   *
   * Fetched rather than hard-coded, because a hard-coded activity list is a UI offering steps the engine will
   * reject — and the failure arrives on save, after the author thought they were finished.
   */
  workflowVocabulary: () =>
    request<{
      activities: string[];
      operators: string[];
      severities: string[];
      fields: string[];
      note: string;
    }>("/workflow/vocabulary"),

  /**
   * Validate a draft without saving it.
   *
   * Server-side on purpose. Re-implementing the rules in TypeScript would produce two validators that disagree,
   * and the browser's would be the one people trust.
   */
  validateWorkflow: (document: unknown) =>
    request<{
      valid: boolean;
      problems: Array<{ where: string; message: string; fix: string | null }>;
      execution_order?: string[];
      compensation_order?: string[];
    }>("/workflow/authored/validate", {
      method: "POST",
      body: JSON.stringify(document),
    }),

  authoredWorkflows: () =>
    request<{
      workflows: unknown[];
      rejected: Array<{ where: string; message: string; fix: string | null }>;
    }>("/workflow/authored"),

  saveWorkflow: (name: string, document: unknown) =>
    request<{
      saved: string;
      path: string;
      execution_order: string[];
      armed: boolean;
    }>(`/workflow/authored/${encodeURIComponent(name)}`, {
      method: "PUT",
      body: JSON.stringify(document),
    }),

  deleteWorkflow: (name: string) =>
    request<{ deleted: string }>(
      `/workflow/authored/${encodeURIComponent(name)}`,
      {
        method: "DELETE",
      },
    ),

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
        steps: Array<{
          index: number;
          kind: string;
          tool?: string | null;
          detail: string;
          latency_ms: number;
          ok: boolean;
        }>;
      };
      elapsed_ms: number;
    }>("/copilot/ask", { method: "POST", body: JSON.stringify({ question }) }),
};

export const mediaUrl = (key: string) => `/media/${key}`;
