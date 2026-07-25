/**
 * Application shell.
 *
 * Layout mirrors how an operator actually works (PRD §12): the live picture dominates, the
 * timeline runs along the bottom because every question is "what happened when", and the side
 * rail holds the panels you dip into — alerts, decisions, copilot, missions, forecasts.
 *
 * The rail is one column and not a dashboard of six tiles, deliberately. An operator is doing one
 * thing at a time — triaging the inbox, or approving a recommendation, or asking a question — and
 * six live tiles compete for the attention that the map needs. The map is the application; the rail
 * is where you go to act on it.
 *
 * Everything that can explain itself opens the same drawer (`ExplanationDrawer`). Every service in
 * the platform produces a full `Explanation` and one shared renderer means none of them is the poor
 * relation — an alert, an event and a recommendation are all inspected the same way.
 */

import { useCallback, useEffect, useMemo, useState } from "react";
import { AlertsPanel } from "./components/AlertsPanel";
import { AnalyticsPanel } from "./components/AnalyticsPanel";
import { CopilotPanel } from "./components/CopilotPanel";
import { DecisionsPanel } from "./components/DecisionsPanel";
import { ErrorBoundary } from "./components/ErrorBoundary";
import { ExplanationDrawer, fromEvent, type Explainable } from "./components/ExplanationDrawer";
import { ForecastPanel } from "./components/ForecastPanel";
import { LiveMap } from "./components/LiveMap";
import { MissionsPanel } from "./components/MissionsPanel";
import { Timeline } from "./components/Timeline";
import { api } from "./lib/api";
import { connectStream } from "./lib/stream";
import { openAlerts, useSioStore } from "./store";
import type { Alert, Entity, SioEvent } from "./types";

type RailTab =
  | "events"
  | "alerts"
  | "decisions"
  | "copilot"
  | "missions"
  | "forecast"
  | "analytics";

/**
 * How recently an entity must have been observed to appear in the live view.
 *
 * SIO deletes nothing (PRD M2), so `/entities` legitimately returns every entity that has ever
 * existed — including ones from an earlier run that will never move again. On a live map those are
 * ghosts. Full history remains reachable through the timeline and `/world/at`.
 */
const LIVE_WINDOW_S = 300;

/** Feed rows rendered at once. See the note in `EventFeed`. */
const FEED_ROWS = 80;

function ConnectionBadge() {
  const connection = useSioStore((state) => state.connection);
  const lastMessageAt = useSioStore((state) => state.lastMessageAt);
  const label = { live: "live", connecting: "connecting", reconnecting: "reconnecting", closed: "offline" }[
    connection
  ];
  return (
    <span className={`badge badge-${connection}`} title={lastMessageAt ?? "no messages yet"}>
      <i className="dot" />
      {label}
    </span>
  );
}

function EventFeed({ onExplain }: { onExplain: (subject: Explainable) => void }) {
  const liveEvents = useSioStore((state) => state.events);
  const historyEvents = useSioStore((state) => state.historyEvents);
  const replayAt = useSioStore((state) => state.replayAt);
  // The feed follows the scrubber. A map showing 03:44 beside a feed showing 03:56 describes two
  // different moments at once, which is worse than not replaying at all because it looks correct.
  const events = replayAt ? historyEvents : liveEvents;
  const selectEntity = useSioStore((state) => state.selectEntity);
  /**
   * How many feed rows to render.
   *
   * The store keeps 500 events and the feed rendered every one, each with a button — 500 rows and 500
   * event handlers in a 900 px scroller that nobody scrolls past the first screen of. Capped, with the
   * total stated, because silently truncating would be its own small lie: an operator who has seen
   * "showing 80 of 500" knows to reach for the timeline, and one shown 80 rows with no note does not.
   */
  const shown = events.slice(0, FEED_ROWS);

  if (events.length === 0) {
    return (
      <p className="empty">
        No events yet. Start the simulator with <code>just seed</code>.
      </p>
    );
  }
  return (
    <>
      {events.length > shown.length && (
        <p className="feed-note">
          showing the {shown.length} most recent of {events.length} — use the timeline for the rest
        </p>
      )}
      <ul className="feed">
        {shown.map((event: SioEvent) => (
        <li key={event.event_id} className={`feed-item sev-${event.severity}`}>
          <div className="feed-head">
            <span className="feed-type">{event.type.replace(/_/g, " ")}</span>
            {/* 24-hour, matching the drawer and the timeline. The feed showed "8:01:42 AM" beside a
                drawer showing "08:01:42" for the same event, which invites the reader to wonder whether
                they are looking at the same thing. Operations software has no business being ambiguous
                about time. */}
            <time>{new Date(event.ts).toLocaleTimeString([], { hour12: false })}</time>
          </div>
          {event.explanation.summary && <p className="feed-summary">{event.explanation.summary}</p>}
          <div className="feed-meta">
            <span title="confidence">{Math.round(event.confidence * 100)}%</span>
            {event.entities.slice(0, 2).map((entityId) => (
              <button key={entityId} className="chip" onClick={() => selectEntity(entityId)}>
                {entityId.slice(0, 12)}
              </button>
            ))}
            {event.source_ids.slice(0, 2).map((source) => (
              <span key={source} className="chip chip-quiet">
                {source}
              </span>
            ))}
            {/* Every event already carries a full explanation — which clause matched, with what value,
                against which evidence. Until the drawer existed it was reachable only by curl. */}
            <button className="link-btn" onClick={() => onExplain(fromEvent(event))}>
              why?
            </button>
          </div>
        </li>
        ))}
      </ul>
    </>
  );
}

