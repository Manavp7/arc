import { api } from './api';

export interface SearchIndex {
  video_id: string; analysis_id: string; status: string; progress: number;
  sample_count: number; model_id: string; error: string | null;
}
export interface SearchRecording {
  video_id: string; title: string;
  analyses: { analysis_id: string; created_at: string }[];
  index: SearchIndex | null;
}
export interface RecordedSearchCatalog {
  videos: SearchRecording[]; model: { available: boolean; model_id: string; reason: string | null };
  busy: boolean; possibly_truncated: boolean; note: string;
}
export interface RecordedSample { video_id: string; analysis_id: string; frame_index: number }
export interface RecordedMatch extends RecordedSample {
  video_title: string; at_s: number; similarity: number; frame_url: string;
  events: { event_id: string; event_type: string }[];
}
export interface RecordedSearchPage {
  results: RecordedMatch[]; total_samples: number; searched_recordings: number;
  skipped_recordings: number; possibly_truncated: boolean; model_id: string; note: string;
}
export function sampleReference(row: RecordedMatch): RecordedSample {
  return { video_id: row.video_id, analysis_id: row.analysis_id, frame_index: row.frame_index };
}
export function recordedFramePath(row: RecordedSample): string | null {
  if (!/^vid_[a-f0-9]{32}$/.test(row.video_id) || !/^ana_[a-f0-9]{32}$/.test(row.analysis_id)
      || !Number.isInteger(row.frame_index) || row.frame_index < 0 || row.frame_index >= 360) return null;
  return `/api/review/videos/${row.video_id}/frames/${row.analysis_id}/${row.frame_index}`;
}
export function similarityLabel(value: number): string {
  return Number.isFinite(value) ? value.toFixed(3) : 'Unavailable';
}
const base = '/review/recorded-search';
export const recordedSearchApi = {
  catalog: (signal?: AbortSignal) => api.request<RecordedSearchCatalog>(`${base}/catalog`, { signal }),
  index: (video_id: string, analysis_id: string, consent: boolean) => {
    if (!consent) return Promise.reject(new Error('Confirm local processing of the private original before indexing.'));
    return api.request<SearchIndex>(`${base}/index`, { method: 'POST', body: JSON.stringify({ video_id, analysis_id, consent_private_original: true }) });
  },
  remove: (video_id: string) => api.request<{ removed: boolean }>(`${base}/index/${encodeURIComponent(video_id)}`, { method: 'DELETE' }),
  query: (text: string, sample: RecordedSample | null, video_id: string, signal?: AbortSignal) => {
    if (!sample && !text.trim()) return Promise.reject(new Error('Describe what you want to find.'));
    return api.request<RecordedSearchPage>(`${base}/query`, { method: 'POST', signal, body: JSON.stringify({ text: sample ? '' : text.trim(), sample, video_id, limit: 24 }) });
  },
};
