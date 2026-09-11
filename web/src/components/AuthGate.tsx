import { useEffect, useState, type ReactNode } from "react";
import * as session from "../lib/session";
import { useSioStore } from "../store";
import "./auth.css";

export function AuthGate({ children }: { children: ReactNode }) {
  const current = session.useSession();
  const [config, setConfig] = useState<session.AuthConfig | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [role, setRole] = useState("operator");
  const [retry, setRetry] = useState(0);
  useEffect(() => {
    let cancelled = false;
    let timer: number | undefined;
    void session.getConfig().then(async (auth) => {
      if (cancelled) return;
      setConfig(auth);
      await session.initialize();
      if (!cancelled) setError("");
    }).catch((reason: unknown) => {
      if (cancelled) return;
      setError(reason instanceof Error ? reason.message : "Could not reach sign-in service.");
      if (!(reason instanceof session.SignInRequired)) timer = window.setTimeout(() => setRetry((n) => n + 1), Math.min(10_000, 1000 * 2 ** Math.min(retry, 4)));
    });
    return () => { cancelled = true; window.clearTimeout(timer); };
  }, [retry]);
  useEffect(() => {
    if (!current) { useSioStore.getState().reset(); return; }
    const timer = window.setInterval(() => {
      void session.ensure().catch((reason: unknown) => { if (reason instanceof session.SignInRequired) setError(reason.message); });
    }, 15_000);
    return () => window.clearInterval(timer);
  }, [current]);
  if (current) return children;
  return <main className="auth-screen"><section className="auth-card" aria-label="Sign in">
    <div className="auth-kicker"><span className="auth-mark">SIO</span> SPATIAL INTELLIGENCE OS</div>
    <p className="auth-eyebrow">SITE OPERATIONS / ACCESS</p>
    <h1>Enter your<br />operations console.</h1>
    <p className="auth-description">Monitor your site, investigate incidents and coordinate a response from one shared picture.</p>
    {config?.mode === "dev" ? <>
      <div className="auth-notice"><strong>Development sign-in</strong><p>This installation issues local test identities. Choose the role you want to exercise.</p></div>
      <label className="auth-role">Console role<select value={role} onChange={(event) => setRole(event.target.value)}>
        <option value="viewer">Viewer — observe</option><option value="operator">Operator — investigate and coordinate</option>
        <option value="commander">Commander — approve responses</option><option value="integrator">Integrator — configure sources</option>
        <option value="ml_engineer">ML engineer — inspect models and forecasts</option>
        <option value="admin">Administrator — development administration</option>
      </select></label>
    </> : <p className="auth-provider">{config ? "Sign in through your organisation’s identity provider." : "Connecting to the sign-in service…"}</p>}
    {error && <p className="auth-error" role="alert">{error}</p>}
    <button className="auth-submit" disabled={!config || busy} onClick={() => {
      setBusy(true); setError("");
      void session.signIn(role).catch((reason: unknown) => setError(reason instanceof Error ? reason.message : "Sign-in failed.")).finally(() => setBusy(false));
    }}>{busy ? "Signing in…" : config?.mode === "dev" ? "Open console" : "Sign in"}<span aria-hidden="true">↗</span></button>
    {!config && <button className="auth-retry" onClick={() => setRetry((n) => n + 1)}>Retry connection</button>}
    <div className="auth-footer">A clear picture. An accountable response.</div>
  </section><div className="auth-grid" aria-hidden="true"><i /><i /><i /><span>OBSERVE / UNDERSTAND / ACT</span></div></main>;
}

export function SessionControls() {
  const current = session.useSession();
  if (!current) return null;
  return <div className="session-controls">
    <span title={`${current.subject} · ${current.tenant} · expires ${new Date(current.expiresAt * 1000).toLocaleString()}`}>{current.mode === "dev" && <b>DEV</b>} {current.subject} <small>{current.roles.filter((role) => !role.startsWith("default-")).join(", ")}</small></span>
    <button onClick={() => { useSioStore.getState().reset(); void session.signOut(); }}>Sign out</button>
  </div>;
}
