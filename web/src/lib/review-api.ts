import { api, ApiError } from "./api";
import * as session from "./session";
import type { AnalysisOptions, CaseNote, CaseRecord, ReviewMetrics, ReviewRule, ReviewSearchResult, ReviewVideo, ReviewZone, RulePreview, SavedSearch, SearchFilters, VideoAnalysis, VideoLibrary } from "./review-types";
const pathFor = (id: string) => `/review/videos/${encodeURIComponent(id)}`;
function query(values: Record<string, unknown>): string { const params = new URLSearchParams(); for (const [key, value] of Object.entries(values)) if (value !== "" && value != null) params.set(key, String(value)); return params.toString() ? `?${params}` : ""; }
export const reviewApi = {
  videos: (signal?: AbortSignal) => api.request<VideoLibrary>("/review/videos", { signal }),
  video: (id: string, signal?: AbortSignal) => api.request<ReviewVideo>(pathFor(id), { signal }),
  upload: (file: File, signal?: AbortSignal) => api.request<ReviewVideo>("/review/videos", { method: "POST", body: file, signal, headers: { "Content-Type": "video/mp4", "X-Filename": encodeURIComponent(file.name) } }),
  configure: (id: string, revision: number, zones: ReviewZone[], rules: ReviewRule[]) => api.request<ReviewVideo>(`${pathFor(id)}/configuration`, { method: "PUT", body: JSON.stringify({ revision, zones, rules }) }),
  analyze: (id: string, options?: AnalysisOptions) => api.request<VideoAnalysis>(`${pathFor(id)}/analyze`, { method: "POST", body: JSON.stringify(options ?? {}) }),
  analysis: (id: string, signal?: AbortSignal, analysisId?: string) => api.request<VideoAnalysis>(`${pathFor(id)}/analysis${query({ analysis_id: analysisId })}`, { signal }),
  preview: (id: string, zones: ReviewZone[], rules: ReviewRule[]) => api.request<RulePreview>(`${pathFor(id)}/preview`, { method: "POST", body: JSON.stringify({ zones, rules }) }),
  cases: (signal?: AbortSignal) => api.request<{ cases: CaseRecord[]; count: number }>("/cases", { signal }),
  case: (id: string, signal?: AbortSignal) => api.request<CaseRecord>(`/cases/${encodeURIComponent(id)}`, { signal }),
  createCase: (body: { video_id?: string; analysis_id?: string; event_id?: string; alert_id?: string; title?: string; annotation_set_id?: string; annotation_id?: string; evaluation_report_id?: string }) => api.request<CaseRecord>("/cases", { method: "POST", body: JSON.stringify(body) }),
  updateCase: (id: string, body: Record<string, unknown>) => api.request<CaseRecord>(`/cases/${encodeURIComponent(id)}`, { method: "PATCH", body: JSON.stringify(body) }),
  attachEvidence: (id: string, body: {expected_revision: number; alert_id?: string; video_id?: string; analysis_id?: string; event_id?: string; note?: string; annotation_set_id?: string; annotation_id?: string; evaluation_report_id?: string}) => api.request<CaseRecord>(`/cases/${encodeURIComponent(id)}/evidence`, {method: "POST", body: JSON.stringify(body)}),
  note: (id: string, text: string) => api.request<CaseNote>(`/cases/${encodeURIComponent(id)}/notes`, { method: "POST", body: JSON.stringify({ text }) }),
  linkMission: (id: string, missionId: string, revision: number) => api.request<CaseRecord>(`/cases/${encodeURIComponent(id)}/missions`, { method: "POST", body: JSON.stringify({ mission_id: missionId, expected_revision: revision }) }),
  search: (filters: SearchFilters, signal?: AbortSignal) => api.request<{ results: ReviewSearchResult[]; count: number; limit: number; possibly_truncated?: boolean; note?: string }>(`/review/search${query(filters as Record<string, unknown>)}`, { signal }),
  savedSearches: (signal?: AbortSignal) => api.request<{ saved_searches: SavedSearch[] }>("/review/saved-searches", { signal }),
  saveSearch: (name: string, filters: SearchFilters) => api.request<SavedSearch>("/review/saved-searches", { method: "POST", body: JSON.stringify({ name, query: filters, shared: false }) }),
  metrics: (signal?: AbortSignal) => api.request<ReviewMetrics>("/review/metrics", { signal }),
};

/** A missing requested version is an error; only a recording with no latest run may lack analysis. */
export async function loadVideoReview(id: string, signal: AbortSignal, analysisId?: string): Promise<[ReviewVideo, VideoAnalysis | null]> {
  return Promise.all([
    reviewApi.video(id, signal),
    reviewApi.analysis(id, signal, analysisId).catch(cause => {
      if (!analysisId && cause instanceof ApiError && cause.status === 404) return null;
      throw cause;
    }),
  ]);
}

