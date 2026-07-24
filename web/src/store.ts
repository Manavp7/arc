/**
 * Client state.
 *
 * Zustand rather than Redux or context: the live map replaces entity positions several times a
 * second, and what matters is that a position update re-renders the map layer without
 * re-rendering the copilot panel. A single store with selector-based subscriptions gives that
 * with no boilerplate.
 *
 * Entities are held in a Map keyed by id (not an array) because the stream delivers upserts, and
 * an array would mean a linear scan per message.
 */

import { create } from "zustand";
import type { Alert, Decision, Entity, Forecast, Mission, SioEvent, Zone } from "./types";

export type ConnectionStatus = "connecting" | "live" | "reconnecting" | "closed";

const MAX_EVENTS = 500;

interface SioState {
  connection: ConnectionStatus;
  entities: Map<string, Entity>;
  events: SioEvent[];
  alerts: Alert[];
  decisions: Decision[];
  forecasts: Forecast[];
  missions: Mission[];
  zones: Zone[];
  selectedEntityId: string | null;
  /** When set, the UI shows the world as it was at this instant instead of live (UC5). */
  replayAt: string | null;
  lastMessageAt: string | null;

  setConnection: (status: ConnectionStatus) => void;
  upsertEntity: (entity: Entity) => void;
  upsertEntities: (entities: Entity[]) => void;
  addEvent: (event: SioEvent) => void;
  setEvents: (events: SioEvent[]) => void;
  setAlerts: (alerts: Alert[]) => void;
  upsertAlert: (alert: Alert) => void;
  setDecisions: (decisions: Decision[]) => void;
  upsertDecision: (decision: Decision) => void;
  setForecasts: (forecasts: Forecast[]) => void;
  setMissions: (missions: Mission[]) => void;
  setZones: (zones: Zone[]) => void;
  selectEntity: (entityId: string | null) => void;
  setReplayAt: (ts: string | null) => void;
  reset: () => void;
}

export const useSioStore = create<SioState>((set) => ({
  connection: "connecting",
  entities: new Map(),
  events: [],
  alerts: [],
  decisions: [],
  forecasts: [],
  missions: [],
  zones: [],
  selectedEntityId: null,
  replayAt: null,
  lastMessageAt: null,

  setConnection: (status) => set({ connection: status }),

  upsertEntity: (entity) =>
    set((state) => {
      const entities = new Map(state.entities);
      entities.set(entity.entity_id, entity);
      return { entities, lastMessageAt: new Date().toISOString() };
    }),

  upsertEntities: (incoming) =>
    set((state) => {
      const entities = new Map(state.entities);
      for (const entity of incoming) entities.set(entity.entity_id, entity);
      return { entities };
    }),

  addEvent: (event) =>
    set((state) => ({
      // Newest first, bounded: an operator console left open for a shift must not grow without
      // limit. Full history stays queryable through /timeline.
      events: [event, ...state.events.filter((e) => e.event_id !== event.event_id)].slice(
        0,
        MAX_EVENTS,
      ),
      lastMessageAt: new Date().toISOString(),
    })),

  setEvents: (events) => set({ events: events.slice(0, MAX_EVENTS) }),
  setAlerts: (alerts) => set({ alerts }),

  upsertAlert: (alert) =>
    set((state) => {
      const others = state.alerts.filter((a) => a.alert_id !== alert.alert_id);
      return { alerts: [alert, ...others].sort((a, b) => b.score - a.score) };
    }),

  setDecisions: (decisions) => set({ decisions }),

  upsertDecision: (decision) =>
    set((state) => ({
      decisions: [
        decision,
        ...state.decisions.filter((d) => d.decision_id !== decision.decision_id),
      ],
    })),

  setForecasts: (forecasts) => set({ forecasts }),
  setMissions: (missions) => set({ missions }),
  setZones: (zones) => set({ zones }),
  selectEntity: (entityId) => set({ selectedEntityId: entityId }),
  setReplayAt: (ts) => set({ replayAt: ts }),

  reset: () =>
    set({
      entities: new Map(),
      events: [],
      alerts: [],
      decisions: [],
      forecasts: [],
      missions: [],
      selectedEntityId: null,
      replayAt: null,
    }),
}));

/**
 * Derivations — plain functions over data, **not** zustand selectors.
 *
 * This distinction is load-bearing. Zustand 5 compares snapshots by reference identity, so a
 * selector like `state => [...state.entities.values()].filter(...)` returns a fresh array on every
 * read, React sees a changed snapshot every time it checks, and the result is
 * "Maximum update depth exceeded" — an infinite render loop that unmounts the whole tree and leaves
 * a blank page. (That is exactly what happened here, and with no error boundary the entire console
 * rendered as one flat dark rectangle.)
 *
 * The rule that avoids it: subscribe to *stored* values only (`state.entities`, `state.alerts` —
 * whose references change only when the data does), then derive inside a `useMemo`.
 */
export const positionedEntities = (entities: Map<string, Entity>): Entity[] =>
  [...entities.values()].filter((entity) => entity.state.geo != null);

export const openAlerts = (alerts: Alert[]): Alert[] =>
  alerts.filter((alert) => alert.state === "open" || alert.state === "escalated");

export const pendingDecisions = (decisions: Decision[]): Decision[] =>
  decisions.filter((decision) => decision.approval === "pending");
