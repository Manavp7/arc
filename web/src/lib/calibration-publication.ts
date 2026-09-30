import { api } from "./api";

export interface CalibrationPreview {
  preview_id: string; operation: "apply" | "rollback"; setup_id: string; setup_revision: number;
  source_id: string; expires_at: string; previous_pose: Record<string, unknown> | null;
  proposed_pose: Record<string, unknown> | null; note: string;
}
export interface CalibrationPublication {
  publication_id: string | null; status: "not_published" | "pending_fusion" | "applied" | "rolled_back" | "superseded";
  source_id: string; calibration_revision: number | null; applied_at: string | null; applied_by: string | null;
  fusion: {acknowledged: boolean; acknowledged_at: string | null}; can_rollback: boolean; note: string;
}
const endpoint = (setupId: string, action: string) => `/camera-setups/${encodeURIComponent(setupId)}/calibration/${action}`;
export const calibrationApi = {
  status: (id: string, signal?: AbortSignal) => api.request<CalibrationPublication>(endpoint(id,"status"), {signal}),
  preview: (id: string, revision: number, rollback = false, signal?: AbortSignal) => api.request<CalibrationPreview>(endpoint(id,rollback ? "rollback-preview" : "preview"), {method:"POST",body:JSON.stringify({expected_revision:revision}),signal}),
  apply: (preview: CalibrationPreview, signal?: AbortSignal) => api.request<CalibrationPublication>(endpoint(preview.setup_id,preview.operation === "rollback" ? "rollback" : "apply"), {method:"POST",body:JSON.stringify({preview_id:preview.preview_id,expected_revision:preview.setup_revision}),signal}),
};
export function calibrationPreviewUsable(preview: CalibrationPreview, setupId: string, revision: number, now = Date.now()): boolean {
  return preview.setup_id === setupId && preview.setup_revision === revision && Date.parse(preview.expires_at) > now;
}
