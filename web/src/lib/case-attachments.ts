import type { CaseEvidenceItem, CaseRecord, ReviewSearchResult } from "./review-types";

/** Details carry immutable snapshots; older cases still have one original source. */
export function caseEvidenceItems(record: CaseRecord): CaseEvidenceItem[] {
  if (record.evidence_items?.length) return record.evidence_items;
  return [{attachment_id: "original", attached_by: record.created_by, attached_at: record.created_at, title: record.title, note: "Original case evidence", video_id: record.video_id, analysis_id: record.analysis_id, event_id: record.event_id, alert_id: record.alert_id, annotation_set_id: record.annotation_set_id, annotation_id: record.annotation_id, evaluation_report_id: record.evaluation_report_id, at_s: record.at_s, evidence: record.evidence}, ...(record.evidence_attachments ?? [])];
}
/** List responses contain counts and origin IDs, never the full evidence payload. */
export function recordedEvidenceCases<T extends {recorded_evidence_count?: number; video_id?: string | null; analysis_id?: string | null}>(rows: T[]): T[] {
  return rows.filter(row => row.recorded_evidence_count != null ? row.recorded_evidence_count > 0 : Boolean(row.video_id && row.analysis_id));
}
export function attachmentRequest(result: ReviewSearchResult, revision: number, note: string) {
  if (result.kind === "alert") return {expected_revision: revision, alert_id: result.id, note: note.trim()};
  if (result.kind === "video_event" && result.video_id && result.analysis_id) return {expected_revision: revision, video_id: result.video_id, analysis_id: result.analysis_id, event_id: result.id, note: note.trim()};
  throw new Error("Choose a persisted recorded event or platform alert.");
}
export function recordedEvidenceItems(record: CaseRecord): CaseEvidenceItem[] {
  return caseEvidenceItems(record).filter(item => item.video_id && item.analysis_id && ["recorded_file", "reviewer_annotation"].includes(item.evidence?.source));
}

export function evidencePosition(item: CaseEvidenceItem): number {
  return item.evidence.source === "reviewer_annotation" ? item.evidence.annotation?.start_s ?? item.at_s ?? 0 : item.evidence.event?.at_s ?? item.at_s ?? 0;
}
export function evidenceOriginLabel(item: CaseEvidenceItem): string {
  if (item.evidence.source === "reviewer_annotation") return item.evidence.evaluation_report ? "Human observation · evaluated miss" : "Human observation";
  return item.evidence.source === "recorded_file" ? "Detected recording event" : "Platform alert";
}
