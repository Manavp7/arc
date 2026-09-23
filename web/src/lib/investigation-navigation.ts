/** Only record identifiers and clip offsets enter URLs; every record is reauthorized by the API. */
export const TAB_WORKSPACE = {
  alerts: "monitor", events: "monitor", analytics: "monitor",
  footage: "review", objects: "review", "visual-search": "review", "recording-timeline": "review",
  evaluation: "review", queue: "review", inbox: "review", cases: "review", compare: "review",
  evidence: "review", search: "review", quality: "review",
  incident: "investigate", copilot: "investigate", forecast: "investigate", twin: "investigate",
  decisions: "respond", missions: "respond", playbooks: "respond",
  sources: "admin", site: "admin", camera: "admin", storage: "admin", system: "admin",
  builder: "admin", deliveries: "admin",
} as const;
export type RailTab = keyof typeof TAB_WORKSPACE;
export type Workspace = typeof TAB_WORKSPACE[RailTab];
export interface InvestigationLocation {
  tab: RailTab; videoId?: string; analysisId?: string; atS?: number;
  caseId?: string; missionId?: string; packageId?: string; siteId?: string; alertId?: string;
}
const validId = (value: string | null) => value && /^[A-Za-z0-9_.-]{1,160}$/.test(value) ? value : undefined;

export function decodeNavigation(search: string): InvestigationLocation | null {
  const params = new URLSearchParams(search);
  const tab = params.get("view");
  if (!tab || !Object.hasOwn(TAB_WORKSPACE, tab)) return null;
  const result: InvestigationLocation = { tab: tab as RailTab };
  if (tab === "footage") {
    result.videoId = validId(params.get("video"));
    if (result.videoId) {
      result.analysisId = validId(params.get("analysis"));
      const at = params.get("at");
      const value = Number(at);
      if (at !== null && at.trim() !== "" && Number.isFinite(value) && value >= 0 && value <= 180) result.atS = value;
    }
  }
  if (["cases", "compare", "evidence"].includes(tab)) result.caseId = validId(params.get("case"));
  if (tab === "evidence" && result.caseId) result.packageId = validId(params.get("package"));
  if (tab === "missions") result.missionId = validId(params.get("mission"));
  if (["site", "camera"].includes(tab)) result.siteId = validId(params.get("site"));
  if (tab === "incident") result.alertId = validId(params.get("alert"));
  return result;
}

export function encodeNavigation(location: InvestigationLocation): string {
  const params = new URLSearchParams({ view: location.tab });
  for (const [name, value] of Object.entries({ video: location.videoId, analysis: location.analysisId,
    case: location.caseId, package: location.packageId, mission: location.missionId, site: location.siteId,
    alert: location.alertId, at: location.atS })) {
    if (value !== undefined) params.set(name, String(value));
  }
  // Normalize and remove fields belonging to a different workspace.
  const clean = decodeNavigation(params.toString()) ?? { tab: "alerts" as RailTab };
  const output = new URLSearchParams({ view: clean.tab });
  for (const [name, value] of Object.entries({ video: clean.videoId, analysis: clean.analysisId,
    case: clean.caseId, package: clean.packageId, mission: clean.missionId, site: clean.siteId,
    alert: clean.alertId, at: clean.atS })) {
    if (value !== undefined) output.set(name, String(value));
  }
  return `?${output}`;
}

export function navigationIdentity(location: InvestigationLocation): string {
  return encodeNavigation({ ...location, atS: undefined });
}

export function resumeKey(identity: { tenant: string; subject: string } | null): string | null {
  return identity && identity.subject !== "anonymous"
    ? `sio.investigation.v1:${JSON.stringify([identity.tenant, identity.subject])}` : null;
}

/** Renewal alone preserves the view; changed access discards already-loaded protected records. */
export function authorizationKey(identity: { tenant: string; subject: string; roles: string[]; clearance: number } | null, claims: Record<string, unknown>): string {
  const zones = Array.isArray(claims.zones) ? claims.zones.map(String) : typeof claims.zones === "string" ? claims.zones.split(",").map(zone => zone.trim()).filter(Boolean) : [];
  return JSON.stringify([identity?.tenant, identity?.subject, [...new Set(identity?.roles ?? [])].sort(), identity?.clearance, [...new Set(zones)].sort(), Boolean(claims.pii_scope || claims.pii)]);
}

export function readResume(key: string | null): InvestigationLocation | null {
  if (!key) return null;
  try { return decodeNavigation(localStorage.getItem(key) ?? ""); } catch { return null; }
}

export function rememberLocation(key: string | null, location: InvestigationLocation): void {
  if (!key) return;
  try { localStorage.setItem(key, encodeNavigation(location)); } catch { /* Storage may be disabled. */ }
}
