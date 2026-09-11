import { useEffect, useState, type FormEvent } from "react";
import { explainError } from "../lib/api";
import * as session from "../lib/session";
import { cleanupReason, storageSize, workbenchOps, type CleanupPreview, type StorageOverview, type StoredVideo } from "../lib/workbench-operations";
import "./review.css";
import "./workbench-operations.css";

export function StoragePanel() {
  session.useSession(); const canWrite = session.can("storage.write");
  const [data, setData] = useState<StorageOverview | null>(null);
  const [selected, setSelected] = useState<string[]>([]);
  const [days, setDays] = useState(30);
  const [preview, setPreview] = useState<CleanupPreview | null>(null);
  const [mode, setMode] = useState<"archive" | "purge">("archive");
  const [confirmation, setConfirmation] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [reload, setReload] = useState(0);
  useEffect(() => { const controller = new AbortController(); void workbenchOps.storage(controller.signal).then(result => { if (!controller.signal.aborted) { setData(result); setDays(result.settings.archive_after_days); } }).catch(cause => { if (!controller.signal.aborted) setError(explainError(cause)); }); return () => controller.abort(); }, [reload]);
  const eligible = preview?.videos.filter(video => video.eligible) ?? [];
  const phrase = `DELETE ${eligible.length} RECORDING${eligible.length === 1 ? "" : "S"}`;
  function toggle(id: string) { setSelected(current => current.includes(id) ? current.filter(value => value !== id) : [...current, id]); setPreview(null); setConfirmation(""); }
  async function action(work: () => Promise<void>) { setBusy(true); setError(null); setMessage(null); try { await work(); } catch (cause) { setError(explainError(cause)); } finally { setBusy(false); } }
  function refresh(messageText: string) { setMessage(messageText); setSelected([]); setPreview(null); setConfirmation(""); setReload(value => value + 1); }
  async function saveRetention(event: FormEvent) { event.preventDefault(); if (!data) return; await action(async () => { await workbenchOps.retention(data.settings.revision, days); refresh("Retention suggestion saved. No files were changed."); }); }
  async function inspect(purge: boolean, allAged = false) { await action(async () => { const result = purge ? await workbenchOps.purgePreview(selected) : await workbenchOps.preview(allAged ? undefined : selected); setMode(purge ? "purge" : "archive"); setPreview(result); setConfirmation(""); }); }
  async function archive() { await action(async () => { await workbenchOps.archive(eligible); refresh("Selected recordings archived. Their files remain available for restore; no disk space was reclaimed."); }); }
  async function restore(video: StoredVideo) { await action(async () => { await workbenchOps.restore(video); refresh("Recording restored to the footage library."); }); }
  async function purge() { if (!preview || confirmation !== phrase) return; await action(async () => { await workbenchOps.purge(preview); refresh("Cleanup request completed. Review the inventory for any files that still need attention."); }); }
  return <section className="ops-workbench">
    <header className="rv-page-head"><div><span className="rv-kicker">ADMINISTRATION / MEDIA LIFECYCLE</span><h1>Storage & retention</h1><p>Inspect usage, protect evidence, and review every cleanup before applying it.</p></div><button className="rv-button" disabled={busy} onClick={() => { setPreview(null); setReload(value => value + 1); }}>Refresh storage</button></header>
    {error && <p className="rv-error" role="alert">{error}</p>}{message && <p className="rv-notice" role="status">{message}</p>}
    {!data && !error && <p>Inspecting recorded-media storage…</p>}
    {data && <><div className="ops-stat-strip"><div><strong>{storageSize(data.usage.total_bytes)}</strong><span>recorded-media files</span></div><div><strong>{data.usage.video_count}</strong><span>stored recordings</span></div><div><strong>{storageSize(data.usage.archived_bytes)}</strong><span>in reversible archive</span></div></div>
      <p className="rv-small">{data.usage.note} Case-linked recordings, evaluation data, exported evidence, and active jobs are protected. Reference and file checks run again before changes are applied.</p>
      {!data.usage.inspection_complete && <p className="rv-error">Some files could not be inspected. Reported bytes are incomplete; affected recordings cannot be cleaned up.</p>}
      <form className="ops-toolbar" onSubmit={event => void saveRetention(event)}><label>Suggest archive after (days)<input type="number" min={1} max={3650} required value={days} disabled={!canWrite || busy} onChange={event => setDays(Number(event.target.value))} /></label><button className="rv-button" type="submit" disabled={!canWrite || busy || days === data.settings.archive_after_days}>Save retention setting</button><button className="rv-button" type="button" disabled={!canWrite || busy} onClick={() => void inspect(false, true)}>Preview aged recordings</button></form>
      <p className="rv-small">Automatic archive and permanent deletion are off. Archiving hides a recording and can be reversed; only confirmed permanent cleanup frees its disk space.</p>
      <div className="rv-row"><span>{selected.length} selected</span><button className="rv-button" disabled={!selected.length || busy} onClick={() => { setSelected([]); setPreview(null); setConfirmation(""); }}>Clear selection</button><button className="rv-button" disabled={!canWrite || !selected.length || busy} onClick={() => void inspect(false)}>Preview archive</button><button className="rv-button ops-danger" disabled={!canWrite || !selected.length || busy} onClick={() => void inspect(true)}>Preview permanent cleanup</button></div>
      {preview && <section className={`ops-confirm ${mode === "purge" ? "ops-danger" : ""}`} aria-label="Cleanup preview"><span className="rv-kicker">REVIEW BEFORE APPLYING</span><h2>{mode === "purge" ? "Permanent cleanup" : "Reversible archive"} · {eligible.length} eligible</h2><ul>{preview.videos.map(video => <li key={video.video_id}><strong>{video.title}</strong> — {video.eligible ? mode === "purge" ? `${storageSize(video.bytes)} to remove permanently` : "can be archived" : video.reasons.map(cleanupReason).join("; ")}</li>)}</ul>
        {mode === "purge" ? <><p>These originals, playback files, and analysis frames will be permanently removed. Saved record history remains. This cannot be undone without a separate backup. Preview expires {preview.expires_at ? new Date(preview.expires_at).toLocaleTimeString() : "soon"}.</p><label>Type {phrase} to confirm<input value={confirmation} onChange={event => setConfirmation(event.target.value)} disabled={busy || !eligible.length} autoComplete="off" /></label><button className="rv-button ops-danger" disabled={!canWrite || busy || !eligible.length || confirmation !== phrase} onClick={() => void purge()}>Permanently delete the listed recordings</button></> : <><p>Archive keeps the media on disk. You can restore these recordings from this page.</p><button className="rv-button rv-primary" disabled={!canWrite || busy || !eligible.length} onClick={() => void archive()}>Archive the listed recordings</button></>}
        <button className="rv-button" disabled={busy} onClick={() => { setPreview(null); setConfirmation(""); }}>Close preview</button>
      </section>}
      <div className="ops-table-wrap"><table className="ops-table"><thead><tr><th>Select</th><th>Recording</th><th>Stored bytes</th><th>State / protection</th><th>Action</th></tr></thead><tbody>{data.videos.map(video => <tr key={video.video_id}><td><input type="checkbox" aria-label={`Select ${video.title}`} checked={selected.includes(video.video_id)} disabled={!canWrite || busy || Boolean(video.purged_at)} onChange={() => toggle(video.video_id)} /></td><td><strong>{video.title}</strong><p className="rv-small">Revision {video.revision}</p>{video.purge_error && <p className="rv-error">{video.purge_error}</p>}</td><td>{storageSize(video.bytes)}</td><td>{video.purged_at ? "Permanently removed" : video.archived_at ? "Archived" : "Active library"}<div className="ops-reasons">{video.reasons.filter(reason => reason !== "within_retention_period").map(reason => <span key={reason}>{cleanupReason(reason)}</span>)}</div></td><td>{video.archived_at && !video.purged_at && <button className="rv-button" disabled={!canWrite || busy} onClick={() => void restore(video)}>Restore</button>}</td></tr>)}</tbody></table></div>
      <p className="rv-small">Full database and media backups are a server-operator task. The repository includes a coordinated backup/restore command that verifies files and restores into a new, isolated database.</p>
    </>}
  </section>;
}