function EntityDetail() {
  const selectedId = useSioStore((state) => state.selectedEntityId);
  const entity = useSioStore((state) => (selectedId ? state.entities.get(selectedId) : undefined));
  const selectEntity = useSioStore((state) => state.selectEntity);

  if (!selectedId) return null;
  if (!entity) {
    return (
      <aside className="detail">
        <header>
          <strong>{selectedId}</strong>
          <button onClick={() => selectEntity(null)}>×</button>
        </header>
        <p className="empty">Not in the live view.</p>
      </aside>
    );
  }

  // Time on site: first observation to most recent. This is the figure UC1 ("entered today and
  // stayed more than 15 minutes") turns on. Dock-specific dwell is a different measure and belongs
  // to the zone-dwell rule in Phase 3, so this row is labelled honestly rather than "dwell".
  const onSiteMinutes = Math.round(
    (new Date(entity.last_seen).getTime() - new Date(entity.first_seen).getTime()) / 60000,
  );

  return (
    <aside className="detail">
      <header>
        <strong>{entity.label ?? entity.entity_id}</strong>
        <button onClick={() => selectEntity(null)} aria-label="close">
          ×
        </button>
      </header>
      <dl>
        <dt>type</dt>
        <dd>{entity.type}</dd>
        <dt>confidence</dt>
        <dd>{Math.round(entity.confidence * 100)}%</dd>
        <dt>on site</dt>
        <dd>{onSiteMinutes} min</dd>
        {entity.state.zone_id && (
          <>
            <dt>zone</dt>
            <dd>{entity.state.zone_id}</dd>
          </>
        )}
        {entity.state.geo && (
          <>
            <dt>position</dt>
            <dd>
              {entity.state.geo.lat.toFixed(5)}, {entity.state.geo.lon.toFixed(5)}
            </dd>
          </>
        )}
      </dl>
      {/* Provenance is the point: an operator can see which sensors produced this belief. */}
      <h4>sources</h4>
      <ul className="sources">
        {entity.provenance.slice(-6).map((provenance, index) => (
          <li key={`${provenance.source_id}-${index}`}>
            <span className="chip chip-quiet">{provenance.modality}</span>
            <span className="source-id" title={provenance.source_id}>
              {provenance.source_id}
            </span>
            <em>{Math.round(provenance.confidence * 100)}%</em>
          </li>
        ))}
        {entity.provenance.length === 0 && <li className="empty">no provenance recorded</li>}
      </ul>
    </aside>
  );
}

