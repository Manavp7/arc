import { claimsOf, type Session } from "./session";

/** Access zones are authorization metadata, never polygons drawn on the image. */
export function uploadAccessZones(identity: Session | null): string[] {
  if (!identity || identity.roles.includes("admin")) return [];
  const value = claimsOf(identity.token).zones;
  const zones = Array.isArray(value) ? value.map(String) : typeof value === "string" ? value.split(",") : [];
  const normalized = [...new Set(zones.map(zone => zone.trim()).filter(Boolean))];
  return normalized.includes("*") ? [] : normalized;
}
