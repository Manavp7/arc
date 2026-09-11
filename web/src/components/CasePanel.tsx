import { useEffect, useRef, useState, type FormEvent } from "react";
import { api, ApiError, explainError } from "../lib/api";
import * as session from "../lib/session";
import { downloadCase, reviewApi } from "../lib/review-api";
import { caseDraft, casePatch, type CaseDraft } from "../lib/review-state";
import { recordedEvidenceItems } from "../lib/case-attachments";
import { CaseEvidenceAttachments } from "./CaseEvidenceAttachments";
import type { CaseRecord } from "../lib/review-types";
import "./review.css";

export interface CasePanelProps { initialCaseId?: string; onOpenVideo?: (id: string, atS?: number, analysisId?: string) => void; onOpenMission?: (id: string) => void; onPrepareEvidence?: (id: string) => void; onCompareEvidence?: (caseId: string) => void }
export function CasePanel({ initialCaseId, onOpenVideo, onOpenMission, onPrepareEvidence, onCompareEvidence }: CasePanelProps) {
  session.useSession();
  const [cases, setCases] = useState<CaseRecord[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(initialCaseId ?? null);
  const selectedRef = useRef(selectedId); selectedRef.current = selectedId;
  const [record, setRecord] = useState<CaseRecord | null>(null);
  const [draft, setDraft] = useState<CaseDraft | null>(null);
  const [dirty, setDirty] = useState(false);
  const [evidenceDirty, setEvidenceDirty] = useState(false);
  const [note, setNote] = useState("");
  const [missionId, setMissionId] = useState("");
  const [createdMission, setCreatedMission] = useState<string | null>(null);
  const [missionChoices, setMissionChoices] = useState<{ mission_id: string; name: string; state: string }[]>([]);
  const [filter, setFilter] = useState("all");
  const [error, setError] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState<string | null>(null);
  const [revision, setRevision] = useState(0);
  const canWrite = session.can("case.write");
  useEffect(() => { if (initialCaseId) setSelectedId(initialCaseId); }, [initialCaseId]);
  useEffect(() => {
    const controller = new AbortController();
    void reviewApi.cases(controller.signal).then(result => { setCases(result.cases); setSelectedId(current => current ?? result.cases[0]?.case_id ?? null); }).catch(cause => { if (!controller.signal.aborted) setError(explainError(cause)); }).finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [revision]);
  useEffect(() => {
    const controller = new AbortController();
    setRecord(null); setDraft(null); setDirty(false); setEvidenceDirty(false); setNote(""); setMissionId(""); setCreatedMission(null); setMessage(null); setError(null);
    if (!selectedId) return () => controller.abort();
    setLoading(true);
    void reviewApi.case(selectedId, controller.signal).then(value => { if (!controller.signal.aborted) { setRecord(value); setDraft(caseDraft(value)); } }).catch(cause => { if (!controller.signal.aborted) setError(explainError(cause)); }).finally(() => { if (!controller.signal.aborted) setLoading(false); });
    if (session.can("mission.read")) void api.missions().then(result => { if (!controller.signal.aborted) setMissionChoices(result.missions as typeof missionChoices); }).catch(() => { if (!controller.signal.aborted) setMissionChoices([]); });
    return () => controller.abort();
  }, [selectedId, revision]);
  function choose(id: string) { if (busy) return; if (dirty || evidenceDirty || note.trim()) { setError("Save or discard the current edits before opening another case."); return; } setSelectedId(id); }
  function change(update: Partial<CaseDraft>) { setDraft(current => current ? { ...current, ...update } : current); setDirty(true); }
  async function refreshDetail(id: string) {
    const value = await reviewApi.case(id);
    if (selectedRef.current !== id) return;
    setRecord(value); setDraft(caseDraft(value)); setDirty(false);
    setCases(current => current.map(row => row.case_id === value.case_id ? value : row));
  }
  async function save(event: FormEvent) {
    event.preventDefault(); if (!record || !draft) return;
    setBusy("save"); setError(null);
    try { await reviewApi.updateCase(record.case_id, casePatch(draft, record.revision)); await refreshDetail(record.case_id); setMessage("Case changes saved with your authenticated identity."); }
    catch (cause) { setError(cause instanceof ApiError && cause.status === 409 ? "This case changed elsewhere. Your draft is still visible. Reload the saved case before applying your changes again." : explainError(cause)); }
    finally { setBusy(null); }
  }
  async function addNote(event: FormEvent) {
    event.preventDefault(); if (!record || !note.trim()) return;
    setBusy("note"); setError(null);
    try { await reviewApi.note(record.case_id, note.trim()); setNote(""); await refreshDetail(record.case_id); setMessage("Note added to the permanent case timeline."); }
    catch (cause) { setError(explainError(cause)); } finally { setBusy(null); }
  }
  async function linkMission(id: string) {
    if (!record || !id) return; setBusy("mission"); setError(null);
    try { await reviewApi.linkMission(record.case_id, id, record.revision); setCreatedMission(null); setMissionId(""); await refreshDetail(record.case_id); setMessage("Mission linked to this case. Its operational state is unchanged."); }
    catch (cause) { setError(explainError(cause)); } finally { setBusy(null); }
  }
  async function createMission() {
    if (!record) return; setBusy("mission"); setError(null);
    try {
      const mission = await api.createMission({ name: `Case response: ${record.title}`.slice(0, 180), objectives: [{ description: `Investigate and resolve case ${record.case_id}` }] }) as { mission_id: string };
      setCreatedMission(mission.mission_id); setMissionId(mission.mission_id);
      await reviewApi.linkMission(record.case_id, mission.mission_id, record.revision);
      setCreatedMission(null); setMissionId(""); await refreshDetail(record.case_id); setMessage("A draft response mission was created and linked. Activation and resources remain explicit actions.");
    } catch (cause) { setError(explainError(cause)); } finally { setBusy(null); }
  }
  async function exportReport(format: "json" | "html") {
    if (!record) return; setBusy("export"); setError(null);
    try { await downloadCase(record.case_id, format); setMessage(`Saved case exported as ${format.toUpperCase()}.`); }
    catch (cause) { setError(explainError(cause)); } finally { setBusy(null); }
  }
  const visible = cases.filter(row => filter === "all" || row.status === filter);
  return <div className="review-shell">
    <aside className="review-library"><span className="rv-kicker">INVESTIGATION RECORDS</span><h2>Cases</h2><p className="rv-muted">Evidence, ownership and decisions in one durable record.</p><label className="rv-filter-label">Case status<select value={filter} onChange={event => setFilter(event.target.value)}><option value="all">All cases</option><option value="open">Open</option><option value="investigating">Investigating</option><option value="resolved">Resolved</option></select></label><div className="rv-library-head"><span>{visible.length} {visible.length === 1 ? "case" : "cases"}</span><button disabled={dirty || evidenceDirty || Boolean(busy) || Boolean(note.trim())} onClick={() => setRevision(value => value + 1)}>Refresh</button></div><div className="rv-library-list">{visible.map(row => <button key={row.case_id} className={`rv-library-item ${row.case_id === selectedId ? "is-selected" : ""}`} disabled={Boolean(busy)} onClick={() => choose(row.case_id)}><strong>{row.title}</strong><span>{row.status} · {row.owner || "Unassigned"}</span>{row.due_at && <small>Due {new Date(row.due_at).toLocaleString()}</small>}</button>)}</div>{!cases.length && !loading && <p className="rv-empty">Open a recorded event, frozen human observation or live alert and choose Create case to begin.</p>}</aside>
    <main className="review-main">
      <header className="rv-page-head"><div><span className="rv-kicker">CASEWORK / ACCOUNTABILITY</span><h1>{record?.title ?? "Follow an investigation"}</h1></div>{record && <span className={`rv-badge ${record.status === "resolved" ? "rv-positive" : ""}`}>{record.status}</span>}</header>
      {error && <div className="rv-error" role="alert">{error}{dirty && <button className="rv-text-button" onClick={() => { setDirty(false); setRevision(value => value + 1); }}>Reload saved case (discard draft)</button>}</div>}{message && <p className="rv-notice" role="status">{message}</p>}
      {loading && <p className="rv-empty">Loading case records…</p>}
      {!record && !loading && <div className="rv-blank"><span>EVIDENCE → REVIEW → RESOLUTION</span><h2>Give every finding an owner.</h2><p>Select a case or create one from an event or frozen human observation. Its source evidence is captured by the server and stays attached to the investigation.</p></div>}
      {record && draft && <>
        <div className="rv-case-meta"><span>{record.case_id}</span><span>Opened {new Date(record.created_at).toLocaleString()} by {record.created_by}</span><span>Revision {record.revision}</span></div>
        <div className="rv-case-grid"><section className="rv-editor-card"><div className="rv-section-title"><div><span className="rv-kicker">OWNERSHIP / DISPOSITION</span><h2>Review this case</h2></div>{dirty && <span className="rv-badge rv-warning">Unsaved</span>}</div>
          <form onSubmit={event => void save(event)}><fieldset disabled={!canWrite || Boolean(busy)} className="rv-fieldset"><div className="rv-form-grid"><label className="rv-span-two">Case title<input required maxLength={200} value={draft.title} onChange={event => change({ title: event.target.value })} /></label><label>Owner<input maxLength={200} placeholder="User or team" value={draft.owner} onChange={event => change({ owner: event.target.value })} /></label><label>Due date (local time)<input type="datetime-local" value={draft.due} onChange={event => change({ due: event.target.value })} /></label><label>Workflow status<select value={draft.status} onChange={event => change({ status: event.target.value as CaseDraft["status"] })}><option value="open">Open</option><option value="investigating">Investigating</option><option value="resolved">Resolved</option></select></label><label>Review outcome<select value={draft.verdict} onChange={event => change({ verdict: event.target.value as CaseDraft["verdict"] })}><option value="unreviewed">Not reviewed</option><option value="confirmed">Confirmed finding</option><option value="false_positive">{record.evidence.source === "reviewer_annotation" ? "Observation not supported" : "False positive"}</option></select></label><label className="rv-span-two">Investigation summary<textarea rows={3} maxLength={8000} value={draft.summary} onChange={event => change({ summary: event.target.value })} /></label><label className="rv-span-two">Resolution reason {draft.status === "resolved" ? "(required)" : "(optional)"}<textarea rows={2} maxLength={8000} required={draft.status === "resolved"} value={draft.resolution} onChange={event => change({ resolution: event.target.value })} placeholder="What did you determine, and what response was taken?" /></label></div><div className="rv-row rv-form-actions"><button type="submit" className="rv-button rv-primary" disabled={!dirty}>{busy === "save" ? "Saving…" : "Save case"}</button><button type="button" className="rv-button" disabled={!dirty} onClick={() => { setDraft(caseDraft(record)); setDirty(false); }}>Discard changes</button></div></fieldset></form>
          <p className="rv-small">{record.evidence.source === "reviewer_annotation" ? "This case began with a human observation. Its outcome is tracked separately and excluded from detector reviewed precision, even if detector evidence is later attached." : "Detector-origin outcome labels contribute to reviewed precision. They do not measure missed incidents or detector recall."}</p>
        </section><section className="rv-editor-card"><div className="rv-section-title"><div><span className="rv-kicker">EVIDENCE / EXPORTS</span><h2>Keep the source history together.</h2></div></div>
          <p className="rv-muted">{record.evidence_count ?? 1} immutable source{(record.evidence_count ?? 1) === 1 ? "" : "s"} attached to this case. Review the chronological source timeline below; each recording opens its exact retained analysis.</p>
          {recordedEvidenceItems(record).length >= 2 && onCompareEvidence && <button className="rv-button" disabled={dirty || evidenceDirty || Boolean(busy) || Boolean(note.trim())} onClick={() => onCompareEvidence(record.case_id)}>Compare footage ↗</button>}
          {recordedEvidenceItems(record).length > 0 && onPrepareEvidence && <button className="rv-button" disabled={dirty || evidenceDirty || Boolean(busy) || Boolean(note.trim())} onClick={() => onPrepareEvidence(record.case_id)}>Prepare evidence package ↗</button>}
          <div className="rv-row rv-form-actions"><button className="rv-button" disabled={Boolean(busy)} onClick={() => void exportReport("html")}>Export HTML report</button><button className="rv-button" disabled={Boolean(busy)} onClick={() => void exportReport("json")}>Export JSON</button></div><p className="rv-small">Reports include all saved source snapshots and authored attachment notes, subject to your current access permissions. Unsaved edits are excluded.</p>
        </section></div>
        <CaseEvidenceAttachments key={record.case_id} record={record} disabled={dirty || Boolean(busy) || Boolean(note.trim())} onDraftChange={setEvidenceDirty} onBusyChange={value => setBusy(value ? "evidence" : null)} onAttached={value => {setRecord(value); setDraft(caseDraft(value)); setCases(current => current.map(row => row.case_id === value.case_id ? value : row)); setDirty(false); setEvidenceDirty(false);}} onOpenVideo={onOpenVideo} />
        <div className="rv-case-grid"><section className="rv-editor-card"><div className="rv-section-title"><div><span className="rv-kicker">OPERATOR RECORD</span><h2>Investigation notes</h2></div></div>
          <div className="rv-notes">{(record.notes ?? []).map(entry => <article key={entry.note_id}><header><strong>{entry.author}</strong><time>{new Date(entry.created_at).toLocaleString()}</time></header><p>{entry.text}</p></article>)}{!record.notes?.length && <p className="rv-empty">No authored notes yet. Add observations, handover details or a review decision.</p>}</div>
          <form onSubmit={event => void addNote(event)}><label>Add a permanent note<textarea rows={3} maxLength={8000} disabled={!canWrite || Boolean(busy) || dirty} value={note} onChange={event => setNote(event.target.value)} placeholder="What should the next operator know?" /></label><button className="rv-button rv-primary" type="submit" disabled={!canWrite || !note.trim() || Boolean(busy) || dirty}>{busy === "note" ? "Adding…" : "Add note"}</button></form><p className="rv-small">Notes are append-only and attributed to your signed-in identity.{dirty ? " Save case edits before adding a note." : ""}</p>
        </section><section className="rv-editor-card"><div className="rv-section-title"><div><span className="rv-kicker">RESPOND / LINKED OPERATIONS</span><h2>Missions and decisions</h2></div></div>
          <div className="rv-linked-records">{(record.missions ?? []).map(mission => <button key={mission.mission_id} onClick={() => onOpenMission?.(mission.mission_id)}><strong>{mission.name}</strong><span>{mission.state} ↗</span></button>)}{!record.missions?.length && <p className="rv-empty">No linked missions. A mission stays in draft until it is explicitly activated.</p>}</div>
          {createdMission && <p className="rv-notice">Draft {createdMission} was created. Link it below to finish connecting it to this case.</p>}
          <label>Existing mission<select value={missionId} disabled={!canWrite || Boolean(busy) || dirty} onChange={event => setMissionId(event.target.value)}><option value="">Choose a mission</option>{createdMission && !missionChoices.some(row => row.mission_id === createdMission) && <option value={createdMission}>{createdMission} (new draft)</option>}{missionChoices.filter(mission => !record.mission_ids.includes(mission.mission_id)).map(mission => <option key={mission.mission_id} value={mission.mission_id}>{mission.name} · {mission.state}</option>)}</select></label>
          <div className="rv-row"><button className="rv-button" disabled={!canWrite || !missionId || Boolean(busy) || dirty} onClick={() => void linkMission(missionId)}>Link selected mission</button><button className="rv-button" disabled={!canWrite || !session.can("mission.write") || Boolean(busy) || dirty || Boolean(createdMission)} onClick={() => void createMission()}>Create and link draft</button></div>
          {(record.decisions ?? []).map(decision => <div className="rv-decision-summary" key={decision.decision_id}><span className="rv-badge">{decision.approval ?? "Recorded decision"}</span><p>{decision.rationale ?? decision.decision_id}</p></div>)}
        </section></div>
        <section className="rv-editor-card"><div className="rv-section-title"><div><span className="rv-kicker">IMMUTABLE HISTORY</span><h2>Case activity</h2></div></div><ol className="rv-audit-timeline">{(record.timeline ?? []).map((entry, index) => <li key={`${entry.ts}-${index}`}><time>{new Date(entry.ts).toLocaleString()}</time><div><strong>{entry.author}</strong><p>{entry.summary}</p></div><span className="rv-badge">{entry.kind.replace(/_/g, " ")}</span></li>)}</ol></section>
      </>}
    </main>
  </div>;
}
