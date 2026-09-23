import { useEffect, useMemo, useState, type FormEvent } from "react";
import { explainError } from "../lib/api";
import { clipOffset, recordingTimelineApi, timelineBounds, type RecordingClock, type RecordingTimeline, type TimelineRecording } from "../lib/recording-timeline";
import * as session from "../lib/session";
import "./review.css";
import "./recording-timeline.css";

const utc = (value: string | number) => new Date(value).toISOString().replace("T", " ").replace(".000Z", " UTC");
interface Props { onOpenVideo: (id: string, atS?: number, analysisId?: string) => void; onDirtyChange?: (dirty: boolean) => void }

function ClockEditor({ row, onSaved, onClose, onDirtyChange }: { row: TimelineRecording; onSaved: () => void; onClose: () => void; onDirtyChange: (dirty: boolean) => void }) {
  const [camera, setCamera] = useState(row.clock?.camera_id ?? "");
  const [capture, setCapture] = useState(row.clock?.capture_started_at ?? "");
  const [offset, setOffset] = useState(String(row.clock?.clock_offset_s ?? 0));
  const [uncertainty, setUncertainty] = useState(row.clock?.uncertainty_s == null ? "" : String(row.clock.uncertainty_s));
  const [note, setNote] = useState(row.clock?.note ?? "");
  const [dirty, setDirty] = useState(false), [busy, setBusy] = useState(false), [error, setError] = useState("");
  useEffect(() => { onDirtyChange(dirty || busy); return () => onDirtyChange(false); }, [dirty, busy, onDirtyChange]);
  useEffect(() => {
    if (!dirty && !busy) return;
    const prevent = (event: BeforeUnloadEvent) => { event.preventDefault(); event.returnValue = ""; };
    window.addEventListener("beforeunload", prevent); return () => window.removeEventListener("beforeunload", prevent);
  }, [dirty, busy]);
  function edit(set: (value: string) => void, value: string) { set(value); setDirty(true); }
  async function save(event: FormEvent) {
    event.preventDefault(); setError("");
    if (capture && (!/(Z|[+-]\d\d:\d\d)$/.test(capture) || !Number.isFinite(Date.parse(capture)))) { setError("Use a capture time with its timezone, such as 2026-09-23T14:05:00+05:30."); return; }
    if (capture && (!camera.trim() || !note.trim())) { setError("Name the camera and describe where this clock measurement came from."); return; }
    if (capture && (!Number.isFinite(Number(offset)) || (uncertainty && !Number.isFinite(Number(uncertainty))))) { setError("Clock correction and uncertainty must be finite seconds."); return; }
    const body: RecordingClock = { revision: row.clock?.revision ?? 0, camera_id: camera.trim(), capture_started_at: capture || null, clock_offset_s: capture ? Number(offset) : 0, uncertainty_s: capture && uncertainty !== "" ? Number(uncertainty) : null, note: note.trim() };
    setBusy(true);
    try { await recordingTimelineApi.save(row.video_id, body); setDirty(false); onSaved(); }
    catch (cause) { setError(explainError(cause)); } finally { setBusy(false); }
  }
  return <form className="rv-editor-card rt-editor" onSubmit={event => void save(event)} aria-label={`Capture clock for ${row.title}`}>
    <div className="rv-row"><h2>Capture clock · {row.title}</h2><span className="rv-badge">Operator declaration</span></div>
    <p className="rv-muted">Use the recording's actual clock and explain its source. Upload time is never used as capture time. Leave capture time empty when unknown.</p>
    <div className="rt-fields"><label>Camera ID<input disabled={busy} value={camera} maxLength={100} onChange={event => edit(setCamera, event.target.value)} placeholder="loading-dock-01" /></label><label>Capture start, including timezone<input disabled={busy} value={capture} onChange={event => edit(setCapture, event.target.value)} placeholder="2026-09-23T14:05:00+05:30" /></label><label>Clock correction (seconds)<input type="number" step="0.001" min="-86400" max="86400" disabled={busy || !capture} value={offset} onChange={event => edit(setOffset, event.target.value)} /></label><label>Uncertainty ± seconds (blank = unknown)<input type="number" step="0.001" min="0" max="86400" disabled={busy || !capture} value={uncertainty} onChange={event => edit(setUncertainty, event.target.value)} /></label></div>
    <label>Clock source / measurement note<textarea disabled={busy} value={note} maxLength={1000} onChange={event => edit(setNote, event.target.value)} placeholder="For example: timestamp from the recorder; corrected against a visible shared event." /></label>
    <p className="rv-small">Corrected start = capture start + correction. A positive correction moves this footage later. Existing case snapshots and saved comparison alignments are preserved.</p>
    {error && <p className="rv-error" role="alert">{error}</p>}
    <div className="rv-row"><button className="rv-button rv-primary" disabled={busy} type="submit">{busy ? "Saving…" : "Save capture clock"}</button><button className="rv-button" disabled={busy} type="button" onClick={onClose}>{dirty ? "Discard edits" : "Close"}</button></div>
  </form>;
}

