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

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { AuthGate, SessionControls } from "./components/AuthGate";
import { IncidentPanel } from "./components/IncidentPanel";
import { SourcesPanel } from "./components/SourcesPanel";
import { SitePanel } from "./components/SitePanel";
import { VideoReviewPanel } from "./components/VideoReviewPanel";
import { CasePanel } from "./components/CasePanel";
import { SearchPanel } from "./components/SearchPanel";
import { ObjectExplorerPanel } from "./components/ObjectExplorerPanel";
import { ReviewMetricsPanel } from "./components/ReviewMetricsPanel";
import { EvaluationPanel } from "./components/EvaluationPanel";
import { ProcessingQueuePanel } from "./components/ProcessingQueuePanel";
import { OperatorInboxPanel } from "./components/OperatorInboxPanel";
import { StoragePanel } from "./components/StoragePanel";
import { CameraSetupPanel } from "./components/CameraSetupPanel";
import { EvidencePackagePanel } from "./components/EvidencePackagePanel";
import { EvidenceComparePanel } from "./components/EvidenceComparePanel";
import { NotificationCenter } from "./components/NotificationCenter";
import type { NotificationTarget } from "./lib/notifications";
import { OperationsProvider, OperatingModes, SystemPanel } from "./components/SystemPanel";
import * as session from "./lib/session";
import { AlertsPanel } from "./components/AlertsPanel";
import { AnalyticsPanel } from "./components/AnalyticsPanel";
import { CopilotPanel } from "./components/CopilotPanel";
import { DecisionsPanel } from "./components/DecisionsPanel";
import { ErrorBoundary } from "./components/ErrorBoundary";
import {
  ExplanationDrawer,
  fromEvent,
  type Explainable,
} from "./components/ExplanationDrawer";
import { ForecastPanel } from "./components/ForecastPanel";
import { LiveMap } from "./components/LiveMap";
import { MissionControlPanel } from "./components/MissionControlPanel";
import { PlaybookRunsPanel } from "./components/PlaybookRunsPanel";
import { TwinPanel } from "./components/TwinPanel";
import { WorkflowBuilderPanel } from "./components/WorkflowBuilderPanel";
import { Timeline } from "./components/Timeline";
import { api } from "./lib/api";
import { mergeAlertSnapshot, settleConsoleSnapshot } from "./lib/snapshot";
import { connectStream } from "./lib/stream";
import { latestTimestamp } from "./lib/freshness";
import { openAlerts, useSioStore } from "./store";
import type { Alert, Entity, SioEvent } from "./types";

type RailTab =
  | "evaluation" | "queue" | "inbox" | "storage" | "camera" | "evidence" | "compare"
  | "incident"
  | "footage"
  | "cases"
  | "search"
  | "objects"
  | "quality"
  | "site"
  | "sources"
  | "system"
  | "events"
  | "alerts"
  | "decisions"
  | "copilot"
  | "missions"
  | "playbooks"
  | "twin"
  | "forecast"
  | "analytics"
  | "builder";

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
  const lastStreamActivityAt = useSioStore((state) => state.lastStreamActivityAt);
  const label = {
    live: "live",
    connecting: "connecting",
    reconnecting: "reconnecting",
    closed: "offline",
  }[connection];
  return (
    <span
      className={`badge badge-${connection}`}
      title={lastStreamActivityAt ? `Last stream activity ${lastStreamActivityAt}` : "No stream activity yet"}
    >
      <i className="dot" />
      {label}
    </span>
  );
}

