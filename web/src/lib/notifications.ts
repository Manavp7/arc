import { api } from "./api";

export interface NotificationTarget {
  kind: "case" | "analysis" | "package";
  id: string;
  case_id?: string;
  video_id?: string;
  analysis_id?: string;
  package_id?: string;
}
export interface PersonalNotification {
  notification_id: string;
  revision: number;
  kind: "case_assigned" | "case_due_soon" | "case_overdue" | "analysis_completed" | "analysis_failed" | "package_completed" | "package_failed";
  title: string;
  message: string;
  severity: "info" | "warning" | "error";
  created_at: string;
  occurred_at: string;
  read_at: string | null;
  target: NotificationTarget;
}
export interface NotificationPage {
  notifications: PersonalNotification[];
  unread_count: number;
  counts_complete: boolean;
  has_more: boolean;
  next_cursor: string | null;
  checked_at: string;
  note: string;
}
export const notificationApi = {
  list: (unreadOnly: boolean, before?: string | null, signal?: AbortSignal) => {
    const query = new URLSearchParams({ unread_only: String(unreadOnly), limit: "20" });
    if (before) query.set("before", before);
    return api.request<NotificationPage>(`/notifications?${query}`, { signal });
  },
  mark: (records: readonly PersonalNotification[], signal?: AbortSignal) => api.request<{ notifications: PersonalNotification[] }>("/notifications/read", {
    method: "POST", body: JSON.stringify({ notifications: unreadSnapshot(records) }), signal,
  }),
  target: (id: string, signal?: AbortSignal) => api.request<{ target: NotificationTarget }>(`/notifications/${encodeURIComponent(id)}/target`, { signal }),
};

/** A bulk action names only the unread rows that were actually shown to this recipient. */
export function unreadSnapshot(records: readonly PersonalNotification[]): { notification_id: string; revision: number }[] {
  const unique = new Map<string, number>();
  for (const record of records) if (!record.read_at) unique.set(record.notification_id, record.revision);
  return [...unique].slice(0, 100).map(([notification_id, revision]) => ({ notification_id, revision }));
}
export function appendNotificationPage(current: readonly PersonalNotification[], next: readonly PersonalNotification[]): PersonalNotification[] {
  const records = new Map(current.map(record => [record.notification_id, record]));
  for (const record of next) records.set(record.notification_id, record);
  return [...records.values()].sort((a, b) => b.created_at.localeCompare(a.created_at) || b.notification_id.localeCompare(a.notification_id));
}
export function notificationCount(count: number, complete: boolean): string {
  const finite = Number.isFinite(count) ? Math.max(0, Math.floor(count)) : 0;
  return `${finite > 99 ? "99" : finite}${finite > 99 || !complete ? "+" : ""}`;
}
export function notificationTime(value: string, now = Date.now()): string {
  const timestamp = new Date(value).getTime();
  if (!Number.isFinite(timestamp)) return "Time unavailable";
  const age = Math.max(0, now - timestamp);
  if (age < 60000) return "Just now";
  if (age < 3600000) return `${Math.floor(age / 60000)}m ago`;
  if (age < 86400000) return `${Math.floor(age / 3600000)}h ago`;
  return new Date(timestamp).toLocaleString([], { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
}
