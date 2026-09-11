import { api } from './api';
import { protectedBlob } from './review-api';
import type { AnalysisModel, NormalizedPoint, ReviewZone } from './review-types';

export interface CountingLine { name: string; start: NormalizedPoint; end: NormalizedPoint }
export interface MovementConfiguration { line: CountingLine | null; class_name: string; min_confidence: number | null; zone_id: string }
export interface OccupancySample { at_s: number; count: number; frame_index: number; frame_url: string }
export interface MovementCrossing { at_s: number; from_s: number; to_s: number; direction: 'a_to_b' | 'b_to_a'; track_id: string; class_name: string; frame_url: string; frame_index: number }
export interface MovementSummary {
  configuration: MovementConfiguration; sample_count: number; occupancy: OccupancySample[];
  peak_count: number; peak_at_s: number | null; mean_sampled_occupancy: number | null;
  crossings: MovementCrossing[]; totals: { a_to_b: number; b_to_a: number; total: number }; limitations: string[];
}
export interface MovementReport { report_id: string; video_id: string; analysis_id: string; title: string; created_by: string; created_at: string; revision: number; configuration: MovementConfiguration; summary: MovementSummary; model: AnalysisModel }
export interface MovementReports { reports: MovementReport[]; limit: number; possibly_truncated: boolean }
const base = (id: string) => `/review/videos/${encodeURIComponent(id)}/movement`;
export const movementApi = {
  list: (videoId: string, analysisId: string, signal?: AbortSignal) => api.request<MovementReports>(`${base(videoId)}?${new URLSearchParams({ analysis_id: analysisId })}`, { signal }),
  preview: (videoId: string, analysisId: string, configuration: MovementConfiguration, signal?: AbortSignal) => api.request<MovementSummary>(`${base(videoId)}/preview`, { method: 'POST', body: JSON.stringify({ analysis_id: analysisId, configuration }), signal }),
  save: (videoId: string, analysisId: string, configuration: MovementConfiguration, title: string, signal?: AbortSignal) => api.request<MovementReport>(base(videoId), { method: 'POST', body: JSON.stringify({ analysis_id: analysisId, configuration, title: title.trim() }), signal }),
};

export function initialMovement(): MovementConfiguration { return { line: null, class_name: '', min_confidence: null, zone_id: '' }; }
/** Include invalid intermediate numeric input in the dirty key instead of aliasing it to null. */
export function movementKey(configuration: MovementConfiguration): string {
  const line = configuration.line;
  const canonical = { line: line ? { name: line.name, start: [...line.start], end: [...line.end] } : null, class_name: configuration.class_name, min_confidence: configuration.min_confidence, zone_id: configuration.zone_id };
  return JSON.stringify(canonical, (_key, value: unknown) => typeof value === 'number' && !Number.isFinite(value) ? 'invalid-number' : value);
}
export function movementProblem(configuration: MovementConfiguration, zones: ReviewZone[]): string | null {
  if (configuration.class_name && (!configuration.class_name.trim() || configuration.class_name !== configuration.class_name.trim())) return 'Choose an object class or All observed classes.';
  if (configuration.min_confidence != null && (!Number.isFinite(configuration.min_confidence) || configuration.min_confidence < 0 || configuration.min_confidence > 1)) return 'Minimum confidence must be between 0 and 1.';
  if (configuration.zone_id && !zones.some(zone => zone.zone_id === configuration.zone_id)) return 'Choose a zone retained with this analysis.';
  if (configuration.line) {
    const { name, start, end } = configuration.line;
    if (!name.trim() || name.length > 100) return 'Name the counting line using 1–100 characters.';
    if ([...start, ...end].some(value => !Number.isFinite(value) || value < 0 || value > 1)) return 'Line coordinates must be between 0 and 1.';
    if (Math.hypot(end[0] - start[0], end[1] - start[1]) < .02) return 'Place the line endpoints farther apart (at least 0.02 frame units).';
  }
  return null;
}
export function reverseCountingLine(line: CountingLine): CountingLine { return { ...line, start: [...line.end], end: [...line.start] }; }
/** Screen coordinates increase downwards: A is visually left of start → end. */
export function countingLineLabels(line: CountingLine): { a: NormalizedPoint; b: NormalizedPoint } | null {
  const dx = line.end[0] - line.start[0], dy = line.end[1] - line.start[1], length = Math.hypot(dx, dy);
  if (!Number.isFinite(length) || length < .02) return null;
  const x = (line.start[0] + line.end[0]) / 2, y = (line.start[1] + line.end[1]) / 2;
  const clamp = (value: number) => Math.min(.97, Math.max(.03, value));
  return { a: [clamp(x + dy / length * .08), clamp(y - dx / length * .08)], b: [clamp(x - dy / length * .08), clamp(y + dx / length * .08)] };
}
export function busiestSamples(samples: OccupancySample[], limit = 5): OccupancySample[] { return [...samples].filter(sample => sample.count > 0).sort((a, b) => b.count - a.count || a.at_s - b.at_s).slice(0, Math.max(0, limit)); }
export function movementTime(atS: number): string { const minutes = Math.floor(atS / 60); return `${minutes}:${(atS - minutes * 60).toFixed(2).padStart(5, '0')}`; }
export async function downloadMovement(reportId: string, signal?: AbortSignal): Promise<void> {
  const blob = await protectedBlob(`/api/review/movement/${encodeURIComponent(reportId)}/csv`, signal, 20 * 1024 * 1024);
  if (signal?.aborted) return;
  const url = URL.createObjectURL(blob), link = document.createElement('a');
  link.href = url; link.download = `movement-${reportId.replace(/[^A-Za-z0-9_-]/g, '_')}.csv`;
  document.body.appendChild(link); link.click(); link.remove(); window.setTimeout(() => URL.revokeObjectURL(url), 1000);
}
