import { api } from "./api";
import type { AnnotationSet, ClipEvaluation, EvaluationReport } from "./evaluation";
import type { CaseRecord, VideoAnalysis } from "./review-types";

export interface AnnotationCaseSource {
  video_id: string;
  analysis_id: string;
  annotation_set_id: string;
  annotation_id: string;
  evaluation_report_id?: string;
}

/** A mutable label draft can never supply a case source: choose its frozen set and retained run. */
export function frozenAnnotationCase(videoId: string, frozen: AnnotationSet | undefined, annotationId: string, analysis: VideoAnalysis | undefined): AnnotationCaseSource {
  if (!frozen?.annotation_set_id || !frozen.annotation_hash || !frozen.frozen_at) throw new Error("Save and freeze the annotations before opening a case.");
  if (frozen.video_id && frozen.video_id !== videoId) throw new Error("The frozen annotations belong to a different recording.");
  if (!analysis || analysis.status !== "completed" || analysis.video_id !== videoId) throw new Error("Choose a completed analysis from this recording to retain as context.");
  if (!frozen.annotations.some(item => item.annotation_id === annotationId)) throw new Error("Choose an observation from the selected frozen annotation set.");
  return { video_id: videoId, analysis_id: analysis.analysis_id, annotation_set_id: frozen.annotation_set_id, annotation_id: annotationId };
}

/** Read the miss from the immutable report itself, not from an editable label or a displayed index. */
export function reportMissCase(report: EvaluationReport, clip: ClipEvaluation, annotationId: string): AnnotationCaseSource {
  const retained = report.candidates.flatMap(candidate => candidate.clips).find(item => item.video_id === clip.video_id && item.analysis_id === clip.analysis_id && item.annotation_set_id === clip.annotation_set_id && item.misses.some(annotation => annotation.annotation_id === annotationId));
  if (!report.report_id || !retained) throw new Error("This observation is not a missed incident in the selected saved report.");
  return { video_id: retained.video_id, analysis_id: retained.analysis_id, annotation_set_id: retained.annotation_set_id, annotation_id: annotationId, evaluation_report_id: report.report_id };
}

/** Backend identity reuses the same frozen observation/run even when opened through another report. */
export function annotationCaseKey(source: AnnotationCaseSource): string {
  return JSON.stringify([source.video_id, source.analysis_id, source.annotation_set_id, source.annotation_id]);
}

export const annotationCasesApi = {
  create: (source: AnnotationCaseSource) => api.request<CaseRecord>("/cases", { method: "POST", body: JSON.stringify(source) }),
  attach: (caseId: string, source: AnnotationCaseSource, revision: number, note: string) => api.request<CaseRecord>(`/cases/${encodeURIComponent(caseId)}/evidence`, { method: "POST", body: JSON.stringify({ ...source, expected_revision: revision, note: note.trim() }) }),
};
