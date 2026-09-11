import { useEffect, useState } from "react";
import { explainError } from "../lib/api";
import * as session from "../lib/session";
import { workbenchOps, type VideoJob } from "../lib/workbench-operations";
import "./review.css";
import "./workbench-operations.css";

export function ProcessingQueuePanel({onOpenVideo}: {onOpenVideo?: (id: string, atS?: number, analysisId?: string) => void}) {
  session.useSession();
  const [jobs, setJobs] = useState<VideoJob[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [filter, setFilter] = useState("active");
  const [reload, setReload] = useState(0);
  useEffect(() => {
    const controller = new AbortController(); let timer: ReturnType<typeof setTimeout>;
    const refresh = async () => {
      try { const result = await workbenchOps.jobs(controller.signal); if (!controller.signal.aborted) { setJobs(result.jobs); setError(null); } }
      catch (cause) { if (!controller.signal.aborted) setError(explainError(cause)); }
      finally { if (!controller.signal.aborted) { setLoading(false); timer = setTimeout(() => void refresh(), 2500); } }
    };
    void refresh(); return () => { controller.abort(); clearTimeout(timer); };
  }, [reload]);
  const active = (job: VideoJob) => ["queued", "running", "cancelling"].includes(job.status);
  const visible = jobs.filter(job => filter === "all" || (filter === "active" ? active(job) : !active(job)));
  async function act(job: VideoJob, action: "cancel" | "retry") {
    setBusy(job.job_id); setError(null);
    try { await workbenchOps[action](job); setReload(value => value + 1); }
    catch (cause) { setError(explainError(cause)); }
    finally { setBusy(null); }
  }
  return <section className="ops-workbench">
    <header className="rv-page-head"><div><span className="rv-kicker">PROCESSING / SAVED WORK</span><h1>Processing queue</h1><p>Submit recordings, follow progress, and return to completed runs.</p></div><button className="rv-button" onClick={() => setReload(value => value + 1)}>Refresh queue</button></header>
    <div className="ops-stat-strip"><div><strong>{jobs.filter(job => job.status === "running").length}</strong><span>processing now</span></div><div><strong>{jobs.filter(job => job.status === "queued").length}</strong><span>waiting</span></div><div><strong>{jobs.filter(job => job.status === "completed").length}</strong><span>completed</span></div></div>
    <p className="rv-small">One recording processes at a time. Interrupted work is recovered with a bounded retry budget, and every attempt preserves its own analysis version. Cancellation is applied at the next safe processing boundary.</p>
    {error && <p className="rv-error" role="alert">{error}</p>}
    <label className="ops-inline-filter">Show jobs<select value={filter} onChange={event => setFilter(event.target.value)}><option value="active">Active jobs</option><option value="finished">Finished jobs</option><option value="all">All jobs</option></select></label>
    {loading && <p>Loading saved jobs…</p>}{!loading && !visible.length && <p className="rv-empty">No {filter === "all" ? "saved" : filter} jobs. Open Footage, save a zone and rule, then choose Run analysis.</p>}
    <div className="ops-job-list">{visible.map(job => <article key={job.job_id} className="ops-job">
      <header><div><span className={`rv-badge ops-status-${job.status}`}>{job.status}</span><h2>{job.video_title || job.video_id}</h2></div><span className="rv-small">Attempt {job.attempt} / {job.max_attempts}</span></header>
      <progress max={1} value={Math.max(0, Math.min(1, job.progress || 0))} aria-label={`Processing progress for ${job.video_title || job.video_id}`} /><p className="rv-small">{Math.round((job.progress || 0) * 100)}% · Queued {new Date(job.queued_at).toLocaleString()}</p>
      {job.error && <p className="rv-error">{job.error}</p>}
      <div className="rv-row"><button className="rv-button" onClick={() => onOpenVideo?.(job.video_id, 0, job.status === "completed" ? job.analysis_id : undefined)}>Open recording ↗</button>{session.can("review.write") && active(job) && <button className="rv-button" disabled={Boolean(busy) || job.status === "cancelling"} onClick={() => void act(job, "cancel")}>{job.status === "cancelling" ? "Cancelling…" : "Cancel job"}</button>}{session.can("review.write") && ["failed", "interrupted", "cancelled"].includes(job.status) && <button className="rv-button" disabled={Boolean(busy)} onClick={() => void act(job, "retry")}>Retry as a new job</button>}</div>
    </article>)}</div>
  </section>;
}
