import { useEffect, useState, type FormEvent } from "react";
import { explainError } from "../lib/api";
import * as session from "../lib/session";
import { reviewApi } from "../lib/review-api";
import { workbenchOps, type Handover, type InboxCase, type InboxSnapshot, type InboxView } from "../lib/workbench-operations";
import "./review.css";
import "./workbench-operations.css";

export function OperatorInboxPanel({onOpenCase}: {onOpenCase?: (id: string) => void}) {
  const identity = session.useSession();
  const [data, setData] = useState<InboxSnapshot | null>(null);
  const [handovers, setHandovers] = useState<Handover[]>([]);
  const [view, setView] = useState<InboxView>("active");
  const [priority, setPriority] = useState("");
  const [q, setQ] = useState("");
  const [selected, setSelected] = useState<string[]>([]);
  const [title, setTitle] = useState("");
  const [note, setNote] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [reload, setReload] = useState(0);
  const canWrite = session.can("case.write");
  useEffect(() => {
    const controller = new AbortController(); let timer: ReturnType<typeof setTimeout>;
    const refresh = async () => {
      try { const [inbox, notes] = await Promise.all([workbenchOps.inbox(view, priority, q, controller.signal), workbenchOps.handovers(controller.signal)]); if (!controller.signal.aborted) { setData(inbox); setHandovers(notes.handovers); setError(null); } }
      catch (cause) { if (!controller.signal.aborted) setError(explainError(cause)); }
      finally { if (!controller.signal.aborted) timer = setTimeout(() => void refresh(), 30000); }
    };
    void refresh(); return () => { controller.abort(); clearTimeout(timer); };
  }, [view, priority, q, reload]);
  async function patch(row: InboxCase, changes: Record<string, unknown>) {
    setBusy(true); setError(null);
    try { await reviewApi.updateCase(row.case_id, {expected_revision: row.revision, ...changes}); setReload(value => value + 1); setMessage("Case updated."); }
    catch (cause) { setError(explainError(cause)); } finally { setBusy(false); }
  }
  async function saveHandover(event: FormEvent) {
    event.preventDefault(); setBusy(true); setError(null);
    try { await workbenchOps.handover(title, note, selected); setTitle(""); setNote(""); setSelected([]); setMessage("Shift handover saved with the selected case revisions."); setReload(value => value + 1); }
    catch (cause) { setError(explainError(cause)); } finally { setBusy(false); }
  }
  return <section className="ops-workbench">
    <header className="rv-page-head"><div><span className="rv-kicker">OPERATIONS / TODAY'S WORK</span><h1>Operator inbox</h1><p>Prioritize cases, take ownership, and leave the next shift a clear record.</p></div><button className="rv-button" disabled={busy} onClick={() => setReload(value => value + 1)}>Refresh inbox</button></header>
    {error && <p role="alert" className="rv-error">{error}</p>}{message && <p role="status" className="rv-notice">{message}</p>}
    <div className="ops-stat-strip"><div><strong>{data?.counts.mine ?? "—"}</strong><span>assigned to me</span></div><div><strong className="ops-overdue">{data?.counts.overdue ?? "—"}</strong><span>past due</span></div><div><strong>{data?.counts.unassigned ?? "—"}</strong><span>unassigned</span></div></div>
    <div className="ops-toolbar"><label>Find cases<input type="search" value={q} maxLength={200} onChange={event => setQ(event.target.value)} placeholder="Title, owner, zone, or case ID" /></label><label>Inbox view<select value={view} onChange={event => setView(event.target.value as InboxView)}><option value="active">Active cases</option><option value="mine">Assigned to me</option><option value="unassigned">Unassigned</option><option value="overdue">Overdue</option><option value="due_soon">Due in 24 hours</option><option value="resolved">Resolved</option><option value="all">All cases</option></select></label><label>Priority filter<select value={priority} onChange={event => setPriority(event.target.value)}><option value="">All priorities</option>{["urgent", "high", "normal", "low"].map(value => <option key={value}>{value}</option>)}</select></label></div>
    {data?.possibly_truncated && <p className="rv-notice">This inbox is bounded to the latest 5,000 visible cases. Counts may omit older records.</p>}
    <p className="rv-small">Sorted by overdue status, priority, then due date. Set deadlines on each case. The notification bell shows personal assignments and deadline reminders; no external messages are sent.</p>
    {data && !data.cases.length && <p className="rv-empty">No cases match this view.</p>}
    {data && data.cases.length > 0 && <div className="ops-table-wrap"><table className="ops-table"><thead><tr><th>Handover</th><th>Case</th><th>Owner</th><th>Priority</th><th>Due</th><th>Status</th></tr></thead><tbody>{data.cases.map(row => <tr key={row.case_id}><td><input type="checkbox" aria-label={`Include ${row.title} in handover`} checked={selected.includes(row.case_id)} disabled={!canWrite || busy} onChange={() => setSelected(current => current.includes(row.case_id) ? current.filter(id => id !== row.case_id) : [...current, row.case_id])} /></td><td><button className="ops-record-button" onClick={() => onOpenCase?.(row.case_id)}>{row.title} ↗</button><p className="rv-small">{row.zone_id ?? "No zone"}</p></td><td>{row.owner || "Unassigned"}{canWrite && row.owner !== identity?.subject && row.status !== "resolved" && <p><button className="rv-button" disabled={busy} onClick={() => void patch(row, {owner: identity?.subject})}>Assign to me</button></p>}</td><td><select aria-label={`Priority for ${row.title}`} value={row.priority ?? "normal"} disabled={!canWrite || busy} onChange={event => void patch(row, {priority: event.target.value})}>{["urgent", "high", "normal", "low"].map(value => <option key={value}>{value}</option>)}</select></td><td className={row.overdue ? "ops-overdue" : ""}>{row.due_at ? new Date(row.due_at).toLocaleString() : "No deadline"}{row.overdue && <p>Overdue</p>}</td><td>{row.status}</td></tr>)}</tbody></table></div>}
    <div className="ops-handover-layout"><section><span className="rv-kicker">SHIFT / HANDOVER</span><h2>Leave the next operator context</h2><p className="rv-small">{selected.length} selected case{selected.length === 1 ? "" : "s"}. Handovers keep the author, text, and case revisions from the moment they are saved.</p>{selected.length > 0 && <button className="rv-button" disabled={busy} onClick={() => setSelected([])}>Clear selected cases</button>}<form onSubmit={event => void saveHandover(event)}><label>Handover title<input value={title} maxLength={160} required disabled={!canWrite || busy} onChange={event => setTitle(event.target.value)} /></label><label>Handover note<textarea rows={5} value={note} maxLength={8000} required disabled={!canWrite || busy} onChange={event => setNote(event.target.value)} placeholder="What changed, what remains open, and what needs attention next?" /></label><button className="rv-button rv-primary" disabled={!canWrite || busy || !selected.length || !title.trim() || !note.trim()}>Save permanent handover</button></form></section><section><span className="rv-kicker">RECENT / SHARED CONTEXT</span><h2>Saved handovers</h2>{!handovers.length && <p className="rv-empty">No visible shift handovers yet.</p>}{handovers.map(handover => <article key={handover.handover_id} className="ops-handover"><h3>{handover.title}</h3><small>{handover.author} · {new Date(handover.captured_at).toLocaleString()}</small><p>{handover.text}</p><ul>{handover.cases.map(row => <li key={row.case_id}><button className="ops-record-button" onClick={() => onOpenCase?.(row.case_id)}>{row.title} ↗</button><p className="rv-small">Captured revision {row.revision} · {row.status} · {row.owner || "Unassigned"}</p></li>)}</ul></article>)}</section></div>
  </section>;
}
