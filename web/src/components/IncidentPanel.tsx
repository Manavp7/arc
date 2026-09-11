import { useEffect, useState } from "react";
import { api, explainError } from "../lib/api";
import { reviewApi } from "../lib/review-api";
import * as session from "../lib/session";
import { useSioStore } from "../store";
import type { Alert, Decision, SioEvent } from "../types";
import { fromAlert, fromDecision, type Explainable } from "./ExplanationDrawer";
import { OperatingModes } from "./SystemPanel";

interface RelatedMission { mission_id: string; name: string; zone_id?: string | null; state: string }

function EvidenceImage({ reference }: { reference: string }) {
  const [url, setUrl] = useState<string | null>(null);
  const [error, setError] = useState(false);
  useEffect(() => {
    const controller = new AbortController();
    let objectUrl: string | null = null;
    setUrl(null); setError(false);
    void (async () => {
      try {
        await session.ensure();
        const response = await fetch(`/media/${reference.split("/").map(encodeURIComponent).join("/")}`, { headers: session.headers(), signal: controller.signal });
        if (!response.ok) throw new Error("Evidence is unavailable");
        const blob = await response.blob();
        if (!blob.type.startsWith("image/")) throw new Error("Evidence is not an image");
        if (controller.signal.aborted) return;
        objectUrl = URL.createObjectURL(blob); setUrl(objectUrl);
      } catch { if (!controller.signal.aborted) setError(true); }
    })();
    return () => { controller.abort(); if (objectUrl) URL.revokeObjectURL(objectUrl); };
  }, [reference]);
  return <figure className="incident-evidence">{url ? <img src={url} alt="Recorded camera evidence for this incident" /> : <p>{error ? "Recorded image is unavailable or access is restricted." : "Loading recorded image…"}</p>}<figcaption>{reference}</figcaption></figure>;
}