/** Credentials may accompany only the API's own protected media/export paths. */
export function protectedUrl(path: string, origin: string): string {
  const url = new URL(path, origin);
  if (url.origin !== origin || (!url.pathname.startsWith("/api/review/") && !url.pathname.startsWith("/api/cases/") && !url.pathname.startsWith("/api/sites/") && !url.pathname.startsWith("/api/evidence-packages/") && !url.pathname.startsWith("/media/"))) throw new Error("This file is not a protected SIO media reference.");
  return url.toString();
}
export async function protectedBlob(path: string, signal?: AbortSignal, maxBytes = 150 * 1024 * 1024): Promise<Blob> {
  const url = protectedUrl(path, window.location.origin);
  let response: Response | undefined;
  for (let attempt = 0; attempt < 2; attempt++) {
    await session.ensure(attempt === 1);
    response = await fetch(url, { headers: session.headers(), signal, credentials: "omit" });
    if (response.status !== 401) break;
  }
  if (!response?.ok) {
    if (response?.status === 401) session.clear();
    let message = `File could not be loaded (HTTP ${response?.status ?? "unavailable"}).`;
    try { const body = await response?.json() as { detail?: string | { message?: string } }; if (typeof body?.detail === "string") message = body.detail; else if (body?.detail?.message) message = body.detail.message; } catch { /* Keep HTTP status when no JSON is available. */ }
    throw new ApiError(message, response?.status ?? 0, url);
  }
  return readBoundedBlob(response, maxBytes);
}
/** Bound both declared and streamed bytes, including responses without Content-Length. */
export async function readBoundedBlob(response: Response, maxBytes: number): Promise<Blob> {
  if (Number(response.headers.get("Content-Length")) > maxBytes) { await response.body?.cancel(); throw new Error("This media file exceeds the browser review size limit."); }
  if (!response.body) throw new Error("The media response contained no file.");
  const reader = response.body.getReader();
  const chunks: Uint8Array<ArrayBuffer>[] = [];
  let total = 0;
  try {
    for (;;) {
      const { done, value } = await reader.read(); if (done) break;
      total += value.byteLength;
      if (total > maxBytes) throw new Error("This media file exceeds the browser review size limit.");
      chunks.push(new Uint8Array(value));
    }
  } finally { await reader.cancel().catch(() => undefined); reader.releaseLock(); }
  return new Blob(chunks, { type: response.headers.get("Content-Type")?.split(";")[0] ?? "application/octet-stream" });
}
export async function downloadCase(id: string, format: "json" | "html"): Promise<void> {
  const blob = await protectedBlob(`/api/cases/${encodeURIComponent(id)}/export?format=${format}`, undefined, 20 * 1024 * 1024);
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a"); link.href = url; link.download = `case-${id.replace(/[^A-Za-z0-9_-]/g, "_")}.${format}`;
  document.body.appendChild(link); link.click(); link.remove(); window.setTimeout(() => URL.revokeObjectURL(url), 1000);
}
export async function validateUpload(file: File, maxBytes: number, maxDurationS: number): Promise<void> {
  if (!/\.mp4$/i.test(file.name) || (file.type && file.type !== "video/mp4")) throw new Error("Choose an MP4 video file.");
  if (!file.size || file.size > maxBytes) throw new Error(`Choose a non-empty MP4 smaller than ${Math.round(maxBytes / 1024 / 1024)} MB.`);
  // Browsers support fewer input codecs than the server transcoder. This probe is
  // advisory when metadata cannot be read; the upload endpoint always validates it.
  const url = URL.createObjectURL(file);
  const video = document.createElement("video"); video.preload = "metadata";
  try {
    const duration = await new Promise<number | null>((resolve) => {
      const finish = (value: number | null) => {
        window.clearTimeout(timer); video.onloadedmetadata = null; video.onerror = null; resolve(value);
      };
      const timer = window.setTimeout(() => finish(null), 10000);
      video.onloadedmetadata = () => finish(Number.isFinite(video.duration) ? video.duration : null);
      video.onerror = () => finish(null);
      video.src = url;
    });
    if (duration != null && (duration <= 0 || duration > maxDurationS)) throw new Error(`Video must be no longer than ${maxDurationS} seconds.`);
  } finally { video.onloadedmetadata = null; video.onerror = null; video.removeAttribute("src"); video.load(); URL.revokeObjectURL(url); }
}
