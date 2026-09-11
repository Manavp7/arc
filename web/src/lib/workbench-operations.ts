import { api } from "./api";
import type { CaseRecord } from "./review-types";

export interface VideoJob {
  job_id: string; video_id: string; video_title: string; analysis_id: string; revision: number;
  status: "queued" | "running" | "completed" | "failed" | "interrupted" | "cancelled" | "cancelling";
  progress: number; attempt: number; max_attempts: number; queued_at: string;
  started_at?: string; finished_at?: string; error?: string; retry_of?: string;
  analysis_ids?: string[];
}
export interface StoredVideo {
  video_id: string; title: string; revision: number; status: string; bytes: number | null;
  archived_at?: string | null; purged_at?: string | null; eligible: boolean; reasons: string[];
  linked_cases: number; linked_evaluations: number; linked_packages: number; purge_error?: string;
}
export interface StorageOverview {
  videos: StoredVideo[];
  settings: {revision: number; archive_after_days: number};
  usage: {total_bytes: number; active_bytes: number; archived_bytes: number; video_count: number; archived_count: number; inspection_complete: boolean; note: string};
}
export interface CleanupPreview { videos: StoredVideo[]; note?: string; preview_token?: string; expires_at?: string; reclaimable_bytes?: number }
export type InboxView = "active" | "mine" | "unassigned" | "overdue" | "due_soon" | "resolved" | "all";
export interface InboxCase extends CaseRecord { overdue: boolean; due_soon: boolean; zone_id?: string }
export interface InboxSnapshot {cases: InboxCase[]; counts: Record<string, number>; as_of: string; subject: string; possibly_truncated: boolean}
export interface Handover {handover_id: string; title: string; text: string; author: string; captured_at: string; case_ids: string[]; cases: {case_id: string; title: string; status: string; owner: string | null; revision: number}[]}
const json = (body: unknown, method = "POST"): RequestInit => ({method, body: JSON.stringify(body)});
export const workbenchOps = {
  jobs: (signal?: AbortSignal) => api.request<{jobs: VideoJob[]; capabilities: {max_attempts: number; max_pending: number; worker_concurrency: number}}>("/review/jobs", {signal}),
  cancel: (job: VideoJob) => api.request<VideoJob>(`/review/jobs/${encodeURIComponent(job.job_id)}/cancel`, json({revision: job.revision})),
  retry: (job: VideoJob) => api.request<VideoJob>(`/review/jobs/${encodeURIComponent(job.job_id)}/retry`, json({revision: job.revision})),
  storage: (signal?: AbortSignal) => api.request<StorageOverview>("/review/storage", {signal}),
  retention: (revision: number, days: number) => api.request("/review/storage/settings", json({revision, archive_after_days: days}, "PUT")),
  preview: (ids?: string[]) => api.request<CleanupPreview>("/review/storage/preview", json(ids ? {video_ids: ids} : {})),
  archive: (videos: StoredVideo[]) => api.request<{note: string}>("/review/storage/archive", json({videos: videos.map(({video_id, revision}) => ({video_id, revision}))})),
  restore: (video: StoredVideo) => api.request(`/review/storage/restore/${encodeURIComponent(video.video_id)}`, json({revision: video.revision})),
  purgePreview: (ids: string[]) => api.request<CleanupPreview>("/review/storage/purge-preview", json({video_ids: ids})),
  purge: (preview: CleanupPreview) => api.request<{results: {status: string; reclaimed_bytes?: number}[]}>("/review/storage/purge", json({preview_token: preview.preview_token, confirmed_video_ids: preview.videos.filter(row => row.eligible).map(row => row.video_id)})),
  inbox: (view: InboxView, priority: string, q: string, signal?: AbortSignal) => api.request<InboxSnapshot>(`/case-inbox?${new URLSearchParams({view, ...(priority ? {priority} : {}), q})}`, {signal}),
  handovers: (signal?: AbortSignal) => api.request<{handovers: Handover[]}>("/case-inbox/handovers", {signal}),
  handover: (title: string, text: string, caseIds: string[]) => api.request<Handover>("/case-inbox/handovers", json({title, text, case_ids: caseIds})),
};

export function storageSize(bytes: number | null | undefined): string {
  if (bytes == null || !Number.isFinite(bytes)) return "Not measured";
  if (bytes < 1024) return `${bytes} B`;
  const unit = bytes >= 1024 ** 3 ? "GiB" : bytes >= 1024 ** 2 ? "MiB" : "KiB";
  const divisor = unit === "GiB" ? 1024 ** 3 : unit === "MiB" ? 1024 ** 2 : 1024;
  return `${(bytes / divisor).toFixed(1)} ${unit}`;
}
export function cleanupReason(value: string): string {
  const labels: Record<string, string> = {movement_report_linked: "Protected by a movement report", bookmark_linked: "Protected by a private bookmark", case_linked: "Protected by a case", evaluation_linked: "Protected by evaluation data", evidence_package_linked: "Protected by an evidence package", active_job: "Processing job is active", active_video: "Recording is processing", already_archived: "Already archived", already_purged: "Permanently removed", archive_first: "Archive before deleting", within_retention_period: "Inside the retention window", reference_scan_incomplete: "Reference check incomplete", storage_inspection_failed: "Storage could not be inspected", unknown_creation_date: "Creation date unavailable"};
  return labels[value] ?? value.replaceAll("_", " ");
}
