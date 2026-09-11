export type MeasuredPose = { lat: number; lon: number; bearing_deg: number; height_m: number; tilt_deg: number; fov_deg: number; vfov_deg: number; frame_width: number; frame_height: number; range_m: number };
export type CameraCheckpoint = { checkpoint_id: string; label: string; image_x: number; image_y: number; measured_east_m: number; measured_north_m: number };
export type SetupCamera = { camera_id: string; name: string; source_id: string; pose?: Omit<MeasuredPose, "range_m"> | null };
export type SetupSite = { site_id: string; revision: number; name: string; cameras: SetupCamera[] };
export type SetupSource = { source_id: string; kind: string; label: string; status: string; enabled: boolean; active: boolean; restart_required: boolean; last_success?: string; error?: string; last_test?: { ok?: boolean; message?: string; tested_at?: string } };
export type SetupValidation = { passed: boolean; checkpoint_count: number; rmse_m: number | null; max_error_m: number | null; tolerance_m: number; problems: string[]; limitations: string[]; validated_at: string; checkpoints: (CameraCheckpoint & { error_m: number | null; passed: boolean })[] };
export type CameraSetupRecord = { setup_id: string; title: string; source_id: string; site_id: string; camera_id: string; pose: MeasuredPose; checkpoints: CameraCheckpoint[]; tolerance_m: number; measurement_note: string; revision: number; status: "draft" | "validated"; validation: SetupValidation | null; updated_by: string; activation: "not_applied" };
export const poseFields: [keyof MeasuredPose, string][] = [["lat", "Latitude"], ["lon", "Longitude"], ["bearing_deg", "Bearing °"], ["height_m", "Height above ground m"], ["tilt_deg", "Downward tilt °"], ["fov_deg", "Horizontal FOV °"], ["vfov_deg", "Vertical FOV °"], ["frame_width", "Frame width px"], ["frame_height", "Frame height px"], ["range_m", "Projection range m"]];
export type CheckpointDraft = { checkpoint_id: string; label: string; image_x: string; image_y: string; measured_east_m: string; measured_north_m: string };
export type SetupDraft = { title: string; source_id: string; site_id: string; camera_id: string; pose: Partial<Record<keyof MeasuredPose, string>>; checkpoints: CheckpointDraft[]; tolerance_m: string; measurement_note: string };
export function setupDraft(record?: CameraSetupRecord): SetupDraft {
  return { title: record?.title ?? "", source_id: record?.source_id ?? "", site_id: record?.site_id ?? "", camera_id: record?.camera_id ?? "", pose: Object.fromEntries(Object.entries(record?.pose ?? {}).map(([key, value]) => [key, String(value)])), checkpoints: (record?.checkpoints ?? []).map(point => ({...point, image_x: String(point.image_x), image_y: String(point.image_y), measured_east_m: String(point.measured_east_m), measured_north_m: String(point.measured_north_m)})), tolerance_m: String(record?.tolerance_m ?? 2), measurement_note: record?.measurement_note ?? "" };
}
function measuredNumber(value: string | undefined, label: string): number { if (!value?.trim() || !Number.isFinite(Number(value))) throw new Error(`Enter a measured number for ${label}.`); return Number(value); }
export function setupBody(draft: SetupDraft, revision: number) {
  if (!draft.title.trim() || !draft.source_id || !draft.site_id || !draft.camera_id) throw new Error("Choose an existing camera source, site and camera, and name the setup.");
  return {...draft, title: draft.title.trim(), pose: Object.fromEntries(poseFields.map(([key, label]) => [key, measuredNumber(draft.pose[key], label)])), checkpoints: draft.checkpoints.map(point => ({...point, image_x: measuredNumber(point.image_x, `${point.label} image X`), image_y: measuredNumber(point.image_y, `${point.label} image Y`), measured_east_m: measuredNumber(point.measured_east_m, `${point.label} surveyed east distance`), measured_north_m: measuredNumber(point.measured_north_m, `${point.label} surveyed north distance`)})), tolerance_m: measuredNumber(draft.tolerance_m, "tolerance"), expected_revision: revision};
}

export type EvidenceAnnotation = { at_s: number; text: string };
/** Case lists omit bulky evidence snapshots; only detail responses carry them. */
export function recordedCaseSummaries<T extends {video_id?: string | null; analysis_id?: string | null}>(rows: T[]): T[] {
  return rows.filter(row => Boolean(row.video_id && row.analysis_id));
}
export function clipProblem(start: number, end: number, duration: number, annotations: EvidenceAnnotation[]): string | null {
  if (![start, end, duration].every(Number.isFinite) || duration <= 0) return "The recording duration is unavailable.";
  if (start < 0 || end > duration || end - start < 0.1 || end - start > 60) return "Choose a clip of 0.1–60 seconds within the recording.";
  if (annotations.some(note => !Number.isFinite(note.at_s) || note.at_s < start || note.at_s > end || !note.text.trim() || note.text.length > 2000)) return "Each annotation needs text and a time inside the selected clip.";
  return null;
}