function EventFeed({
  onExplain,
}: {
  onExplain: (subject: Explainable) => void;
}) {
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
          showing the {shown.length} most recent of {events.length} — use the
          timeline for the rest
        </p>
      )}
      <ul className="feed">
        {shown.map((event: SioEvent) => (
          <li
            key={event.event_id}
            className={`feed-item sev-${event.severity}`}
          >
            <div className="feed-head">
              <span className="feed-type">{event.type.replace(/_/g, " ")}</span>
              {/* 24-hour, matching the drawer and the timeline. The feed showed "8:01:42 AM" beside a
                drawer showing "08:01:42" for the same event, which invites the reader to wonder whether
                they are looking at the same thing. Operations software has no business being ambiguous
                about time. */}
              <time>
                {new Date(event.ts).toLocaleTimeString([], { hour12: false })}
              </time>
            </div>
            {event.explanation.summary && (
              <p className="feed-summary">{event.explanation.summary}</p>
            )}
            <div className="feed-meta">
              <span title="confidence">
                {Math.round(event.confidence * 100)}%
              </span>
              {event.entities.slice(0, 2).map((entityId) => (
                <button
                  key={entityId}
                  className="chip"
                  onClick={() => selectEntity(entityId)}
                >
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
              <button
                className="link-btn"
                onClick={() => onExplain(fromEvent(event))}
              >
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
  const entity = useSioStore((state) =>
    selectedId ? (state.replayAt ? state.historyEntities : state.entities).get(selectedId) : undefined,
  );
  const selectEntity = useSioStore((state) => state.selectEntity);

  if (!selectedId) return null;
  if (!entity) {
    return (
      <aside className="detail">
        <header>
          <strong>{selectedId}</strong>
          <button onClick={() => selectEntity(null)}>×</button>
        </header>
        <p className="empty">Not observed in the selected view.</p>
      </aside>
    );
  }

  // Time on site: first observation to most recent. This is the figure UC1 ("entered today and
  // stayed more than 15 minutes") turns on. Dock-specific dwell is a different measure and belongs
  // to the zone-dwell rule in Phase 3, so this row is labelled honestly rather than "dwell".
  const onSiteMinutes = Math.round(
    (new Date(entity.last_seen).getTime() -
      new Date(entity.first_seen).getTime()) /
      60000,
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
              {entity.state.geo.lat.toFixed(5)},{" "}
              {entity.state.geo.lon.toFixed(5)}
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
        {entity.provenance.length === 0 && (
          <li className="empty">no provenance recorded</li>
        )}
      </ul>
    </aside>
  );
}

type Workspace = "monitor" | "review" | "investigate" | "respond" | "admin";
const SECTIONS: Record<Workspace, { label: string; tabs: RailTab[] }> = {
  monitor: { label: "Monitor", tabs: ["alerts", "events", "analytics"] },
  review: { label: "Footage & cases", tabs: ["footage", "objects", "evaluation", "queue", "inbox", "cases", "compare", "evidence", "search", "quality"] },
  investigate: { label: "Investigate", tabs: ["incident", "copilot", "forecast", "twin"] },
  respond: { label: "Respond", tabs: ["decisions", "missions", "playbooks"] },
  admin: { label: "Administration", tabs: ["sources", "site", "camera", "storage", "system", "builder"] },
};
const TAB_NAMES: Record<RailTab, string> = { objects: "Objects", compare: "Compare footage", evaluation: "Evaluation lab", queue: "Processing queue", inbox: "Operator inbox", storage: "Storage", camera: "Camera setup", evidence: "Evidence packages", footage: "Footage", cases: "Cases", search: "Search", quality: "Review quality", site: "Site editor", alerts: "Alerts", events: "Event feed", analytics: "Analytics", incident: "Incident", copilot: "Copilot", forecast: "Forecasts", twin: "3D twin", decisions: "Decisions", missions: "Missions", playbooks: "Response log", sources: "Sources", system: "System health", builder: "Workflow builder" };
const TAB_PERMISSIONS: Partial<Record<RailTab, string>> = { objects: "review.read", compare: "case.read", storage: "storage.read", camera: "site.write", inbox: "case.read", evidence: "case.read", sources: "integration.read", missions: "mission.read", copilot: "copilot.ask", builder: "workflow.write" };
function canOpenTab(name: RailTab): boolean {
  const permission = TAB_PERMISSIONS[name];
  return !permission || session.can(permission);
}

function Console() {
  // Role changes on token renewal refresh existing controls without resetting the workspace.
  const identity = session.useSession();
  const [workspace, setWorkspace] = useState<Workspace>("monitor");
  const [tab, setTab] = useState<RailTab>("alerts");
  const [incident, setIncident] = useState<Alert | null>(null);
  const [missionId, setMissionId] = useState<string | undefined>();
  const [caseId, setCaseId] = useState<string | undefined>();
  const [comparisonDirty, setComparisonDirty] = useState(false);
  const [footageDirty, setFootageDirty] = useState(false);
  const [navigationNotice, setNavigationNotice] = useState<string | null>(null);
  const [packageId, setPackageId] = useState<string | undefined>();
  const [videoId, setVideoId] = useState<string | undefined>();
  const [videoAt, setVideoAt] = useState<number | undefined>();
  const [analysisId, setAnalysisId] = useState<string | undefined>();
  const [siteId, setSiteId] = useState<string | undefined>();
  const [explaining, setExplaining] = useState<Explainable | null>(null);
  const [snapshotError, setSnapshotError] = useState<string | null>(null);
  const [snapshotAt, setSnapshotAt] = useState<string | null>(null);
  const [now, setNow] = useState(Date.now());
  const generation = useRef(0);
  const onExplain = useCallback((subject: Explainable) => setExplaining(subject), []);
  const closeDrawer = useCallback(() => setExplaining(null), []);
  const alerts = useSioStore(state => state.alerts);
  const entities = useSioStore(state => state.entities);
  const historyEntities = useSioStore(state => state.historyEntities);
  const replayAt = useSioStore(state => state.replayAt);
  const lastMessageAt = useSioStore(state => state.lastMessageAt);
  const unresolvedAlerts = useMemo(() => openAlerts(alerts), [alerts]);
  const entityCount = [...(replayAt ? historyEntities : entities).values()].filter(entity => !entity.is_static).length;
  const activeIncident = incident ? alerts.find(row => row.alert_id === incident.alert_id) ?? incident : null;
  const refreshedAt = latestTimestamp(lastMessageAt, snapshotAt);
  const age = refreshedAt ? Math.max(0, Math.floor((now - new Date(refreshedAt).getTime()) / 1000)) : null;
  const loadSnapshot = useCallback(async () => {
    const run = ++generation.current;
    const alertsAtStart = new Map(useSioStore.getState().alerts.map(alert => [alert.alert_id, alert]));
    const snapshot = await settleConsoleSnapshot({
      entities: api.entities({ limit: 500, active_within_s: LIVE_WINDOW_S }),
      events: api.events({ limit: 100 }), zones: api.zones(), alerts: api.alertInbox({ limit: 200 }),
    });
    if (run !== generation.current) return;
    const store = useSioStore.getState();
    if (snapshot.entities) store.replaceEntities(snapshot.entities);
    if (snapshot.events) store.setEvents([...new Map([...snapshot.events, ...store.events].map(event => [event.event_id, event])).values()].sort((a, b) => b.ts.localeCompare(a.ts)).slice(0, 500));
    if (snapshot.zones) store.setZones(snapshot.zones);
    if (snapshot.alerts) store.setAlerts(mergeAlertSnapshot(snapshot.alerts, useSioStore.getState().alerts, alertsAtStart));
    setSnapshotError(snapshot.failures.length ? `Some sections could not refresh. ${snapshot.failures.join("; ")}` : null);
    // The freshness indicator describes the site picture, so an alerts-only refresh does not advance it.
    if (snapshot.entities) setSnapshotAt(new Date().toISOString());
  }, []);
  useEffect(() => {
    void loadSnapshot();
    const retry = setInterval(() => void loadSnapshot(), 30000);
    return () => { generation.current++; clearInterval(retry); };
  }, [loadSnapshot]);
  useEffect(() => {
    const timer = setInterval(() => { setNow(Date.now()); useSioStore.getState().pruneStaleEntities(LIVE_WINDOW_S); }, 5000);
    return () => clearInterval(timer);
  }, []);
  useEffect(() => connectStream({
    onStatus: state => useSioStore.getState().setConnection(state),
    onActivity: receivedAt => useSioStore.getState().markStreamActivity(receivedAt),
    onConnected: () => void loadSnapshot(),
    onMessage: message => {
      const store = useSioStore.getState();
      if (message.kind === "Entity") store.upsertEntity(message.payload as Entity);
      else if (message.kind === "Event") store.addEvent(message.payload as SioEvent);
      else if (message.kind === "Alert") store.upsertAlert(message.payload as Alert);
    },
  }), [loadSnapshot]);
  useEffect(() => {
    if (!canOpenTab(tab)) setTab(SECTIONS[workspace].tabs.find(canOpenTab) ?? "alerts");
  }, [identity, tab, workspace]);
  useEffect(() => { if (!comparisonDirty && !footageDirty) setNavigationNotice(null); }, [comparisonDirty,footageDirty]);
  function navigate(section: Workspace, target?: RailTab): boolean {
    const next = target && canOpenTab(target) ? target : SECTIONS[section].tabs.find(canOpenTab) ?? "alerts";
    if (tab === "compare" && comparisonDirty && next !== "compare") {
      setNavigationNotice("Save or discard your alignment changes before leaving Compare footage.");
      return false;
    }
    if (tab === "footage" && footageDirty && next !== "footage") {
      setNavigationNotice("Save a movement snapshot or discard its draft, save or discard footage edits and bookmarks, and queue or reset changed analysis settings before leaving Footage.");
      return false;
    }
    setNavigationNotice(null);
    setWorkspace(section);
    setTab(next);
    return true;
  }
  function investigate(alert: Alert) { setIncident(alert); navigate("investigate", "incident"); if (alert.entity_ids[0]) useSioStore.getState().selectEntity(alert.entity_ids[0]); }
  function openMission(id?: string) { if (navigate("respond", "missions")) setMissionId(id); }
  function openCase(id: string) { if (navigate("review", "cases")) setCaseId(id); }
  function openComparison(id: string) { if (navigate("review", "compare")) setCaseId(id); }
  function openPackage(id: string, selectedPackageId?: string) { if (navigate("review", "evidence")) { setCaseId(id); setPackageId(selectedPackageId); } }
  function openVideo(id: string, atS?: number, version?: string) { if (tab === "footage" && footageDirty) { setNavigationNotice("Save a movement snapshot or discard its draft, save or discard footage edits and bookmarks, and queue or reset changed analysis settings before opening another recording or analysis."); return; } if (navigate("review", "footage")) { setVideoId(id); setVideoAt(atS); setAnalysisId(version); } }
  function openNotification(target: NotificationTarget) {
    if (target.kind === "case") openCase(target.case_id ?? target.id);
    else if (target.kind === "analysis" && target.video_id) openVideo(target.video_id, undefined, target.analysis_id ?? target.id);
    else if (target.kind === "package" && target.case_id) openPackage(target.case_id, target.package_id ?? target.id);
  }
  function searchResult(kind: string, id: string, atS?: number, version?: string) {
    if (kind === "case") openCase(id);
    else if (kind === "video" || kind === "video_event") openVideo(id, atS, version);
    else if (kind === "alert") { const found = alerts.find(row => row.alert_id === id); if (found) investigate(found); else void api.request<Alert>(`/alerts/${encodeURIComponent(id)}`).then(investigate).catch(() => setSnapshotError("This alert could not be opened.")); }
    else if (kind === "mission") openMission(id);
    else if (kind === "site") { setSiteId(id); navigate("admin", "site"); }
    else if (kind === "entity") { useSioStore.getState().selectEntity(id); navigate("monitor", "events"); }
    else if (kind === "event") { void api.request<SioEvent>(`/events/${encodeURIComponent(id)}`).then(event => onExplain(fromEvent(event))).catch(() => setSnapshotError("This event could not be opened.")); }
  }
  return <div className={`app ${workspace === "review" || workspace === "admin" ? "app-review" : ""}`}>
    <header className="topbar"><div className="brand"><span className="brand-mark">S</span><h1>SIO <span>Site operations</span></h1></div><div className="topbar-right"><NotificationCenter onOpenTarget={openNotification} /><ConnectionBadge /><SessionControls /></div></header>
    <div className="workspace-bar"><nav className="workspace-nav" aria-label="Workspaces">{(Object.keys(SECTIONS) as Workspace[]).map(section => <button key={section} aria-current={workspace === section ? "page" : undefined} className={workspace === section ? "workspace-tab active" : "workspace-tab"} onClick={() => navigate(section)}>{SECTIONS[section].label}{section === "monitor" && unresolvedAlerts.length > 0 && <span className="tab-badge">{unresolvedAlerts.length}</span>}</button>)}</nav><OperatingModes /></div>
    <main className={`workspace workspace-${workspace}`}>
      {workspace !== "admin" && workspace !== "review" && <section className="map-pane" aria-label="Site map"><ErrorBoundary label="Site map"><LiveMap /></ErrorBoundary><div className="map-status"><strong>{replayAt ? "Historical picture" : "Live picture"}</strong><span>{entityCount} moving entities</span><span>{replayAt ? new Date(replayAt).toLocaleString() : age == null ? "Waiting for observations" : `Updated ${age}s ago`}</span></div><ErrorBoundary label="Entity detail"><EntityDetail /></ErrorBoundary></section>}
      <aside className="rail"><nav className="tabs" aria-label={`${SECTIONS[workspace].label} tools`}>{SECTIONS[workspace].tabs.filter(canOpenTab).map(name => <button key={name} aria-pressed={name === tab} className={name === tab ? "tab tab-active" : "tab"} onClick={() => navigate(workspace, name)}>{TAB_NAMES[name]}</button>)}</nav>
        <div className="rail-body">{navigationNotice && <p className="snapshot-error" role="alert">{navigationNotice}</p>}{snapshotError && <div className="snapshot-error" role="status"><span>Live snapshot unavailable. {snapshotError}</span><button onClick={() => void loadSnapshot()}>Retry</button></div>}
          <ErrorBoundary key={tab} label={TAB_NAMES[tab]}>
            {tab === "alerts" && <AlertsPanel onExplain={onExplain} onInvestigate={investigate} />}
            {tab === "events" && <EventFeed onExplain={onExplain} />}
            {tab === "incident" && <IncidentPanel alert={activeIncident} onExplain={onExplain} onMission={openMission} onCase={openCase} onClose={() => navigate("monitor", "alerts")} />}
            {tab === "decisions" && <DecisionsPanel onExplain={onExplain} />}
            {tab === "copilot" && <CopilotPanel onExplain={onExplain} />}
            {tab === "missions" && <MissionControlPanel initialMissionId={missionId} />}
            {tab === "playbooks" && <PlaybookRunsPanel />}
            {tab === "twin" && <TwinPanel />}
            {tab === "forecast" && <ForecastPanel />}
            {tab === "analytics" && <AnalyticsPanel />}
            {tab === "builder" && session.can("workflow.write") && <WorkflowBuilderPanel />}
            {tab === "sources" && <SourcesPanel />}
            {tab === "system" && <SystemPanel />}
            {tab === "site" && <SitePanel initialSiteId={siteId} />}
            {tab === "footage" && <VideoReviewPanel initialVideoId={videoId} initialAtS={videoAt} initialAnalysisId={analysisId} onOpenCase={openCase} onDirtyChange={setFootageDirty} />}
            {tab === "cases" && <CasePanel initialCaseId={caseId} onOpenVideo={openVideo} onOpenMission={openMission} onPrepareEvidence={openPackage} onCompareEvidence={openComparison} />}
            {tab === "evaluation" && <EvaluationPanel onOpenVideo={openVideo} onOpenCase={openCase} />}
            {tab === "queue" && <ProcessingQueuePanel onOpenVideo={openVideo} />}
            {tab === "inbox" && <OperatorInboxPanel onOpenCase={openCase} />}
            {tab === "storage" && <StoragePanel />}
            {tab === "camera" && <CameraSetupPanel initialSiteId={siteId} onOpenSite={id => { setSiteId(id); navigate("admin", "site"); }} />}
            {tab === "compare" && <EvidenceComparePanel initialCaseId={caseId} onOpenCase={openCase} onOpenVideo={openVideo} onDirtyChange={setComparisonDirty} />}
            {tab === "evidence" && <EvidencePackagePanel initialCaseId={caseId} initialPackageId={packageId} onOpenCase={openCase} />}
            {tab === "search" && <SearchPanel onSelectResult={searchResult} />}
            {tab === "objects" && <ObjectExplorerPanel onOpenFootage={(id, atS, version) => searchResult("video", id, atS, version)} />}
            {tab === "quality" && <ReviewMetricsPanel />}
          </ErrorBoundary>
        </div>
      </aside>
    </main>
    <ErrorBoundary label="Explanation"><ExplanationDrawer subject={explaining} onClose={closeDrawer} /></ErrorBoundary>
    {workspace !== "review" && workspace !== "admin" && <footer className="timeline-strip"><span className="timeline-label">Timeline</span><ErrorBoundary label="Timeline"><Timeline /></ErrorBoundary></footer>}
  </div>;
}

export default function App() { return <AuthGate><OperationsProvider><Console /></OperationsProvider></AuthGate>; }
