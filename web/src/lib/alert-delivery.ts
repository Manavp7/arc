import { api } from "./api";

export type DeliveryStatus = "pending" | "sending" | "delivered" | "failed" | "blocked";
export interface DeliveryHistory {
  history_id?: number; kind: string; at: string; attempt: number; status_code: number | null;
  error: string | null; actor: string | null; reason: string | null;
}
export interface AlertDelivery {
  delivery_id: string; alert_id: string; action: "raised" | "escalated"; title: string;
  status: DeliveryStatus; attempts: number; cycle_attempts: number; max_attempts: number;
  created_at: string; updated_at: string; next_attempt_at: string | null;
  delivered_at: string | null; status_code: number | null; error: string | null;
  destination: string; can_retry: boolean; history: DeliveryHistory[]; history_next_cursor?: string | null;
}
export interface AlertDeliveryList {
  configured: boolean; destination: string | null; max_attempts: number;
  deliveries: AlertDelivery[]; next_cursor?: string | null;
}

export const alertDeliveryApi = {
  list: (status: DeliveryStatus | "", signal?: AbortSignal, page: {cursor?: string; alert_id?: string} = {}) => api.request<AlertDeliveryList>(
    `/alert-deliveries?${new URLSearchParams({limit: "100", ...(status ? {status} : {}), ...(page.cursor ? {cursor:page.cursor} : {}), ...(page.alert_id ? {alert_id:page.alert_id} : {})})}`, { signal },
  ),
  history: (id: string, cursor?: string, signal?: AbortSignal) => api.request<{history: DeliveryHistory[]; next_cursor: string | null}>(
    `/alert-deliveries/${encodeURIComponent(id)}/history?${new URLSearchParams({limit:"20", ...(cursor ? {cursor} : {})})}`, {signal},
  ),
  retry: (id: string, reason: string, signal?: AbortSignal) => api.request<AlertDelivery>(
    `/alert-deliveries/${encodeURIComponent(id)}/retry`,
    { method: "POST", body: JSON.stringify({ reason: reason.trim() }), signal },
  ),
};

export function canRetryDelivery(row: AlertDelivery, roles: string[]): boolean {
  return row.can_retry && ["failed", "blocked"].includes(row.status)
    && roles.some(role => role === "admin" || role === "integrator");
}

export function deliveryTime(value: string | null): string {
  if (!value) return "—";
  const date = new Date(value);
  return Number.isFinite(date.getTime()) ? date.toLocaleString() : "Time unavailable";
}

export function mergeDeliveryHistory(current: DeliveryHistory[], older: DeliveryHistory[]): DeliveryHistory[] {
  const seen = new Set<string>();
  return [...current, ...older].filter(item => {const key = item.history_id != null ? String(item.history_id) : JSON.stringify([item.at,item.kind,item.attempt,item.actor,item.reason]); if (seen.has(key)) return false; seen.add(key); return true;});
}
