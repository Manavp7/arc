/**
 * Application shell.
 *
 * Layout mirrors how an operator actually works (PRD §12): the live picture dominates, the
 * timeline runs along the bottom because every question is "what happened when", and the side
 * rail holds the panels you dip into — copilot, alerts, decisions, missions.
 *
 * Phase 1 wires the map, the event feed and the connection state. Later phases fill the rail.
 */

import { useEffect, useState } from "react";
import { LiveMap } from "./components/LiveMap";
import { api } from "./lib/api";
import { connectStream } from "./lib/stream";
import { selectOpenAlerts, useSioStore } from "./store";
import type { Entity, SioEvent } from "./types";

type RailTab = "events" | "copilot" | "alerts" | "decisions" | "missions";

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

function EventFeed() {
  const events = useSioStore((state) => state.events);
  const selectEntity = useSioStore((state) => state.selectEntity);

  if (events.length === 0) {
    return (
      <p className="empty">
        No events yet. Start the simulator with <code>just seed</code>.
      </p>
    );
  }
  return (
    <ul className="feed">
      {events.map((event: SioEvent) => (
        <li key={event.event_id} className={`feed-item sev-${event.severity}`}>
          <div className="feed-head">
            <span className="feed-type">{event.type.replace(/_/g, " ")}</span>
            <time>{new Date(event.ts).toLocaleTimeString()}</time>
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
          </div>
        </li>
      ))}
    </ul>
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

  const dwellMinutes = Math.round(
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
        <dt>dwell</dt>
        <dd>{dwellMinutes} min</dd>
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
            {provenance.source_id}
            <em>{Math.round(provenance.confidence * 100)}%</em>
          </li>
        ))}
        {entity.provenance.length === 0 && <li className="empty">no provenance recorded</li>}
      </ul>
    </aside>
  );
}

function Placeholder({ label, phase }: { label: string; phase: string }) {
  return (
    <p className="empty">
      {label} arrives in {phase}.
    </p>
  );
}

export default function App() {
  const [tab, setTab] = useState<RailTab>("events");
  const setConnection = useSioStore((state) => state.setConnection);
  const upsertEntity = useSioStore((state) => state.upsertEntity);
  const upsertEntities = useSioStore((state) => state.upsertEntities);
  const addEvent = useSioStore((state) => state.addEvent);
  const setEvents = useSioStore((state) => state.setEvents);
  const setZones = useSioStore((state) => state.setZones);
  const openAlerts = useSioStore(selectOpenAlerts);
  const entityCount = useSioStore((state) => state.entities.size);

  // Initial snapshot, then live updates. Loading the snapshot first means the map is populated
  // immediately rather than filling in as messages happen to arrive.
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const [entities, events, zones] = await Promise.all([
          api.entities({ limit: 500 }),
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
          default:
            break;
        }
      },
    });
    return close;
  }, [setConnection, upsertEntity, addEvent]);

  return (
    <div className="app">
      <header className="topbar">
        <h1>
          SIO <span>Spatial Intelligence OS</span>
        </h1>
        <div className="topbar-right">
          <span className="stat">{entityCount} entities</span>
          <span className="stat">{openAlerts.length} open alerts</span>
          <ConnectionBadge />
        </div>
      </header>

      <main className="workspace">
        <section className="map-pane">
          <LiveMap />
          <EntityDetail />
        </section>

        <aside className="rail">
          <nav className="tabs">
            {(["events", "copilot", "alerts", "decisions", "missions"] as RailTab[]).map((name) => (
              <button
                key={name}
                className={name === tab ? "tab tab-active" : "tab"}
                onClick={() => setTab(name)}
              >
                {name}
              </button>
            ))}
          </nav>
          <div className="rail-body">
            {tab === "events" && <EventFeed />}
            {tab === "copilot" && <Placeholder label="The copilot" phase="Phase 4" />}
            {tab === "alerts" && <Placeholder label="The alerts inbox" phase="Phase 4" />}
            {tab === "decisions" && <Placeholder label="Recommendations" phase="Phase 4" />}
            {tab === "missions" && <Placeholder label="Mission control" phase="Phase 6" />}
          </div>
        </aside>
      </main>

      <footer className="timeline-strip">
        <span className="timeline-label">timeline</span>
        <p className="empty">The scrubber arrives in Phase 3.</p>
      </footer>
    </div>
  );
}
