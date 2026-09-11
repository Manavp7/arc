export type NormalizedPoint = [number, number];
export interface ReviewZone { zone_id: string; name: string; points: NormalizedPoint[] }
export interface ReviewRule { rule_id: string; name: string; zone_id: string; event_type: "entry" | "dwell"; target_class: string; threshold_s: number; cooldown_s: number; enabled: boolean }
export interface ReviewVideo {
  video_id: string; id?: string; title: string; duration_s: number; width: number; height: number; fps?: number; playback_fps?: number | null;
  status: "ready" | "queued" | "analyzing" | "completed" | "failed" | "interrupted" | "cancelled" | "purged"; media_url: string; poster_url?: string;
  revision: number; zones: ReviewZone[]; rules: ReviewRule[]; analysis_id: string | null;
  privacy: string; source: string; created_at?: string; updated_at?: string;
}
export interface AnalysisOptions { mode: "auto" | "motion" | "onnx"; confidence_threshold?: number; sample_fps: 1 | 2 }
export interface AnalysisProfile { requested_mode: "auto" | "motion" | "onnx"; resolved_mode: "motion" | "onnx"; confidence_threshold?: number | null; sample_fps: 1 | 2; model_id: string; weights_sha256?: string | null; [key: string]: unknown }
export interface AnalysisProfileCapabilities { models: {mode:"motion"|"onnx";name:string;available:boolean;weights_sha256?:string;unavailable_reason?:string;classes?:string[]}[]; sample_fps: (1|2)[]; confidence_threshold: {min:number;max:number;default:number}; default_mode:"auto"|"motion"|"onnx" }
export interface VideoCapabilities { upload: boolean; max_bytes: number; max_duration_s: number; privacy: string; motion_detection: boolean; onnx_available: boolean; max_width?: number; max_height?: number; max_videos?: number; sample_fps?: number; analysis_profiles?: AnalysisProfileCapabilities }
export interface VideoLibrary { videos: ReviewVideo[]; capabilities: VideoCapabilities }
export interface DetectionObject { track_id: string; class_name: string; confidence: number | null; bbox: [number, number, number, number] }
export interface DetectionFrame { at_s: number; frame_index: number; frame_url: string; objects: DetectionObject[] }
export interface VideoEvent { event_id: string; at_s: number; end_s?: number; zone_id: string; rule_id: string; title: string; track_id: string; confidence: number | null; frame_url?: string }
export interface AnalysisModel { mode: "pending" | "motion" | "onnx"; name: string; warning?: string; weights_sha256?: string; confidence_threshold?: number | null; sample_fps?: number }
export interface VideoAnalysis {
  analysis_id: string; video_id: string; status: "queued" | "running" | "completed" | "failed" | "interrupted" | "cancelled";
  progress: number; zones: ReviewZone[]; rules: ReviewRule[]; model: AnalysisModel; profile?: AnalysisProfile; detections: DetectionFrame[]; events: VideoEvent[]; error?: string | null;
}
export interface RulePreview { events: VideoEvent[]; model: AnalysisModel; preview: boolean }
export interface CaseNote { note_id: string; text: string; author: string; created_at: string }
export interface CaseTimeline { kind: string; ts: string; author: string; summary: string; ref?: string }
export interface CaseEvidenceItem {
  annotation_set_id?: string | null; annotation_id?: string | null; evaluation_report_id?: string | null;
  title?: string; source_id?: string | null; timeline_at?: string;
  time_basis?: "platform_source_time" | "attachment_time_recording_clock_unknown" | "attachment_time_source_clock_unknown";
  attachment_id: string; attached_by: string; attached_at: string; note?: string;
  video_id?: string | null; analysis_id?: string | null; event_id?: string | null; alert_id?: string | null;
  at_s?: number | null; zone_id?: string | null; evidence_zone_ids?: string[];
  evidence: CaseRecord["evidence"];
}
export interface CaseRecord {
  annotation_set_id?: string | null; annotation_id?: string | null; evaluation_report_id?: string | null;
  evidence_count?: number;
  recorded_evidence_count?: number;
  evidence_attachments?: CaseEvidenceItem[];
  evidence_items?: CaseEvidenceItem[];
  priority?: "urgent" | "high" | "normal" | "low";
  case_id: string; record_id?: string; title: string; status: "open" | "investigating" | "resolved";
  owner: string | null; due_at: string | null; summary: string; verdict: "unreviewed" | "confirmed" | "false_positive";
  resolution_note?: string; video_id?: string | null; analysis_id?: string | null; event_id?: string | null; alert_id?: string | null;
  at_s?: number | null; mission_ids: string[]; revision: number; created_by: string; created_at: string; updated_at: string;
  first_response_at?: string | null; resolved_at?: string | null;
  evidence: { source: string; event?: VideoEvent; annotation?: {annotation_id:string;zone_id:string;event_type:"entry"|"dwell";start_s:number;end_s:number;note:string}; annotation_set?: {annotation_set_id:string;annotation_hash:string;frozen_by?:string;frozen_at:string;draft_revision:number;footage_origin?:string;partition?:string;[key:string]:unknown}; evaluation_report?: {report_id?:string;[key:string]:unknown}; video?: { video_id: string; title: string; duration_s: number; privacy: string; source: string }; model?: AnalysisModel; captured_at?: string; resolved_frames?: { frame_id: string; source_id: string; ts: string; media_url: string; redacted: boolean; width: number; height: number; evidence_refs: string[] }[]; unresolved_media?: { kind: string; ref: string; reason: string }[]; media_note?: string; [key: string]: unknown };
  timeline: CaseTimeline[]; notes?: CaseNote[]; missions?: { mission_id: string; name: string; state: string }[];
  decisions?: { decision_id: string; rationale?: string; approval?: string; [key: string]: unknown }[];
}
export interface SearchFilters { q?: string; kind?: string; source_id?: string; zone_id?: string; since?: string; until?: string; limit?: number }
export interface ReviewSearchResult { kind: string; id: string; title: string; ts: string; source_id?: string | null; zone_id?: string | null; video_id?: string | null; analysis_id?: string | null; case_id?: string | null; at_s?: number | null; summary?: string }
export interface SavedSearch { search_id: string; record_id?: string; name: string; query: SearchFilters; shared: boolean; owner: string; revision: number }
export interface ReviewMetrics {
  detector_counts?: {total:number;confirmed:number;false_positive:number;unreviewed:number};
  reviewer_annotation_counts?: {total:number;confirmed:number;false_positive:number;unreviewed:number};
  detector_reviewed_count?: number; reviewer_annotation_reviewed_count?: number;
  counts: { total: number; confirmed: number; false_positive: number; unreviewed: number; open: number; investigating: number; resolved: number };
  reviewed_precision: number | null; median_response_seconds: number | null; median_resolution_seconds: number | null;
  recall: null; limitations: string[]; reviewed_count?: number; response_sample_count?: number; resolution_sample_count?: number; scan_limit?: number; possibly_truncated?: boolean;
}
