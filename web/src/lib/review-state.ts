import type { CaseRecord, ReviewVideo, VideoAnalysis } from "./review-types";
export interface CaseDraft { title: string; owner: string; due: string; status: CaseRecord["status"]; summary: string; verdict: CaseRecord["verdict"]; resolution: string }
export function localDateInput(iso?: string | null): string {
  if (!iso) return "";
  const date = new Date(iso); if (!Number.isFinite(date.getTime())) return "";
  return new Date(date.getTime() - date.getTimezoneOffset() * 60000).toISOString().slice(0, 16);
}
export function caseDraft(record: CaseRecord): CaseDraft { return { title: record.title, owner: record.owner ?? "", due: localDateInput(record.due_at), status: record.status, summary: record.summary ?? "", verdict: record.verdict, resolution: record.resolution_note ?? "" }; }
export function casePatch(draft: CaseDraft, revision: number): Record<string, unknown> {
  if (!draft.title.trim()) throw new Error("Give this case a title.");
  if (draft.status === "resolved" && !draft.resolution.trim()) throw new Error("Add a resolution reason before resolving the case.");
  const due = draft.due ? new Date(draft.due) : null;
  if (due && !Number.isFinite(due.getTime())) throw new Error("Choose a valid due date and time.");
  return { expected_revision: revision, title: draft.title.trim(), owner: draft.owner.trim() || null, due_at: due?.toISOString() ?? null, status: draft.status, summary: draft.summary, verdict: draft.verdict, ...(draft.resolution.trim() ? { resolution_note: draft.resolution.trim() } : {}) };
}
export function metricDuration(seconds: number | null): string {
  if (seconds == null || !Number.isFinite(seconds)) return "Not available";
  if (seconds < 60) return `${Math.round(seconds)}s`;
  if (seconds < 3600) return `${(seconds / 60).toFixed(1)}m`;
  return `${(seconds / 3600).toFixed(1)}h`;
}

/** A case opens the configuration that produced its evidence, even after the video is edited. */
export function reviewConfiguration(video: ReviewVideo, analysis: VideoAnalysis | null, historical: boolean) {
  const record = historical ? analysis : video;
  return { zones: record?.zones ?? [], rules: record?.rules ?? [] };
}