export default function App() {
  const [tab, setTab] = useState<RailTab>("events");
  const [explaining, setExplaining] = useState<Explainable | null>(null);
  const onExplain = useCallback((subject: Explainable) => setExplaining(subject), []);
  const closeDrawer = useCallback(() => setExplaining(null), []);
  const setConnection = useSioStore((state) => state.setConnection);
  const upsertEntity = useSioStore((state) => state.upsertEntity);
  const upsertEntities = useSioStore((state) => state.upsertEntities);
  const addEvent = useSioStore((state) => state.addEvent);
  const upsertAlert = useSioStore((state) => state.upsertAlert);
  const setEvents = useSioStore((state) => state.setEvents);
  const setZones = useSioStore((state) => state.setZones);
  const alerts = useSioStore((state) => state.alerts);
  const unresolvedAlerts = useMemo(() => openAlerts(alerts), [alerts]);
  // Movers only, matching what the copilot counts.
  //
  // These disagreed: the header said "58 entities" (everything, including cameras, gates and docks) while
  // the copilot answered "28 entities on site" for the same moment, because it excludes fixed
  // infrastructure. Two parts of one product giving different answers to "how many things are here" is
  // corrosive out of all proportion to the size of the bug — the user cannot tell which to believe, so
  // they believe neither.
  //
  // The copilot's definition is the right one: a camera is not ON the site, it IS the site.
  const liveCount = useSioStore(
    (state) => [...state.entities.values()].filter((entity) => !entity.is_static).length,
  );
  // Same definition during replay, so the number does not change meaning when scrubbing.
  const historyCount = useSioStore(
    (state) => [...state.historyEntities.values()].filter((entity) => !entity.is_static).length,
  );
  const scrubbing = useSioStore((state) => state.replayAt !== null);
  const entityCount = scrubbing ? historyCount : liveCount;

  // Initial snapshot, then live updates. Loading the snapshot first means the map is populated
  // immediately rather than filling in as messages happen to arrive.
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const [entities, events, zones] = await Promise.all([
          api.entities({ limit: 500, active_within_s: LIVE_WINDOW_S }),
          api.events({ limit: 50 }),
          api.zones().catch(() => []),
        ]);
        if (cancelled) return;
        upsertEntities(entities);
        setEvents(events);
        setZones(zones);
      } catch (error) {
        console.warn("initial load failed (is the API running?)", error);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [upsertEntities, setEvents, setZones]);

  useEffect(() => {
    const close = connectStream({
      onStatus: setConnection,
      onMessage: (message) => {
        switch (message.kind) {
          case "Entity":
            upsertEntity(message.payload as Entity);
            break;
          case "Event":
            addEvent(message.payload as SioEvent);
            break;
          case "Alert":
            // The header count is driven by the stream so it moves the moment an alert is raised, rather
            // than on the inbox panel's poll — which the operator may not have open.
            upsertAlert(message.payload as Alert);
            break;
          default:
            break;
        }
      },
    });
    return close;
  }, [setConnection, upsertEntity, addEvent, upsertAlert]);

  return (
    <div className="app">
      <header className="topbar">
        <h1>
          SIO <span>Spatial Intelligence OS</span>
        </h1>
        <div className="topbar-right">
          <span className="stat" title="Moving entities seen in the last 5 minutes — fixed infrastructure is not counted">
            {entityCount} entities
          </span>
          <span className="stat">{unresolvedAlerts.length} open alerts</span>
          <ConnectionBadge />
        </div>
      </header>

      <main className="workspace">
        <section className="map-pane">
          {/* Each panel gets its own boundary: a broken map must still leave the event feed,
              alerts and copilot usable rather than blanking the console. */}
          <ErrorBoundary label="Live map">
            <LiveMap />
          </ErrorBoundary>
          <ErrorBoundary label="Entity detail">
            <EntityDetail />
          </ErrorBoundary>
        </section>

        <aside className="rail">
          <nav className="tabs">
            {(
              [
                "events",
                "alerts",
                "decisions",
                "copilot",
                "missions",
                "forecast",
                "analytics",
              ] as RailTab[]
            ).map(
              (name) => (
                <button
                  key={name}
                  className={name === tab ? "tab tab-active" : "tab"}
                  onClick={() => setTab(name)}
                >
                  {name}
                  {/* The unattended count rides on the tab, because the operator will be looking at the
                      map when it changes. */}
                  {name === "alerts" && unresolvedAlerts.length > 0 && (
                    <span className="tab-badge">{unresolvedAlerts.length}</span>
                  )}
                </button>
              ),
            )}
          </nav>
          <div className="rail-body">
            {/* Keyed by tab so a panel that throws is contained to that tab and remounts cleanly when
                the operator switches away and back, rather than poisoning the rail. */}
            <ErrorBoundary key={tab} label={tab}>
              {tab === "events" && <EventFeed onExplain={onExplain} />}
              {tab === "alerts" && <AlertsPanel onExplain={onExplain} />}
              {tab === "decisions" && <DecisionsPanel onExplain={onExplain} />}
              {tab === "copilot" && <CopilotPanel onExplain={onExplain} />}
              {tab === "missions" && <MissionsPanel />}
              {tab === "forecast" && <ForecastPanel />}
              {tab === "analytics" && <AnalyticsPanel />}
            </ErrorBoundary>
          </div>
        </aside>
      </main>

      {/* One drawer for events, alerts and recommendations. Outside the rail so it can be wide enough
          for an evidence list without squeezing the map. */}
      <ErrorBoundary label="the explanation">
        <ExplanationDrawer subject={explaining} onClose={closeDrawer} />
      </ErrorBoundary>

      <footer className="timeline-strip">
        <span className="timeline-label">timeline</span>
        <ErrorBoundary label="the timeline">
          <Timeline />
        </ErrorBoundary>
      </footer>
    </div>
  );
}
