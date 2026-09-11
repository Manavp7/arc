import { api } from "./api";

export interface ObjectObservation {
  at_s: number;
  frame_index: number;
  frame_url: string | null;
  confidence: number | null;
  bbox: number[];
}
export interface RecordedObject {
  video_id: string;
  video_title: string;
  analysis_id: string;
  track_id: string;
  class_name: string;
  first_s: number;
  last_s: number;
  at_s: number;
  sample_count: number;
  confidence_min: number | null;
  confidence_max: number | null;
  frame_url: string | null;
  zone_ids: string[];
  zone_names: string[];
  observations: ObjectObservation[];
}
export interface ObjectPage {
  results: RecordedObject[];
  count: number;
  limit: number;
  offset: number;
  possibly_truncated: boolean;
  scan_truncated: boolean;
  note: string;
}
export interface ObjectCatalogAnalysis {
  analysis_id: string;
  created_at: string;
  model: { mode: string; name: string };
  zones: { zone_id: string; name: string; points: number[][] }[];
  classes: string[];
}
export interface ObjectCatalog {
  videos: { video_id: string; title: string; duration_s: number; analyses: ObjectCatalogAnalysis[] }[];
  possibly_truncated: boolean;
  note: string;
}
export interface ObjectFilters {
  video_id: string;
  analysis_id: string;
  class_name: string;
  zone_id: string;
  min_confidence: string;
  start_s: string;
  end_s: string;
}
export const emptyObjectFilters = (): ObjectFilters => ({ video_id: "", analysis_id: "", class_name: "", zone_id: "", min_confidence: "", start_s: "", end_s: "" });
export const OBJECT_PAGE_SIZE = 50;
const OBJECT_MAX_OFFSET = 50000;

