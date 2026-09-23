import { api } from "./api";

export type DeliveryStatus = "pending" | "sending" | "delivered" | "failed" | "blocked";
export interface DeliveryHistory {
  kind: string; at: string; attempt: number; status_code: number | null;
  error: string | null; actor: string | null; reason: string | null;
}
export interface AlertDelivery {
  delivery_id: string; alert_id: string; action: "raised" | "escalated"; title: string;
  status: DeliveryStatus; attempts: number; cycle_attempts: number; max_attempts: number;
  created_at: string; updated_at: string; next_attempt_at: string | null;
  delivered_at: string | null; status_code: number | null; error: string | null;
  destination: string; can_retry: boolean; history: DeliveryHistory[];
}
export interface AlertDeliveryList {
  configured: boolean; destination: string | null; max_attempts: number;
  deliveries: AlertDelivery[];
}

export const alertDeliveryApi = {
  list: (status: DeliveryStatus | "", signal?: AbortSignal) => api.request<AlertDeliveryList>(
    `/alert-deliveries?limit=100${status ? `&status=${encodeURIComponent(status)}` : ""}`, { signal },
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
