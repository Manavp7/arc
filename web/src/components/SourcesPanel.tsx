import { useCallback, useEffect, useState, type FormEvent } from "react";
import { api, explainError } from "../lib/api";
import * as session from "../lib/session";

interface Source {
  source_id: string; kind: string; modality: string; label: string | null; enabled: boolean;
  options: Record<string, unknown>; rate_hz: number; status: string; data_mode: string; restart_required: boolean;
  last_success: string | null; error: string | null;
  last_observation: Record<string, unknown> | null;
}
interface SourceList { sources: Source[]; registered_kinds: string[]; restart_required: boolean; note?: string }
interface Probe { ok: boolean; tested_at: string; sample: Record<string, unknown> | null; message: string }

export function SourcesPanel() {
  const [data, setData] = useState<SourceList | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [sourceId, setSourceId] = useState("");
  const [label, setLabel] = useState("");
  const [kind, setKind] = useState("camera_rtsp");
  const [modality, setModality] = useState("video");
  const [options, setOptions] = useState("{}");
  const [cameraUrl, setCameraUrl] = useState("rtsp://");
  const [rate, setRate] = useState(1);
  const [enabled, setEnabled] = useState(true);
  const [editing, setEditing] = useState(false);
  const [sample, setSample] = useState<{ id: string; value: unknown } | null>(null);
  const canEdit = session.can("integration.write");
  const load = useCallback(async () => {
    try { setData(await api.request<SourceList>("/sources")); setError(null); }
    catch (cause) { setError(explainError(cause)); }
  }, []);
  useEffect(() => { void load(); const timer = setInterval(() => void load(), 10000); return () => clearInterval(timer); }, [load]);

  async function save(event: FormEvent) {
    event.preventDefault(); setBusy("save"); setError(null); setMessage(null);
    try {
      const parsed: unknown = JSON.parse(options);
      if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) throw new Error("Options must be a JSON object.");
      await api.request(`/sources/${encodeURIComponent(sourceId)}`, { method: "PUT", body: JSON.stringify({ kind, modality, label, enabled, rate_hz: rate, options: kind === "camera_rtsp" ? { ...parsed, url: cameraUrl } : parsed }) });
      setEditing(false); setMessage("Configuration saved. Restart ingestion to apply it; active sources keep their current configuration until then."); await load();
    } catch (cause) { setError(explainError(cause)); } finally { setBusy(null); }
  }
  async function test(source: Source) {
    setBusy(source.source_id); setError(null); setMessage(null);
    try {
      const result = await api.request<Probe>(`/sources/${encodeURIComponent(source.source_id)}/test`, { method: "POST", body: "{}" });
      setMessage(`${source.label || source.source_id}: ${result.message}`);
      setSample({ id: source.source_id, value: result.sample });
      await load();
      if (!result.ok) setError("Connection test did not produce a valid observation. Check the source address, credentials, and optional connector dependencies.");
    } catch (cause) { setError(explainError(cause)); } finally { setBusy(null); }
  }
  async function toggle(source: Source) {
    setBusy(source.source_id);
    try {
      await api.request(`/sources/${encodeURIComponent(source.source_id)}/enabled`, { method: "POST", body: JSON.stringify({ enabled: !source.enabled }) });
      setMessage("Change saved. Restart ingestion to apply the new enabled state."); await load();
    } catch (cause) { setError(explainError(cause)); } finally { setBusy(null); }
  }
  return <div className="panel-body operations-panel">
    <header className="section-heading"><div><span className="eyebrow">Administration / inputs</span><h2>Sources</h2></div><button className="small-button" onClick={() => void load()}>Refresh</button></header>
    <p className="section-intro">Connect a camera or sensor, inspect its latest observation, and check whether data is arriving.</p>
    {data?.restart_required && <p className="mode-notice">Saved changes are waiting for an ingestion restart. Current connections have not changed.</p>}
    {error && <p className="panel-error" role="alert">{error}</p>}
    {message && <p className="panel-flash" role="status">{message}</p>}
    {!data && !error && <p className="panel-empty">Loading sources…</p>}
    <div className="source-list">{data?.sources.map(source => <article className="source-card" key={source.source_id}>
      <header><div><strong>{source.label || source.source_id}</strong><span className="source-id">{source.source_id} · {source.kind}</span></div><span className={`mode-tag ${source.status === "error" ? "mode-warning" : ""}`}>{source.status.replace(/_/g, " ")}</span></header>
      <p><span className="mode-tag">{source.data_mode === "simulated" ? "Simulated" : "Real source"}</span> <span className="source-last">{source.last_success ? `Last observation ${new Date(source.last_success).toLocaleTimeString()}` : "No successful observation recorded"}</span></p>
      {source.error && <p className="service-problem">{source.error}</p>}
      <div className="source-actions">
        <button onClick={() => setSample({ id: source.source_id, value: source.last_observation })}>Latest sample</button>
        {canEdit && <><button disabled={busy !== null} onClick={() => void test(source)}>{busy === source.source_id ? "Working…" : "Test connection"}</button>
        <button onClick={() => { setSourceId(source.source_id); setLabel(source.label ?? ""); setKind(source.kind); setModality(source.modality); setOptions(JSON.stringify(source.kind === "camera_rtsp" ? Object.fromEntries(Object.entries(source.options).filter(([key]) => key !== "url")) : source.options, null, 2)); setCameraUrl(String(source.options.url ?? "rtsp://")); setRate(source.rate_hz); setEnabled(source.enabled); setEditing(true); }}>Configure</button>
        <button disabled={busy !== null} onClick={() => void toggle(source)}>{source.enabled ? "Disable" : "Enable"}</button></>}
      </div>
    </article>)}</div>
    {sample && <section className="sample-view"><header><strong>Observation · {sample.id}</strong><button aria-label="Close sample" onClick={() => setSample(null)}>×</button></header><pre>{sample.value ? JSON.stringify(sample.value, null, 2) : "No observation is available yet."}</pre><p className="feed-note">Connection tests read one sample without publishing it to the live world.</p></section>}
    {canEdit && !editing && <button className="primary wide-button" onClick={() => { setSourceId(""); setLabel(""); setKind("camera_rtsp"); setModality("video"); setOptions("{}"); setCameraUrl("rtsp://"); setRate(1); setEnabled(true); setEditing(true); }}>Add a source</button>}
    {!canEdit && <p className="feed-note">Source configuration requires an integrator or administrator role.</p>}
    {editing && <form className="source-form form-panel" onSubmit={event => void save(event)}>
      <h3>Source configuration</h3>
      <label>Source ID<input required pattern="[A-Za-z0-9_.-]+" value={sourceId} onChange={event => setSourceId(event.target.value)} placeholder="north-gate-camera" /></label>
      <label>Display name<input value={label} onChange={event => setLabel(event.target.value)} placeholder="North gate" /></label>
      <div className="form-columns"><label>Connector<select value={kind} onChange={event => setKind(event.target.value)}>{[...new Set([kind, ...(data?.registered_kinds ?? [])])].map(value => <option key={value}>{value}</option>)}</select></label><label>Modality<select value={modality} onChange={event => setModality(event.target.value)}>{["video", "gps", "iot", "weather", "satellite", "enterprise", "audio"].map(value => <option key={value}>{value}</option>)}</select></label></div>
      {kind === "camera_rtsp" && <label>Camera address<input required value={cameraUrl} onChange={event => setCameraUrl(event.target.value)} placeholder="rtsp://camera-address/stream" autoComplete="off" /></label>}
      <label>Sampling rate (observations / second)<input required type="number" min="0.01" max="120" step="any" value={rate} onChange={event => setRate(Number(event.target.value))} /></label>
      <label>Additional connection options<textarea rows={5} spellCheck={false} value={options} onChange={event => setOptions(event.target.value)} /></label>
      <p className="feed-note">{kind === "camera_rtsp" ? "Enter the RTSP stream in Camera address. " : "Use the connector's documented options. "}Existing masked credentials are preserved when unchanged. Saved credentials stay on the server.</p>
      <label className="checkbox-label"><input type="checkbox" checked={enabled} onChange={event => setEnabled(event.target.checked)} /> Enabled after restart</label>
      <div className="source-actions"><button className="primary" disabled={busy !== null} type="submit">{busy === "save" ? "Saving…" : "Save configuration"}</button><button type="button" onClick={() => setEditing(false)}>Cancel</button></div>
    </form>}
  </div>;
}