/** Dependent filters belong to the selected recording and its frozen analysis. */
export function changeObjectFilter(filters: ObjectFilters, key: keyof ObjectFilters, value: string): ObjectFilters {
  if (key === "video_id" && filters.video_id !== value) return { ...filters, video_id: value, analysis_id: "", class_name: "", zone_id: "", start_s: "", end_s: "" };
  if (key === "analysis_id" && filters.analysis_id !== value) return { ...filters, analysis_id: value, class_name: "", zone_id: "" };
  return { ...filters, [key]: value };
}
export function objectAnalyses(catalog: ObjectCatalog | null, videoId: string): ObjectCatalogAnalysis[] {
  return [...(catalog?.videos.find(video => video.video_id === videoId)?.analyses ?? [])].sort((a, b) => b.created_at.localeCompare(a.created_at) || b.analysis_id.localeCompare(a.analysis_id));
}
export function objectFilterOptions(catalog: ObjectCatalog | null, filters: ObjectFilters) {
  const analyses = objectAnalyses(catalog, filters.video_id);
  const selected = filters.analysis_id ? analyses.find(analysis => analysis.analysis_id === filters.analysis_id) : analyses[0];
  const scoped = filters.video_id ? selected ? [selected] : [] : (catalog?.videos ?? []).flatMap(video => objectAnalyses(catalog, video.video_id).slice(0, 1));
  return { analyses, selected, classes: [...new Set(scoped.flatMap(analysis => analysis.classes))].sort(), zones: selected?.zones ?? [] };
}
/** A refreshed catalog cannot leave a zone or class attached to another analysis. */
export function reconcileObjectFilters(filters: ObjectFilters, catalog: ObjectCatalog): ObjectFilters {
  if (filters.video_id && !catalog.videos.some(video => video.video_id === filters.video_id)) return emptyObjectFilters();
  let next = { ...filters };
  const analyses = objectAnalyses(catalog, next.video_id);
  if (next.analysis_id && !analyses.some(analysis => analysis.analysis_id === next.analysis_id)) next = changeObjectFilter(next, "analysis_id", "");
  if (!next.video_id) next.analysis_id = "";
  const options = objectFilterOptions(catalog, next);
  if (!options.zones.some(zone => zone.zone_id === next.zone_id)) next.zone_id = "";
  if (!options.classes.includes(next.class_name)) next.class_name = "";
  return next;
}
export function objectFilterProblem(filters: ObjectFilters, duration?: number): string | null {
  if (filters.analysis_id && !filters.video_id) return "Choose a recording before choosing an analysis version.";
  if (filters.zone_id && !filters.video_id) return "Choose a recording before choosing a zone.";
  const number = (value: string) => value.trim() === "" ? null : Number(value);
  const confidence = number(filters.min_confidence), start = number(filters.start_s), end = number(filters.end_s);
  if (confidence !== null && (!Number.isFinite(confidence) || confidence < 0 || confidence > 1)) return "Minimum confidence must be between 0 and 1.";
  if ([start, end].some(value => value !== null && (!Number.isFinite(value) || value < 0))) return "Clip times must be non-negative seconds.";
  if (start !== null && end !== null && start > end) return "The end of the clip window must be at or after its start.";
  if ([start, end].some(value => value !== null && value > 180)) return "Clip times cannot exceed the 180-second recording limit.";
  if (duration !== undefined && [start, end].some(value => value !== null && value > duration)) return "The clip window extends beyond this recording's duration.";
  return null;
}
export function objectQuery(filters: ObjectFilters, offset = 0): string {
  const problem = objectFilterProblem(filters);
  if (problem) throw new Error(problem);
  const query = new URLSearchParams({ limit: String(OBJECT_PAGE_SIZE), offset: String(Math.min(OBJECT_MAX_OFFSET, Math.max(0, Number.isFinite(offset) ? Math.floor(offset) : 0))) });
  for (const [key, value] of Object.entries(filters)) if (value.trim() !== "") query.set(key, value.trim());
  return query.toString();
}
export function sameObjectFilters(a: ObjectFilters, b: ObjectFilters): boolean { return Object.keys(a).every(key => a[key as keyof ObjectFilters] === b[key as keyof ObjectFilters]); }
export function objectPageLabel(page: ObjectPage): string {
  if (!page.results.length) return page.offset ? "No tracks on this page" : "No matching tracks";
  const range = `${page.offset + 1}–${page.offset + page.results.length}`;
  return `${range} of ${page.scan_truncated ? "at least " : ""}${page.count} matching ${page.count === 1 ? "track" : "tracks"}`;
}
export function nextObjectOffset(page: ObjectPage): number | null {
  const next = page.offset + page.limit;
  return page.results.length > 0 && next < page.count && next <= OBJECT_MAX_OFFSET ? next : null;
}
/** Only server-returned observations are seek targets; never interpolate a missing frame. */
export function objectObservations(row: RecordedObject): ObjectObservation[] {
  const observations = new Map<string, ObjectObservation>();
  for (const observation of row.observations) if (Number.isFinite(observation.at_s) && observation.at_s >= 0 && Number.isInteger(observation.frame_index) && observation.frame_index >= 0) observations.set(`${observation.at_s}:${observation.frame_index}`, observation);
  return [...observations.values()].sort((a, b) => a.at_s - b.at_s || a.frame_index - b.frame_index);
}
export function objectFramePath(row: RecordedObject, observation: ObjectObservation | undefined): string | null {
  if (!observation || !Number.isInteger(observation.frame_index) || observation.frame_index < 0) return null;
  const expected = `/api/review/videos/${encodeURIComponent(row.video_id)}/frames/${encodeURIComponent(row.analysis_id)}/${observation.frame_index}`;
  return observation.frame_url === expected ? expected : null;
}
export function objectConfidence(value: number | null): string { return value !== null && Number.isFinite(value) ? `${Math.round(value * 100)}%` : "Not measured"; }
export function objectTrackKey(row: Pick<RecordedObject, "video_id" | "analysis_id" | "track_id" | "class_name">): string { return JSON.stringify([row.video_id, row.analysis_id, row.track_id, row.class_name]); }
export const objectExplorerApi = {
  catalog: (signal?: AbortSignal) => api.request<ObjectCatalog>("/review/objects/catalog", { signal }),
  search: (filters: ObjectFilters, offset = 0, signal?: AbortSignal) => api.request<ObjectPage>(`/review/objects?${objectQuery(filters, offset)}`, { signal }),
};