export function RecordingTimelinePanel({ onOpenVideo, onDirtyChange }: Props) {
  session.useSession();
  const [data, setData] = useState<RecordingTimeline | null>(null), [error, setError] = useState("");
  const [revision, setRevision] = useState(0), [camera, setCamera] = useState("");
  const [cursor, setCursor] = useState<number | null>(null), [editing, setEditing] = useState<string | null>(null);
  const [dirty, setDirty] = useState(false);
  useEffect(() => { onDirtyChange?.(dirty); return () => onDirtyChange?.(false); }, [dirty, onDirtyChange]);
  useEffect(() => {
    const controller = new AbortController();
    void recordingTimelineApi.list(controller.signal).then(value => { if (!controller.signal.aborted) { setData(value); setError(""); } }).catch(cause => { if (!controller.signal.aborted) setError(explainError(cause)); });
    return () => controller.abort();
  }, [revision]);
  const rows = useMemo(() => data?.recordings.filter(row => !camera || row.clock?.camera_id === camera) ?? [], [data, camera]);
  const bounds = timelineBounds(rows), instant = bounds ? Math.max(bounds[0], Math.min(bounds[1], cursor ?? bounds[0])) : null;
  const known = rows.filter(row => row.interval), unknown = rows.filter(row => !row.interval);
  const editor = data?.recordings.find(row => row.video_id === editing);
  const cameras = [...new Set(data?.recordings.map(row => row.clock?.camera_id).filter((value): value is string => Boolean(value)) ?? [])];
  function edit(row: TimelineRecording) { if (!dirty) setEditing(row.video_id); }
  function open(row: TimelineRecording, atS = 0) { if (!dirty) onOpenVideo(row.video_id, atS, row.analysis_id ?? undefined); }
  return <section className="review-main rt-panel">
    <header className="rv-page-head"><div><span className="rv-kicker">RECORDED FOOTAGE / SHARED CLOCK</span><h1>One moment. Every camera.</h1><p className="rv-muted">Line up recordings by declared capture time, then open the matching moment.</p></div><button className="rv-button" disabled={dirty} onClick={() => setRevision(value => value + 1)}>Refresh</button></header>
    {error && <p className="rv-error" role="alert">{error}</p>}
    <div className="rt-toolbar"><label>Camera<select disabled={dirty} value={camera} onChange={event => { setCamera(event.target.value); setCursor(null); }}><option value="">All cameras</option>{cameras.map(id => <option key={id}>{id}</option>)}</select></label><span>{known.length} aligned · {unknown.length} with unknown clocks</span></div>
    {bounds && instant !== null && <section className="rt-board" aria-label="Shared recording timeline">
      <div className="rt-time-controls"><label>Review moment (UTC)<input type="datetime-local" step="0.001" value={new Date(instant).toISOString().slice(0,23)} onChange={event => { const value = Date.parse(`${event.target.value}Z`); if (Number.isFinite(value)) setCursor(value); }} /></label><strong>{utc(instant)}</strong></div>
      <input className="rt-slider" type="range" aria-label="Shared timeline cursor" min={bounds[0]} max={bounds[1]} step="100" value={instant} onChange={event => setCursor(Number(event.target.value))} />
      <div className="rt-axis"><span>{utc(bounds[0])}</span><span>{utc(bounds[1])}</span></div>
      {known.map(row => {
        const start = Date.parse(row.interval!.start), end = Date.parse(row.interval!.end), span = Math.max(1, bounds[1] - bounds[0]);
        const at = clipOffset(row, instant);
        return <article className={`rt-recording ${at !== null ? "rt-active" : ""}`} key={row.video_id}><div className="rt-recording-head"><div><strong>{row.clock?.camera_id}</strong><h3>{row.title}</h3></div><div className="rv-row"><button className="rv-button rv-primary" disabled={dirty || at === null} onClick={() => at !== null && open(row, at)}>Open at cursor ↗</button>{session.can("review.write") && <button className="rv-button" disabled={dirty} onClick={() => edit(row)}>Edit clock</button>}</div></div><div className="rt-track"><span className="rt-bar" style={{left:`${100*(start-bounds[0])/span}%`,width:`${Math.max(.2,100*(end-start)/span)}%`}} /><span className="rt-cursor" style={{left:`${100*(instant-bounds[0])/span}%`}} /></div><p className="rv-small">{utc(start)} · {row.duration_s}s · {row.clock?.uncertainty_s == null ? "Clock uncertainty unknown" : `Clock uncertainty ±${row.clock.uncertainty_s}s`} · declared by {row.clock?.declared_by}</p><p className="rv-small">{row.clock?.note}</p></article>;
      })}
    </section>}
    {editor && <ClockEditor key={`${editor.video_id}:${editor.clock?.revision ?? 0}`} row={editor} onDirtyChange={setDirty} onClose={() => { setEditing(null); setDirty(false); }} onSaved={() => { setEditing(null); setDirty(false); setRevision(value => value + 1); }} />}
    {unknown.length > 0 && <section className="rt-unknown"><h2>Capture clock unknown</h2><p className="rv-muted">These clips are available to review, but cannot be placed on a shared timeline yet.</p>{unknown.map(row => <article className="rt-recording-head" key={row.video_id}><div><h3>{row.title}</h3><span className="rv-small">{row.duration_s}s · {row.clock?.camera_id || "Camera not declared"}</span></div><div className="rv-row"><button className="rv-button" disabled={dirty} onClick={() => open(row)}>Open footage</button>{session.can("review.write") && <button className="rv-button" disabled={dirty} onClick={() => edit(row)}>Set capture clock</button>}</div></article>)}</section>}
    {data && rows.length === 0 && <p className="rv-notice">No accessible recordings in this camera selection. Upload footage to begin.</p>}
    {!data && !error && <p className="rv-muted" role="status">Loading recordings…</p>}
    <p className="rv-small rt-note">{data?.note}{data?.possibly_truncated && " This inventory is bounded; older records may be omitted."}</p>
  </section>;
}
