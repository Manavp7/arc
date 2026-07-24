/** Typed HTTP client for the SIO API. Requests are same-origin via the Vite proxy. */

import type {
  Alert,
  Decision,
  Entity,
  Forecast,
  HealthStatus,
  Mission,
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

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const url = `${BASE}${path}`;
  const response = await fetch(url, {
    ...init,
    headers: { "Content-Type": "application/json", ...(init?.headers ?? {}) },
  });
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

  worldAt: (ts: string) => request<{ entities: Entity[]; ts: string }>(`/world/at${query({ ts })}`),

  nearby: (params: { lat: number; lon: number; radius_m: number; type?: string }) =>
    request<Entity[]>(`/spatial/nearby${query(params)}`),

  zones: () => request<Zone[]>("/spatial/zones"),

  alerts: (params: { state?: string; limit?: number } = {}) =>
    request<Alert[]>(`/alerts${query(params)}`),

  acknowledgeAlert: (alertId: string, note?: string) =>
    request<Alert>(`/alerts/${encodeURIComponent(alertId)}/ack`, {
      method: "POST",
      body: JSON.stringify({ note }),
    }),

  decisions: (params: { approval?: string; limit?: number } = {}) =>
    request<Decision[]>(`/decisions${query(params)}`),

  approveDecision: (decisionId: string, optionId?: string) =>
    request<Decision>(`/decisions/${encodeURIComponent(decisionId)}/approve`, {
      method: "POST",
      body: JSON.stringify({ option_id: optionId }),
    }),

  rejectDecision: (decisionId: string, reason?: string) =>
    request<Decision>(`/decisions/${encodeURIComponent(decisionId)}/reject`, {
      method: "POST",
      body: JSON.stringify({ reason }),
    }),

  forecasts: (params: { target?: string; entity_id?: string; limit?: number } = {}) =>
    request<Forecast[]>(`/forecasts${query(params)}`),

  missions: () => request<Mission[]>("/missions"),

  ask: (question: string) =>
    request<{ answer: string; explanation: import("../types").Explanation }>("/copilot/ask", {
      method: "POST",
      body: JSON.stringify({ question }),
    }),
};

export const mediaUrl = (key: string) => `/media/${key}`;
