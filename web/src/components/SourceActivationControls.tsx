import { useEffect, useRef, useState } from "react";
import { api, explainError } from "../lib/api";
import { activationAction, activationEndpoint, activationProblem, type SourceActivationPreview, type SourceActivationResult } from "../lib/source-activation";
import "./source-activation.css";

/** Preview is a short-lived server ticket, never a editable client configuration. */
export function SourceActivationControls({sourceId, disabled = false, onApplied}: {sourceId: string; disabled?: boolean; onApplied?: () => void | Promise<void>}) {
  const [preview, setPreview] = useState<SourceActivationPreview | null>(null);
  const [result, setResult] = useState<SourceActivationResult | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const controller = useRef<AbortController | null>(null);
  useEffect(() => {
    const request = new AbortController(); controller.current = request;
    setPreview(null); setResult(null); setError(null); setBusy(false);
    void api.request<SourceActivationResult>(activationEndpoint(sourceId, "status"), {signal: request.signal}).then(value => {if (!request.signal.aborted) setResult(value);}).catch(cause => {if (!request.signal.aborted) setError(explainError(cause));});
    return () => request.abort();
  }, [sourceId]);
  async function review(rollback = false) {
    const request = controller.current; if (!request || request.signal.aborted) return;
    setBusy(true); setError(null); setPreview(null);
    try {
      const value = await api.request<SourceActivationPreview>(activationEndpoint(sourceId, rollback ? "rollback-preview" : "preview"), {method: "POST", body: "{}", signal: request.signal});
      if (!request.signal.aborted) setPreview(value);
    } catch (cause) {if (!request.signal.aborted) setError(explainError(cause));}
    finally {if (!request.signal.aborted) setBusy(false);}
  }
  async function apply() {
    const request = controller.current; if (!preview || !request || request.signal.aborted) return;
    setBusy(true); setError(null);
    try {
      const value = await api.request<SourceActivationResult>(activationEndpoint(sourceId, preview.action), {method: "POST", body: JSON.stringify({preview_id: preview.preview_id, timeout_s: 20}), signal: request.signal});
      if (!request.signal.aborted) {setResult(value); setPreview(null); await onApplied?.();}
    } catch (cause) {
      if (!request.signal.aborted) {
        setError(explainError(cause)); setPreview(null);
        // A lost response does not mean the server abandoned the operation.
        try {const value = await api.request<SourceActivationResult>(activationEndpoint(sourceId, "status"), {signal: request.signal}); if (!request.signal.aborted) setResult(value);} catch { /* The visible error retains the original failure. */ }
      }
    } finally {if (!request.signal.aborted) setBusy(false);}
  }
  return <section className="source-activation" aria-label={`Activation for ${sourceId}`}>
    <div className="source-actions"><button disabled={disabled || busy} onClick={() => void review()}>Preview activation</button>{result?.rollback_available && <button disabled={disabled || busy} onClick={() => void review(true)}>Preview rollback</button>}</div>
    {busy && <p className="feed-note" role="status">{preview ? "Applying this source and checking incoming data… Automatic recovery can take up to a minute." : "Loading reviewed configuration…"}</p>}
    {error && <p className="panel-error" role="alert">{error}</p>}
    {result?.message && <p className={activationProblem(result) ? "panel-error" : "feed-note"} role={activationProblem(result) ? "alert" : "status"}>{result.message}{result.verified_at ? ` · Fresh sample received ${new Date(result.verified_at).toLocaleTimeString()}` : ""}</p>}
    {preview && <div className="source-activation-review"><header><strong>{preview.action === "rollback" ? "Review source rollback" : "Review saved source changes"}</strong><span>Expires {new Date(preview.expires_at).toLocaleTimeString()}</span></header><p className="feed-note">Changed: {preview.changed_fields.length ? preview.changed_fields.join(", ") : "saved configuration matches the running configuration; activation verifies a new connection"}.</p><div className="source-activation-diff"><div><h4>Currently running</h4><pre>{preview.before ? JSON.stringify(preview.before, null, 2) : "No running source"}</pre></div><div><h4>After this action</h4><pre>{JSON.stringify(preview.after, null, 2)}</pre></div></div><ul>{preview.warnings.map(warning => <li key={warning}>{warning}</li>)}</ul><div className="source-actions"><button className="primary" disabled={disabled || busy} onClick={() => void apply()}>{activationAction(preview)}</button><button disabled={busy} onClick={() => setPreview(null)}>Cancel preview</button></div></div>}
  </section>;
}
