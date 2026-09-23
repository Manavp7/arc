/** Public activation views contain redacted configuration only. */
export type SourceActivationPreview = {
  preview_id: string;
  source_id: string;
  action: "activate" | "rollback";
  expires_at: string;
  before: Record<string, unknown> | null;
  after: Record<string, unknown> | null;
  changed_fields: string[];
  warnings: string[];
  requires_fresh_observation: boolean;
};
export type SourceActivationResult = {
  activation_id: string | null;
  source_id: string | null;
  status: "applying" | "recovering" | "verified" | "disabled" | "rolled_back" | "rollback_failed" | "recovered_after_restart" | null;
  message: string | null;
  rollback_available: boolean | null;
  verified_at: string | null;
  sample: Record<string, unknown> | null;
};
export function activationProblem(result: SourceActivationResult): boolean {
  return result.status === "rollback_failed" || (result.status === "rolled_back" && Boolean(result.message?.startsWith("Activation failed")));
}
export function activationAction(preview: SourceActivationPreview): string {
  return preview.action === "rollback" ? "Restore previous configuration" : preview.requires_fresh_observation ? "Activate and verify incoming data" : "Apply disabled state";
}
export function activationEndpoint(sourceId: string, operation: "preview" | "rollback-preview" | "activate" | "rollback" | "status"): string {
  const base = `/sources/${encodeURIComponent(sourceId)}/activation`;
  return operation === "activate" || operation === "status" ? base : `${base}/${operation}`;
}
