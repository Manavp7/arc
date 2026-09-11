import { createContext, useCallback, useContext, useEffect, useState, type ReactNode } from "react";
import { api, explainError } from "../lib/api";

interface ServiceStatus {
  service: string; status: string; checks: Record<string, string>; info: Record<string, string>;
  pending: number | null; errors: number | null; action: string | null;
}
export interface SystemStatus {
  checked_at: string; status: string; services: ServiceStatus[]; adapters: Record<string, string>;
  modes: { actions: string; actuators: string; data: string; copilot: string; semantic_search: boolean | null; authentication: string };
}
const Operations = createContext<{ status: SystemStatus | null; error: string | null; refresh: () => void }>({ status: null, error: null, refresh: () => {} });

export function OperationsProvider({ children }: { children: ReactNode }) {
  const [status, setStatus] = useState<SystemStatus | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [revision, setRevision] = useState(0);
  const refresh = useCallback(() => setRevision(value => value + 1), []);
  useEffect(() => {
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout>;
    const load = async () => {
      try {
        const next = await api.request<SystemStatus>("/system", { signal: controller.signal });
        if (!controller.signal.aborted) { setStatus(next); setError(null); }
      } catch (cause) {
        if (!controller.signal.aborted) setError(explainError(cause));
      } finally {
        if (!controller.signal.aborted) timer = setTimeout(() => void load(), 15000);
      }
    };
    void load();
    return () => { controller.abort(); clearTimeout(timer); };
  }, [revision]);
  return <Operations.Provider value={{ status, error, refresh }}>{children}</Operations.Provider>;
}

export function OperatingModes() {
  const { status, error } = useContext(Operations);
  const modes = !error ? status?.modes : undefined;
  const dataLabels: Record<string, string> = { simulated: "Simulated data", real: "Real sources", mixed: "Mixed sources", inspect_sources: "Check source modes", unknown: "Data mode unknown" };
  return <div className="operating-modes" aria-label="Operating modes">
    <span className="mode-tag">{dataLabels[modes?.data ?? "unknown"] ?? modes?.data}</span>
    <span className={`mode-tag ${modes?.actions === "armed" ? "mode-warning" : ""}`} title={modes?.actuators}>
      {modes?.actions === "dry_run" ? "Dry-run actions" : modes?.actions === "armed" ? "Armed · no physical actuators" : "Action mode unknown"}
    </span>
    <span className="mode-tag">{modes?.copilot === "scripted" ? "Scripted copilot" : modes?.copilot === "keyword_routing" ? "Keyword routing · model unavailable" : !modes || modes.copilot === "unknown" ? "Copilot mode unknown" : `${modes.copilot} copilot`}</span>
  </div>;
}

export function SystemPanel() {
  const { status, error, refresh } = useContext(Operations);
  const issues = status?.services.filter(row => row.status !== "ok") ?? [];
  return <div className="panel-body operations-panel">
    <header className="section-heading"><div><span className="eyebrow">Administration / health</span><h2>System status</h2></div><button className="small-button" onClick={refresh}>Refresh</button></header>
    <p className="section-intro">Connection checks, processing backlog, and failures across the running platform.</p>
    {error && <p className="panel-error" role="alert">Status could not be refreshed. {error}</p>}
    {!status && !error && <p className="panel-empty">Checking services…</p>}
    {status && <>
      <div className="health-summary"><strong>{issues.length === 0 ? "All services responding" : `${issues.length} services need attention`}</strong><span>{error ? "Last successful check" : "Checked"} {new Date(status.checked_at).toLocaleTimeString()}</span></div>
      <div className="service-list">{status.services.map(row => <article key={row.service} className={`service-card service-${row.status}`}>
        <header><strong>{row.service}</strong><span className="mode-tag">{row.status}</span></header>
        <p className="service-counters">{row.pending == null ? "Backlog unavailable" : `${row.pending} pending`} · {row.errors == null ? "Errors unavailable" : `${row.errors} processing errors`}</p>
        {Object.entries(row.checks).filter(([, value]) => !value.toLowerCase().startsWith("ok")).map(([key, value]) => <p className="service-problem" key={key}>{key}: {value}</p>)}
        {row.action && <p className="service-action">{row.action}</p>}
      </article>)}</div>
      <details className="adapter-details"><summary>Active runtime choices</summary><dl>{Object.entries(status.adapters).map(([name, value]) => <div key={name}><dt>{name}</dt><dd>{value}</dd></div>)}</dl></details>
      <p className="feed-note">{status.modes.actuators} {status.modes.semantic_search === null ? "Semantic search status is unavailable." : status.modes.semantic_search ? "" : "Hash embeddings are active; semantic similarity is unavailable."}</p>
    </>}
  </div>;
}
