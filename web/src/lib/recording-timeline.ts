import { api } from "./api";
export interface RecordingClock {
  revision: number; camera_id: string; capture_started_at: string | null; clock_offset_s: number;
  uncertainty_s: number | null; note: string; declared_by?: string; updated_at?: string;
}
export interface TimelineRecording {
  video_id: string; title: string; duration_s: number; analysis_id: string | null;
  clock: RecordingClock | null; interval: { start: string; end: string } | null;
}
export interface RecordingTimeline { recordings: TimelineRecording[]; possibly_truncated?: boolean; note: string; next_cursor?: string | null; camera_ids?: string[] }
export interface TimelineFilters { camera_id?: string; from?: string; to?: string; cursor?: string; limit?: number }
export function timelineQuery(filters: TimelineFilters = {}): string {
  const query = new URLSearchParams({limit: String(filters.limit ?? 20)});
  for (const key of ["camera_id", "from", "to", "cursor"] as const) if (filters[key]) query.set(key, filters[key]!);
  return `?${query}`;
}
export function timelineFilterValues(camera: string, from: string, to: string): TimelineFilters {
  const start = from ? new Date(`${from}Z`) : null, end = to ? new Date(`${to}Z`) : null;
  if ((start && !Number.isFinite(start.getTime())) || (end && !Number.isFinite(end.getTime()))) throw new Error("Enter valid UTC dates.");
  if (start && end && start >= end) throw new Error("The end must be later than the start.");
  return {camera_id: camera.trim() || undefined, from: start?.toISOString(), to: end?.toISOString()};
}
export const recordingTimelineApi = {
  list: (signal?: AbortSignal, filters: TimelineFilters = {}) => api.request<RecordingTimeline>(`/review/recording-timeline${timelineQuery(filters)}`, { signal }),
  save: (id: string, clock: RecordingClock) => api.request<RecordingClock>(`/review/videos/${encodeURIComponent(id)}/clock`, { method: "PUT", body: JSON.stringify(clock) }),
};
export function clipOffset(recording: TimelineRecording, utcMs: number): number | null {
  if (!recording.interval || !Number.isFinite(utcMs)) return null;
  const start = Date.parse(recording.interval.start), end = Date.parse(recording.interval.end);
  if (!Number.isFinite(start) || !Number.isFinite(end) || utcMs < start || utcMs >= end) return null;
  return Math.min(recording.duration_s, Math.max(0, (utcMs - start) / 1000));
}
export function timelineBounds(rows: TimelineRecording[]): [number, number] | null {
  const known = rows.filter(row => row.interval && Number.isFinite(Date.parse(row.interval.start)) && Number.isFinite(Date.parse(row.interval.end)));
  return known.length ? [Math.min(...known.map(row => Date.parse(row.interval!.start))), Math.max(...known.map(row => Date.parse(row.interval!.end)))] : null;
}
