import { useEffect, useRef, useState } from "react";
import { api, explainError } from "../lib/api";
import { protectedBlob, reviewApi } from "../lib/review-api";
import { clipProblem, type EvidenceAnnotation } from "../lib/commissioning";
import { recordedEvidenceCases, recordedEvidenceItems, evidencePosition, evidenceOriginLabel } from "../lib/case-attachments";
import type { CaseRecord } from "../lib/review-types";
import * as session from "../lib/session";
import "./review.css";
import "./commissioning.css";
import "./case-attachments.css";

type EvidencePackage = { package_id: string; case_id: string; case_title: string; attachment_id?: string; evidence_title?: string; case_revision?: number; status: "queued" | "building" | "ready" | "failed" | "interrupted"; start_s: number; end_s: number; annotations: EvidenceAnnotation[]; created_at: string; created_by: string; download_url: string; error?: string; bytes?: number; sha256?: string; manifest?: { files: {path: string; bytes: number; sha256: string}[]; encoded_duration_s: number } };

export function EvidencePackagePanel({initialCaseId, initialPackageId, onOpenCase}: {initialCaseId?: string; initialPackageId?: string; onOpenCase?: (id: string) => void}) {
  const [cases, setCases] = useState<CaseRecord[]>([]);
  const [selectedId, setSelectedId] = useState("");
  const [record, setRecord] = useState<CaseRecord | null>(null);
  const [attachmentId, setAttachmentId] = useState("original");
  const [packages, setPackages] = useState<EvidencePackage[]>([]);
  const [start, setStart] = useState("0");
  const [end, setEnd] = useState("0");
  const [notes, setNotes] = useState<{at_s: string; text: string}[]>([]);
  const [media, setMedia] = useState<string | null>(null);
  const [mediaError, setMediaError] = useState("");
  const [busy, setBusy] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState("");
  const [previewing, setPreviewing] = useState(false);
  const player = useRef<HTMLVideoElement | null>(null);
  const scrolledPackage = useRef("");
  const canBuild = session.can("case.write") && session.can("review.read") && session.can("media.read");

  useEffect(() => {
    const controller = new AbortController(); setLoading(true);
    void Promise.all([reviewApi.cases(controller.signal), api.request<{packages: EvidencePackage[]}>("/evidence-packages", {signal: controller.signal}), initialPackageId ? api.request<EvidencePackage>(`/evidence-packages/${encodeURIComponent(initialPackageId)}`, {signal: controller.signal}).catch(cause => {if (!controller.signal.aborted) setError(explainError(cause)); return null;}) : Promise.resolve(null)]).then(async ([list, bundles, focused]) => {
      if (controller.signal.aborted) return;
      const recorded = recordedEvidenceCases(list.cases);
      const matchingFocus = focused && (!initialCaseId || focused.case_id === initialCaseId) ? focused : null;
      if (focused && !matchingFocus) setError("This package belongs to a different case than the selected notification target.");
      if (matchingFocus && !recorded.some(row => row.case_id === matchingFocus.case_id)) {const detail = await reviewApi.case(matchingFocus.case_id, controller.signal); if (controller.signal.aborted) return; recorded.push(detail);}
      setCases(recorded); setPackages(matchingFocus ? [matchingFocus, ...bundles.packages.filter(row => row.package_id !== matchingFocus.package_id)] : bundles.packages); setSelectedId(matchingFocus?.case_id ?? recorded.find(row => row.case_id === initialCaseId)?.case_id ?? recorded[0]?.case_id ?? "");
      if (initialCaseId && !recorded.some(row => row.case_id === initialCaseId)) setNotice("This case has no recorded-video evidence yet. Attach a recorded event or frozen observation in Cases, then return here.");
    }).catch(cause => {if (!controller.signal.aborted) setError(explainError(cause));}).finally(() => {if (!controller.signal.aborted) setLoading(false);});
    return () => controller.abort();
  }, [initialCaseId, initialPackageId]);

  useEffect(() => {if (!loading && initialPackageId && scrolledPackage.current !== initialPackageId && packages.some(row => row.package_id === initialPackageId && row.case_id === selectedId)) {const card = document.getElementById(`package-${initialPackageId}`); if (card) {card.scrollIntoView({block: "nearest"}); scrolledPackage.current = initialPackageId;}}}, [loading, initialPackageId, selectedId, packages]);

  useEffect(() => {
    const controller = new AbortController(); setRecord(null); setMedia(null); setMediaError(""); setPreviewing(false); setNotes([]);
    if (selectedId) void reviewApi.case(selectedId, controller.signal).then(value => {
      if (controller.signal.aborted) return;
      setRecord(value); const choices = recordedEvidenceItems(value);
      setAttachmentId(choices.find(item => item.attachment_id === "original")?.attachment_id ?? choices[0]?.attachment_id ?? "");
    }).catch(cause => {if (!controller.signal.aborted) setError(explainError(cause));});
    return () => controller.abort();
  }, [selectedId]);

  const evidenceChoices = record ? recordedEvidenceItems(record) : [];
  const selectedEvidence = evidenceChoices.find(item => item.attachment_id === attachmentId);
  useEffect(() => {
    const controller = new AbortController(); let url: string | null = null; setMedia(null); setMediaError(""); setPreviewing(false); setNotes([]);
    const duration = selectedEvidence?.evidence.video?.duration_s ?? 0;
    const at = selectedEvidence ? evidencePosition(selectedEvidence) : 0;
    setStart(String(Math.max(0, Math.round((at - 3) * 10) / 10))); setEnd(String(Math.min(duration, Math.round((at + 7) * 10) / 10)));
    if (selectedEvidence?.video_id) void reviewApi.video(selectedEvidence.video_id, controller.signal).then(async video => {const blob = await protectedBlob(video.media_url, controller.signal); if (controller.signal.aborted) return; url = URL.createObjectURL(blob); setMedia(url);}).catch(cause => {if (!controller.signal.aborted) setMediaError(explainError(cause));});
    return () => {controller.abort(); if (url) URL.revokeObjectURL(url);};
  }, [selectedId, selectedEvidence?.attachment_id, selectedEvidence?.video_id]);

  const pending = packages.some(row => row.status === "queued" || row.status === "building");
  useEffect(() => {
    if (!pending) return;
    const controller = new AbortController(); let timer: number | undefined;
    async function refresh() {try {const result = await api.request<{packages: EvidencePackage[]}>("/evidence-packages", {signal: controller.signal}); if (!controller.signal.aborted) setPackages(result.packages);} catch (cause) {if (!controller.signal.aborted) setError(explainError(cause));} finally {if (!controller.signal.aborted) timer = window.setTimeout(() => void refresh(), 1500);}}
    timer = window.setTimeout(() => void refresh(), 1000);
    return () => {controller.abort(); if (timer != null) window.clearTimeout(timer);};
  }, [pending]);

  async function run(action: () => Promise<void>) {setBusy(true); setError(null); setNotice(""); try {await action();} catch (cause) {setError(explainError(cause));} finally {setBusy(false);}}
  const annotations = notes.map(note => ({at_s: note.at_s.trim() ? Number(note.at_s) : NaN, text: note.text.trim()}));
  const clipStart = start.trim() ? Number(start) : NaN, clipEnd = end.trim() ? Number(end) : NaN;
  const duration = selectedEvidence?.evidence.video?.duration_s ?? 0;
  const problem = record ? clipProblem(clipStart, clipEnd, duration, annotations) : null;
  async function download(bundle: EvidencePackage) {const blob = await protectedBlob(bundle.download_url, undefined, 110 * 1024 * 1024); const url = URL.createObjectURL(blob); const link = document.createElement("a"); link.href = url; link.download = `evidence-${bundle.package_id}.zip`; document.body.appendChild(link); link.click(); link.remove(); window.setTimeout(() => URL.revokeObjectURL(url), 1000); setNotice("Evidence ZIP downloaded. The manifest records file hashes and the case revision.");}
  return <main className="review-main package-builder"><header className="rv-page-head"><div><span className="rv-kicker">CASEWORK / EVIDENCE PACKAGE</span><h1>Keep the evidence together.</h1></div><span className="rv-badge rv-positive">Protected media only</span></header><p className="rv-muted">Select a recorded case, trim a short interval, and add factual observations. Export a ZIP with pixelated video, the case report, annotations and an integrity manifest.</p>
    {error && <p className="rv-error" role="alert">{error}</p>}{notice && <p className="rv-notice" role="status">{notice}</p>}{loading && <p className="rv-muted" role="status">Loading recorded cases…</p>}
    <div className="rv-case-grid"><section className="rv-editor-card"><div className="package-selection"><label>Recorded case<select value={selectedId} disabled={busy || notes.length > 0} onChange={event => {setSelectedId(event.target.value); setNotice(""); setError(null);}}><option value="">Choose a case</option>{cases.map(row => <option value={row.case_id} key={row.case_id}>{row.title}</option>)}</select></label>{record && <label>Recording evidence<select value={attachmentId} disabled={busy || notes.length > 0} onChange={event => setAttachmentId(event.target.value)}>{evidenceChoices.map(item => <option key={item.attachment_id} value={item.attachment_id}>{item.attachment_id === "original" ? "Original · " : "Attached · "}{item.title || item.evidence.video?.title || item.video_id} · {evidencePosition(item).toFixed(1)} s · {evidenceOriginLabel(item)}</option>)}</select></label>}{record && onOpenCase && <button className="rv-text-button" onClick={() => onOpenCase(record.case_id)}>Open case and full timeline</button>}</div>
      {!loading && !cases.length && <div className="rv-blank"><span>NO RECORDED CASES</span><h2>Start with recorded evidence.</h2><p>Open a case from a detected recording event or frozen human observation. Attach recorded evidence to an existing case, or use HTML/JSON export when it contains only platform alerts.</p></div>}
      {record && <><div className="rv-case-meta"><span>{record.case_id}</span><span>Case revision {record.revision}</span><span>{duration.toFixed(1)} s recording</span><span>Pinned analysis {selectedEvidence?.analysis_id}</span></div>{media ? <video className="package-video" ref={player} src={media} controls preload="metadata" playsInline onTimeUpdate={() => {const video = player.current; if (previewing && video && video.currentTime >= clipEnd) {video.pause(); setPreviewing(false);}}} /> : <p className={mediaError ? "rv-error" : "rv-media-placeholder"}>{mediaError || "Loading protected rendition…"}</p>}<p className="rv-small">Preview is the protected recording. The exported interval receives additional full-frame pixelation; audio and private originals are excluded.</p>
        <fieldset className="rv-fieldset" disabled={!canBuild || busy}><div className="rv-form-grid package-interval"><label>Clip start · seconds<input type="number" min="0" max={duration} step="0.1" value={start} onChange={event => {setStart(event.target.value); setPreviewing(false); player.current?.pause();}} /></label><label>Clip end · seconds<input type="number" min="0.1" max={duration} step="0.1" value={end} onChange={event => {setEnd(event.target.value); setPreviewing(false); player.current?.pause();}} /></label></div><div className="rv-row"><button className="rv-button" disabled={!media} onClick={() => setStart((player.current?.currentTime ?? 0).toFixed(1))}>Use playhead as start</button><button className="rv-button" disabled={!media} onClick={() => setEnd(Math.min(duration, player.current?.currentTime ?? 0).toFixed(1))}>Use playhead as end</button><button className="rv-button" disabled={!media || Boolean(problem)} onClick={() => {if (player.current) {player.current.currentTime = clipStart; setPreviewing(true); void player.current.play().catch(cause => {setPreviewing(false); setError(explainError(cause));});}}}>Preview interval</button></div>
        <h2>Factual annotations</h2><p className="rv-small">Record observable actions or context. These notes are attributed to the signed-in operator and are not independent findings.</p>{notes.map((note, index) => <div className="package-annotation" key={index}><label>Time · seconds<input type="number" min={clipStart || 0} max={clipEnd || duration} step="0.1" value={note.at_s} onChange={event => setNotes(rows => rows.map((row, i) => i === index ? {...row, at_s: event.target.value} : row))} /></label><label>Observation<textarea rows={2} maxLength={2000} value={note.text} onChange={event => setNotes(rows => rows.map((row, i) => i === index ? {...row, text: event.target.value} : row))} /></label><button className="rv-button" onClick={() => setNotes(rows => rows.filter((_, i) => i !== index))}>Remove</button></div>)}<div className="rv-row rv-form-actions"><button className="rv-button" disabled={notes.length >= 30} onClick={() => setNotes(rows => [...rows, {at_s: Number.isFinite(clipStart) ? String(Math.max(clipStart, Math.min(clipEnd, player.current?.currentTime ?? clipStart))) : "", text: ""}])}>Add annotation</button>{notes.length > 0 && <button className="rv-button" onClick={() => setNotes([])}>Clear draft annotations</button>}</div>
        {problem && <p className="rv-error">{problem}</p>}<div className="rv-form-actions"><button className="rv-button rv-primary" disabled={busy || Boolean(problem) || !media || !selectedEvidence} onClick={() => void run(async () => {const bundle = await api.request<EvidencePackage>("/evidence-packages", {method: "POST", body: JSON.stringify({case_id: record.case_id, attachment_id: selectedEvidence?.attachment_id, start_s: clipStart, end_s: clipEnd, annotations})}); setPackages(rows => [bundle, ...rows]); setNotes([]); setNotice("Package queued. Its case snapshot and authored annotations are fixed; follow the status on the right.");})}>{busy ? "Working…" : "Build evidence ZIP"}</button></div></fieldset></>}
    </section><aside><section className="rv-editor-card"><span className="rv-kicker">PACKAGE CONTENTS</span><h2>One interval. Exact context.</h2><ul className="commissioning-problems"><li>Pixelated MP4 clip, up to 60 seconds</li><li>Case HTML and JSON at the captured revision</li><li>Authored annotations with recording timestamps</li><li>Manifest with file sizes and SHA-256 hashes</li></ul><p className="rv-small">Hashes identify package bytes. They do not certify authenticity or replace chain-of-custody procedures.</p></section><section className="rv-editor-card"><div className="rv-section-title"><div><span className="rv-kicker">RECENT EXPORTS</span><h2>Evidence packages</h2></div><button className="rv-button" disabled={busy} onClick={() => void run(async () => setPackages((await api.request<{packages: EvidencePackage[]}>("/evidence-packages")).packages))}>Refresh</button></div><div className="package-list">{packages.filter(row => !selectedId || row.case_id === selectedId).map(bundle => <article id={`package-${bundle.package_id}`} className={`package-card ${bundle.package_id === initialPackageId ? "is-focused" : ""}`} key={bundle.package_id}>{bundle.package_id === initialPackageId && <p className="rv-kicker">OPENED FROM NOTIFICATION</p>}<header><strong>{bundle.case_title}</strong><span className={`rv-badge ${bundle.status === "ready" ? "rv-positive" : "rv-warning"}`}>{bundle.status}</span></header><p className="rv-small">{bundle.evidence_title && <>{bundle.evidence_title}<br /></>}{bundle.case_revision != null && <>Case revision {bundle.case_revision}<br /></>}{bundle.start_s.toFixed(1)}–{bundle.end_s.toFixed(1)} s · {bundle.annotations.length} annotations<br />{new Date(bundle.created_at).toLocaleString()} · {bundle.created_by}</p>{bundle.error && <p className="rv-error">{bundle.error}</p>}{bundle.status === "queued" && <p className="rv-small" role="status">Waiting for the local media processor.</p>}{bundle.status === "building" && <p className="rv-small" role="status">Trimming protected media and hashing package files…</p>}{bundle.status === "ready" && <><p className="rv-small">{((bundle.bytes ?? 0) / 1024 / 1024).toFixed(2)} MiB · {bundle.manifest?.files.length ?? 4} hashed files</p><code>{bundle.sha256}</code><div className="rv-row"><button className="rv-button rv-primary" disabled={busy} onClick={() => void run(() => download(bundle))}>Download ZIP</button></div></>}</article>)}{!packages.some(row => !selectedId || row.case_id === selectedId) && <p className="rv-empty">No packages for this case yet.</p>}</div></section></aside></div>
  </main>;
}
