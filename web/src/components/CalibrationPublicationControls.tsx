import { useEffect, useRef, useState } from "react";
import { explainError } from "../lib/api";
import { calibrationApi, calibrationPreviewUsable, type CalibrationPreview, type CalibrationPublication } from "../lib/calibration-publication";
import "./source-activation.css";

export function CalibrationPublicationControls({setupId, revision, disabled = false, onBusyChange}: {setupId: string; revision: number; disabled?: boolean; onBusyChange?: (busy: boolean) => void}) {
  const [status,setStatus] = useState<CalibrationPublication | null>(null);
  const [preview,setPreview] = useState<CalibrationPreview | null>(null);
  const [busy,setBusy] = useState(false), [error,setError] = useState("");
  const [now,setNow] = useState(Date.now());
  const request = useRef<AbortController | null>(null);
  useEffect(() => {onBusyChange?.(busy); return () => onBusyChange?.(false);}, [busy,onBusyChange]);
  useEffect(() => {
    const controller = new AbortController(); request.current = controller;
    setStatus(null);setPreview(null);setError("");setBusy(false);
    void calibrationApi.status(setupId,controller.signal).then(value => {if (!controller.signal.aborted) setStatus(value);}).catch(cause => {if (!controller.signal.aborted) setError(explainError(cause));});
    return () => controller.abort();
  }, [setupId,revision]);
  useEffect(() => {if (disabled) setPreview(null);}, [disabled]);
  useEffect(() => {
    if (!preview) return;
    const timer = window.setInterval(() => setNow(Date.now()),1000);
    return () => window.clearInterval(timer);
  }, [preview]);
  useEffect(() => {
    if (status?.status !== "pending_fusion") return;
    const controller = new AbortController();
    const timer = window.setInterval(() => {void calibrationApi.status(setupId,controller.signal).then(value => {if (!controller.signal.aborted) setStatus(value);}).catch(cause => {if (!controller.signal.aborted) setError(explainError(cause));});},5000);
    return () => {controller.abort();window.clearInterval(timer);};
  }, [setupId,status?.status]);
  async function refresh() {
    const signal = request.current?.signal; if (!signal || signal.aborted) return;
    try {const value = await calibrationApi.status(setupId,signal);if (!signal.aborted) {setStatus(value);setError("");}} catch(cause) {if (!signal.aborted) setError(explainError(cause));}
  }
  async function review(rollback = false) {
    const signal = request.current?.signal; if (!signal || signal.aborted || disabled || busy) return;
    setBusy(true);setError("");setPreview(null);
    try {const value = await calibrationApi.preview(setupId,revision,rollback,signal);if (!signal.aborted) {setPreview(value);setNow(Date.now());}}
    catch(cause) {if (!signal.aborted) setError(explainError(cause));}
    finally {if (!signal.aborted) setBusy(false);}
  }
  async function apply() {
    const signal = request.current?.signal;
    if (!signal || signal.aborted || disabled || busy || !preview || !calibrationPreviewUsable(preview,setupId,revision)) return;
    setBusy(true);setError("");
    try {const value = await calibrationApi.apply(preview,signal);if (!signal.aborted) {setStatus(value);setPreview(null);}}
    catch(cause) {if (!signal.aborted) {setError(explainError(cause));setPreview(null);try {const value=await calibrationApi.status(setupId,signal);if (!signal.aborted) setStatus(value);} catch { /* Retain the action failure; refresh remains available. */ }}}
    finally {if (!signal.aborted) setBusy(false);}
  }
  const usable = preview && calibrationPreviewUsable(preview,setupId,revision,now);
  return <section className="source-activation" aria-label="Publish camera calibration">
    <div className="source-actions"><button disabled={disabled || busy} onClick={() => void review()}>Preview calibration publication</button>{status?.can_rollback && <button disabled={disabled || busy} onClick={() => void review(true)}>Preview calibration rollback</button>}<button disabled={busy} onClick={() => void refresh()}>Refresh calibration status</button></div>
    {busy && <p role="status">Processing reviewed calibration…</p>}{error && <p className="panel-error" role="alert">{error}</p>}
    {status && <div><p className="feed-note"><strong>{status.status.replaceAll("_"," ")}</strong>{status.calibration_revision != null && ` · calibration revision ${status.calibration_revision}`}{status.applied_by && ` · ${status.applied_by}`}</p><p className="feed-note">{status.note}</p>{status.publication_id && <p className="feed-note">Fusion {status.fusion.acknowledged ? `acknowledged this revision${status.fusion.acknowledged_at ? ` at ${new Date(status.fusion.acknowledged_at).toLocaleString()}` : ""}` : "has not acknowledged this revision yet"}.</p>}</div>}
    {preview && <div className="source-activation-review"><header><strong>{preview.operation === "rollback" ? "Review calibration rollback" : "Review measured pose publication"}</strong><span>Expires {new Date(preview.expires_at).toLocaleTimeString()}</span></header><p className="feed-note">Source {preview.source_id} · saved setup revision {preview.setup_revision}</p><div className="source-activation-diff"><div><h4>Current calibration</h4><pre>{preview.previous_pose ? JSON.stringify(preview.previous_pose,null,2) : "No published calibration"}</pre></div><div><h4>Proposed calibration</h4><pre>{preview.proposed_pose ? JSON.stringify(preview.proposed_pose,null,2) : "Remove published calibration"}</pre></div></div><p className="feed-note">{preview.note}</p>{!usable && <p className="panel-error">This preview expired or its setup changed. Request a new preview.</p>}<div className="source-actions"><button className="primary" disabled={disabled || busy || !usable} onClick={() => void apply()}>{preview.operation === "rollback" ? "Apply calibration rollback" : "Publish measured calibration"}</button><button disabled={busy} onClick={() => setPreview(null)}>Cancel preview</button></div></div>}
    <p className="feed-note">Publication updates the projection used by fusion after acknowledgement. Checkpoint agreement and fusion acknowledgement do not independently verify the survey, terrain or real camera accuracy.</p>
  </section>;
}