export function IncidentPanel({ alert, onExplain, onMission, onCase, onClose }: {
  alert: Alert | null; onExplain: (subject: Explainable) => void;
  onMission: (id?: string) => void; onCase?: (id: string) => void; onClose: () => void;
}) {
  const entities = useSioStore(state => state.entities);
  const selectEntity = useSioStore(state => state.selectEntity);
  const requestReplay = useSioStore(state => state.requestReplay);
  const upsertAlert = useSioStore(state => state.upsertAlert);
  const [events, setEvents] = useState<SioEvent[]>([]);
  const [decisions, setDecisions] = useState<Decision[]>([]);
  const [missions, setMissions] = useState<RelatedMission[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [revision, setRevision] = useState(0);
  const linkedIds = [...(alert?.decision_ids ?? []), ...(alert?.event_ids ?? [])].join("|");
  useEffect(() => { setEvents([]); setDecisions([]); setMissions([]); setError(null); }, [alert?.alert_id]);
  useEffect(() => { if (!alert) return; const timer = setInterval(() => setRevision(value => value + 1), 15000); return () => clearInterval(timer); }, [alert?.alert_id]);
  useEffect(() => {
    let cancelled = false;
    if (!alert) return;
    void (async () => {
      const from = new Date(new Date(alert.ts).getTime() - 60000).toISOString();
      const to = new Date(new Date(alert.last_ts).getTime() + 60000).toISOString();
      const responses = await Promise.allSettled([
        api.timeline({ from, to, limit: 1000 }), api.decisions({ limit: 100 }), api.missions(),
        Promise.all((alert.decision_ids ?? []).map(id => api.request<Decision>(`/decisions/${encodeURIComponent(id)}`))),
      ]);
      if (cancelled) return;
      const [eventRows, decisionRows, missionRows, linkedRows] = responses;
      if (eventRows.status === "fulfilled") setEvents(eventRows.value.filter(row => alert.event_ids.includes(row.event_id)));
      const related = decisionRows.status === "fulfilled" ? decisionRows.value.decisions.filter(row => (row.trigger_event && alert.event_ids.includes(row.trigger_event)) || alert.decision_ids?.includes(row.decision_id)) : [];
      if (linkedRows.status === "fulfilled") related.push(...linkedRows.value);
      if (decisionRows.status === "fulfilled" || linkedRows.status === "fulfilled") setDecisions([...new Map(related.map(row => [row.decision_id, row])).values()]);
      if (missionRows.status === "fulfilled") setMissions((missionRows.value.missions as RelatedMission[]).filter(row => !!alert.zone_id && row.zone_id === alert.zone_id));
      const failed = responses.filter(row => row.status === "rejected");
      setError(failed.length ? "Some incident records could not be loaded. Missing sections are unavailable, rather than confirmed empty." : null);
    })();
    return () => { cancelled = true; };
  }, [alert?.alert_id, alert?.last_ts, linkedIds, revision]);

  if (!alert) return <div className="panel-body incident-empty"><span className="eyebrow">Investigate / incident</span><h2>Follow the evidence.</h2><p>Select an alert in Monitor to bring its location, observations, decisions, and response records into one workspace.</p></div>;
  const evidence = [...new Set([...alert.explanation.evidence, ...events.flatMap(event => event.evidence)].filter(item => /\.(?:jpe?g|png|webp)$/i.test(item.ref) && !item.ref.includes("://") && !item.ref.startsWith("pending/")).map(item => item.ref))].slice(0, 3);
  async function perform(action: () => Promise<unknown>) {
    setBusy(true); setError(null);
    try { await action(); setRevision(value => value + 1); }
    catch (cause) { setError(explainError(cause)); }
    finally { setBusy(false); }
  }
  async function createMission() {
    if (!alert) return;
    const mission = await api.createMission({ name: `Response: ${alert.title}`, zone_id: alert.zone_id, objectives: [{ description: `Investigate alert ${alert.alert_id}`, zone_id: alert.zone_id }] }) as RelatedMission;
    // The draft is visible before any assignment or activation is requested.
    onMission(mission.mission_id);
  }
  return <div className="panel-body incident-panel">
    <header className="section-heading"><div><span className="eyebrow">Investigate / incident</span><h2>{alert.title}</h2></div><button className="small-button" onClick={onClose}>Alerts</button></header>
    <div className="incident-state"><span className={`sev-tag sev-${alert.severity}`}>{alert.severity}</span><span>{alert.state}</span><span>{alert.zone_id?.replace(/_/g, " ") || "Location not recorded"}</span></div>
    <OperatingModes />
    <p className="section-intro">{alert.explanation.summary || alert.urgency_reason}</p>
    {error && <p role="alert" className="panel-error">{error}</p>}
    <div className="incident-actions">
      <button onClick={() => onExplain(fromAlert(alert))}>Why this alert?</button>
      {onCase && session.can("case.write") && <button disabled={busy} onClick={() => void perform(async () => { const record = await reviewApi.createCase({alert_id: alert.alert_id}); onCase(record.case_id); })}>Open case</button>}
      <button onClick={() => requestReplay({ from: new Date(new Date(alert.ts).getTime() - 60000).toISOString(), to: new Date(new Date(alert.last_ts).getTime() + 60000).toISOString(), label: alert.title })}>Replay incident</button>
      {session.can("alerts.write") && alert.state === "open" && <button disabled={busy} onClick={() => void perform(async () => { upsertAlert(await api.acknowledgeAlert(alert.alert_id, "acknowledged from incident workspace")); })}>Acknowledge</button>}
    </div>
    <section className="incident-section"><h3>Camera evidence</h3>{evidence.length ? evidence.map(reference => <EvidenceImage key={reference} reference={reference} />) : <p className="panel-empty">No recorded image is attached to this incident. Sensor references remain available in the explanation.</p>}</section>
    <section className="incident-section"><h3>Affected entities · current state</h3>{alert.entity_ids.length ? <ul className="entity-links">{alert.entity_ids.map(id => <li key={id}><button onClick={() => selectEntity(id)}>{entities.get(id)?.label || id}</button><span>{entities.get(id)?.state.zone_id?.replace(/_/g, " ") || "Not currently observed"}</span></li>)}</ul> : <p className="panel-empty">No affected entities are recorded.</p>}</section>
    <section className="incident-section"><h3>Recorded sequence</h3>{events.length ? <ol className="incident-sequence">{events.toSorted((a, b) => a.ts.localeCompare(b.ts)).map(event => <li key={event.event_id}><time>{new Date(event.ts).toLocaleTimeString()}</time><span>{event.explanation.summary || event.type.replace(/_/g, " ")}</span></li>)}</ol> : <p className="panel-empty">No linked event records loaded for this time window.</p>}</section>
    <section className="incident-section"><h3>Response options</h3>{decisions.length ? decisions.map(decision => <article className="response-card" key={decision.decision_id}>
      <span className="mode-tag">{decision.approval}</span><p>{decision.rationale || decision.expected_effect}</p>
      {decision.options.map(option => <div className="response-option" key={option.option_id}><strong>{option.expected_effect || option.action}</strong><small>Score {option.score.toFixed(1)} · cost {option.cost} · risk {option.risk}</small>{!option.feasible && <p className="service-problem">{option.rejection_reason || "Not feasible"}</p>}{decision.approval === "pending" && session.can("decision.approve") && <button disabled={busy || !option.feasible} onClick={() => void perform(() => api.approveDecision(decision.decision_id, option.option_id))}>Approve this option</button>}</div>)}
      <div className="source-actions"><button onClick={() => onExplain(fromDecision(decision))}>Decision evidence</button>{decision.approval === "pending" && session.can("decision.reject") && <button disabled={busy} onClick={() => void perform(() => api.rejectDecision(decision.decision_id, "rejected from incident workspace"))}>Reject</button>}</div>
    </article>) : <p className="panel-empty">No linked recommendations loaded. A mission can be drafted for a human-led response.</p>}</section>
    <section className="incident-section"><h3>Missions in this zone</h3>{missions.map(mission => <button className="mission-link" key={mission.mission_id} onClick={() => onMission(mission.mission_id)}><span>{mission.name}</span><small>{mission.state}</small></button>)}{!missions.length && <p className="panel-empty">No missions in this zone were loaded.</p>}{session.can("mission.write") && <button className="primary wide-button" disabled={busy} onClick={() => void perform(createMission)}>Draft an incident response mission</button>}<p className="feed-note">A new mission starts as a draft. Resource assignment and activation remain explicit actions.</p></section>
  </div>;
}
