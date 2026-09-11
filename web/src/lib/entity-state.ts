import type { Entity, SioEvent } from "../types";

/** Older deliveries may extend history, but must never move the current position backwards. */
export function mergeEntity(previous: Entity | undefined, incoming: Entity): Entity {
  if (!previous) return incoming;
  const newest = Date.parse(previous.last_seen) > Date.parse(incoming.last_seen) ? previous : incoming;
  return { ...newest, first_seen: Date.parse(previous.first_seen) < Date.parse(incoming.first_seen) ? previous.first_seen : incoming.first_seen,
    last_seen: newest.last_seen };
}
export function entityIsActive(entity: Entity, windowS: number, nowMs: number): boolean {
  return entity.is_static || Date.parse(entity.last_seen) >= nowMs - windowS * 1000;
}
export function replayEvents(previous: SioEvent[], incoming: SioEvent[], ts: string, append: boolean): SioEvent[] {
  const seen = new Set<string>();
  const at = Date.parse(ts);
  return [...incoming, ...(append ? previous : [])]
    .filter((event) => Date.parse(event.ts) <= at)
    .sort((a, b) => Date.parse(b.ts) - Date.parse(a.ts))
    .filter((event) => { if (seen.has(event.event_id)) return false; seen.add(event.event_id); return true; })
    .slice(0, 60);
}
