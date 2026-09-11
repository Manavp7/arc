import type { Alert, Entity, SioEvent, Zone } from "../types";

export interface ConsoleSnapshot {
  entities?: Entity[];
  events?: SioEvent[];
  zones?: Zone[];
  alerts?: Alert[];
  failures: string[];
}
/** A failed optional service must not discard a healthy world-model snapshot. */
export async function settleConsoleSnapshot(requests: {
  entities: Promise<Entity[]>;
  events: Promise<SioEvent[]>;
  zones: Promise<Zone[]>;
  alerts: Promise<{ alerts: Alert[] }>;
}): Promise<ConsoleSnapshot> {
  const [entities, events, zones, alerts] = await Promise.allSettled([
    requests.entities, requests.events, requests.zones, requests.alerts,
  ]);
  const result: ConsoleSnapshot = { failures: [] };
  const describe = (name: string, reason: unknown) => `${name}: ${reason instanceof Error ? reason.message : String(reason)}`;
  if (entities.status === "fulfilled") result.entities = entities.value;
  else result.failures.push(describe("Entities", entities.reason));
  if (events.status === "fulfilled") result.events = events.value;
  else result.failures.push(describe("Events", events.reason));
  if (zones.status === "fulfilled") result.zones = zones.value;
  else result.failures.push(describe("Zones", zones.reason));
  if (alerts.status === "fulfilled") result.alerts = alerts.value.alerts;
  else result.failures.push(describe("Alerts", alerts.reason));
  return result;
}

/** Acknowledgements and resolutions may leave last_ts unchanged. Object identity records which
 * alerts changed in the store while this request was in flight, including locally confirmed actions. */
export function mergeAlertSnapshot(
  incoming: Alert[],
  current: Alert[],
  atRequestStart: ReadonlyMap<string, Alert>,
): Alert[] {
  const existing = new Map(current.map((alert) => [alert.alert_id, alert]));
  const merged = new Map<string, Alert>();
  for (const row of incoming) {
    const latest = existing.get(row.alert_id);
    merged.set(row.alert_id, latest && latest !== atRequestStart.get(row.alert_id) ? latest : row);
  }
  for (const row of current) if (!merged.has(row.alert_id)) merged.set(row.alert_id, row);
  return [...merged.values()];
}
