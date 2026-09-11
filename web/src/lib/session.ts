/** Browser credentials stay in this tab and travel only in Authorization headers. */
import { useSyncExternalStore } from "react";

const STORAGE_KEY = "sio.session.v2";
const TRANSACTION_KEY = "sio.oidc.transaction";
const RENEW_MARGIN_S = 60;

export interface AuthConfig {
  mode: "dev" | "keycloak";
  required: boolean;
  oidc: null | {
    issuer: string;
    client_id: string;
    authorization_endpoint: string;
    token_endpoint: string;
    logout_endpoint?: string | null;
  };
}
export interface Session {
  token: string;
  subject: string;
  tenant: string;
  roles: string[];
  clearance: number;
  expiresAt: number;
  mode: "dev" | "keycloak";
}
interface StoredSession { token: string; refreshToken?: string; idToken?: string; mode: Session["mode"] }
interface TokenResponse { access_token: string; refresh_token?: string; id_token?: string }
interface Transaction { state: string; verifier: string; redirectUri: string; createdAt: number; issuer: string }

export class SignInRequired extends Error {
  constructor(message = "Your session has ended. Sign in to continue.") { super(message); this.name = "SignInRequired"; }
}

let current: Session | null = null;
let credentials: StoredSession | null = null;
let config: AuthConfig | null = null;
let configRequest: Promise<AuthConfig> | null = null;
let inflight: Promise<Session> | null = null;
let initialization: Promise<void> | null = null;
let generation = 0;
const listeners = new Set<() => void>();
const notify = () => listeners.forEach((listener) => listener());
/** Observe committed identity changes (sign-in, renewal and sign-out). */
export const subscribeSession = (listener: () => void) => { listeners.add(listener); return () => { listeners.delete(listener); }; };

export function useSession(): Session | null { return useSyncExternalStore(subscribeSession, peek, () => null); }
export function peek(): Session | null { return current; }

export function claimsOf(token: string): Record<string, unknown> {
  try {
    const segment = (token.split(".")[1] ?? "").replace(/-/g, "+").replace(/_/g, "/");
    const bytes = Uint8Array.from(atob(segment.padEnd(Math.ceil(segment.length / 4) * 4, "=")), (c) => c.charCodeAt(0));
    return JSON.parse(new TextDecoder().decode(bytes)) as Record<string, unknown>;
  } catch { return {}; }
}
function describe(stored: StoredSession): Session {
  const claims = claimsOf(stored.token);
  const realm = claims.realm_access as { roles?: unknown } | undefined;
  const flat = Array.isArray(claims.roles) ? claims.roles.map(String) : typeof claims.roles === "string" ? claims.roles.split(",") : [];
  const roles = [...new Set([...flat, ...(Array.isArray(realm?.roles) ? realm.roles.map(String) : [])].map((role) => role.trim().toLowerCase()))];
  return { token: stored.token, subject: String(claims.preferred_username ?? claims.sub ?? "unknown"),
    tenant: String(claims.tenant ?? claims.tenant_id ?? ""), roles,
    clearance: Number(claims.clearance ?? 0), expiresAt: Number(claims.exp ?? 0), mode: stored.mode };
}
function persist(stored: StoredSession): Session {
  const next = describe(stored);
  if (!next.expiresAt || next.expiresAt <= Date.now() / 1000) throw new SignInRequired("The identity provider returned an expired or invalid access token.");
  credentials = stored;
  current = next;
  window.sessionStorage.setItem(STORAGE_KEY, JSON.stringify(stored));
  notify();
  return next;
}
export async function getConfig(): Promise<AuthConfig> {
  if (config) return config;
  if (configRequest) return configRequest;
  configRequest = (async () => {
    const response = await fetch("/auth/config");
    if (!response.ok) throw new Error(`Sign-in service unavailable (HTTP ${response.status}). Retrying…`);
    const result = await response.json() as AuthConfig;
    if (!["dev", "keycloak"].includes(result.mode)) throw new Error("The API returned an unsupported authentication mode.");
    config = result;
    return result;
  })().finally(() => { configRequest = null; });
  return configRequest;
}
async function tokenRequest(endpoint: string, params: URLSearchParams): Promise<TokenResponse> {
  const response = await fetch(endpoint, { method: "POST", headers: { "Content-Type": "application/x-www-form-urlencoded" }, body: params });
  if (!response.ok) {
    if (response.status === 400 || response.status === 401) throw new SignInRequired();
    throw new Error(`Identity provider unavailable (HTTP ${response.status}).`);
  }
  return await response.json() as TokenResponse;
}
function base64url(bytes: Uint8Array): string { return btoa(String.fromCharCode(...bytes)).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, ""); }

export async function signIn(profile = "operator"): Promise<void> {
  const auth = await getConfig();
  if (auth.mode === "dev") {
    const allowed = ["viewer", "operator", "commander", "integrator", "ml_engineer", "admin"];
    if (!allowed.includes(profile)) throw new Error("Unknown development role.");
    const expected = generation;
    const params = new URLSearchParams({ subject: "console", roles: profile, clearance: profile === "admin" ? "3" : profile === "commander" ? "2" : "1" });
    const response = await fetch(`/auth/dev/token?${params}`, { method: "POST" });
    if (!response.ok) throw new Error(`Could not sign in (HTTP ${response.status}).`);
    const body = await response.json() as TokenResponse;
    if (expected !== generation) return;
    persist({ token: body.access_token, mode: "dev" });
    return;
  }
  if (!auth.oidc) throw new Error("The API has no identity-provider configuration.");
  const state = base64url(crypto.getRandomValues(new Uint8Array(32)));
  const verifier = base64url(crypto.getRandomValues(new Uint8Array(48)));
  const challenge = base64url(new Uint8Array(await crypto.subtle.digest("SHA-256", new TextEncoder().encode(verifier))));
  const redirectUri = `${window.location.origin}${window.location.pathname}`;
  window.sessionStorage.setItem(TRANSACTION_KEY, JSON.stringify({ state, verifier, redirectUri, createdAt: Date.now(), issuer: auth.oidc.issuer } satisfies Transaction));
  const target = new URL(auth.oidc.authorization_endpoint);
  target.search = new URLSearchParams({ client_id: auth.oidc.client_id, response_type: "code", scope: "openid profile", redirect_uri: redirectUri, state, code_challenge: challenge, code_challenge_method: "S256" }).toString();
  window.location.assign(target.toString());
}

async function initializeOnce(): Promise<void> {
  // Remove credentials left by the old browser client. The new reader needs no JS-written cookie.
  window.localStorage.removeItem("sio.token");
  document.cookie = "sio_token=; Path=/; Max-Age=0; SameSite=Strict";
  const auth = await getConfig();
  const url = new URL(window.location.href);
  const rawTransaction = window.sessionStorage.getItem(TRANSACTION_KEY);
  if (url.searchParams.has("code") || url.searchParams.has("error")) {
    const state = url.searchParams.get("state");
    const code = url.searchParams.get("code");
    const callbackError = url.searchParams.get("error");
    const transaction = rawTransaction ? JSON.parse(rawTransaction) as Transaction : null;
    window.sessionStorage.removeItem(TRANSACTION_KEY);
    for (const key of ["code", "state", "session_state", "iss", "error", "error_description"]) url.searchParams.delete(key);
    window.history.replaceState({}, "", url.toString());
    if (!transaction || transaction.state !== state || Date.now() - transaction.createdAt > 600_000 || transaction.issuer !== auth.oidc?.issuer) {
      throw new SignInRequired("The sign-in response could not be verified. Start sign-in again.");
    }
    if (callbackError || !code || !auth.oidc) throw new SignInRequired("Sign-in was cancelled or declined.");
    const expected = generation;
    const body = await tokenRequest(auth.oidc.token_endpoint, new URLSearchParams({ grant_type: "authorization_code", client_id: auth.oidc.client_id, code, code_verifier: transaction.verifier, redirect_uri: transaction.redirectUri }));
    if (expected === generation) persist({ token: body.access_token, refreshToken: body.refresh_token, idToken: body.id_token, mode: "keycloak" });
    return;
  }
  const raw = window.sessionStorage.getItem(STORAGE_KEY);
  if (!raw) return;
  try {
    const stored = JSON.parse(raw) as StoredSession;
    if (stored.mode !== auth.mode) { clear(); return; }
    credentials = stored;
    current = describe(stored);
    await ensure();
    notify();
  } catch (error) { clear(); if (!(error instanceof SignInRequired)) throw error; }
}
export async function initialize(): Promise<void> {
  if (!initialization) initialization = initializeOnce().finally(() => { initialization = null; });
  return initialization;
}

/** Reuse a session or renew it. Signing in is always an explicit operator action. */
export async function ensure(forceRefresh = false): Promise<Session> {
  if (current && !forceRefresh && current.expiresAt > Date.now() / 1000 + RENEW_MARGIN_S) return current;
  if (inflight) return inflight;
  if (!credentials || !current) throw new SignInRequired("Sign in to open the console.");
  const expected = generation;
  const existing = credentials;
  const identity = current;
  inflight = (async () => {
    const auth = await getConfig();
    let next: StoredSession;
    if (existing.mode === "dev") {
      const params = new URLSearchParams({ subject: identity.subject, roles: identity.roles.join(","), clearance: String(identity.clearance) });
      const response = await fetch(`/auth/dev/token?${params}`, { method: "POST" });
      if (!response.ok) { if (response.status === 401 || response.status === 403) throw new SignInRequired(); throw new Error(`Session renewal failed (HTTP ${response.status}).`); }
      const body = await response.json() as TokenResponse;
      next = { token: body.access_token, mode: "dev" };
    } else {
      if (!auth.oidc || !existing.refreshToken) throw new SignInRequired();
      const body = await tokenRequest(auth.oidc.token_endpoint, new URLSearchParams({ grant_type: "refresh_token", client_id: auth.oidc.client_id, refresh_token: existing.refreshToken }));
      next = { token: body.access_token, refreshToken: body.refresh_token ?? existing.refreshToken, idToken: body.id_token ?? existing.idToken, mode: "keycloak" };
    }
    if (expected !== generation) throw new SignInRequired();
    return persist(next);
  })().catch((error: unknown) => {
    if (expected === generation && (error instanceof SignInRequired || (current?.expiresAt ?? 0) <= Date.now() / 1000)) clear();
    throw error;
  }).finally(() => { if (expected === generation) inflight = null; });
  return inflight;
}
export function headers(): Record<string, string> { return current ? { Authorization: `Bearer ${current.token}` } : {}; }
export function clear(): void {
  generation += 1;
  current = null;
  credentials = null;
  inflight = null;
  window.sessionStorage.removeItem(STORAGE_KEY);
  window.sessionStorage.removeItem(TRANSACTION_KEY);
  window.localStorage.removeItem("sio.token");
  document.cookie = "sio_token=; Path=/; Max-Age=0; SameSite=Strict";
  notify();
}
export async function signOut(): Promise<void> {
  const idToken = credentials?.idToken;
  const mode = current?.mode;
  clear();
  if (mode !== "keycloak" || !config?.oidc?.logout_endpoint) return;
  const target = new URL(config.oidc.logout_endpoint);
  target.searchParams.set("client_id", config.oidc.client_id);
  target.searchParams.set("post_logout_redirect_uri", `${window.location.origin}${window.location.pathname}`);
  if (idToken) target.searchParams.set("id_token_hint", idToken);
  window.location.assign(target.toString());
}

/** Presentation hints only. Server policy also checks tenant, zone, clearance and current policy. */
export function can(action: string): boolean {
  if (!current || current.expiresAt <= Date.now() / 1000) return false;
  if (current.roles.includes("admin")) return true;
  if (action === "notifications.write") return true;
  const rules: Record<string, string[]> = {
    "storage.read": ["integrator"], "storage.write": [],
    "review.write": ["operator", "commander", "integrator", "ml_engineer"],
    "case.write": ["operator", "commander"], "site.write": ["integrator"],
    "alerts.write": ["operator", "commander"], "decision.approve": ["commander"], "decision.reject": ["operator", "commander"],
    "decisions.write": ["operator", "commander", "service"], "workflow.execute": ["commander"], "workflow.write": ["commander", "service"],
    "mission.read": ["viewer", "operator", "analyst", "commander"],
    "mission.write": ["operator", "commander"], "mission.assign": ["commander"],
    "integration.write": ["integrator"], "integration.read": ["integrator", "commander"],
    "copilot.ask": ["operator", "commander", "ml_engineer", "integrator", "service"],
    "timeline.write": ["operator", "commander", "ml_engineer", "integrator", "service"],
    "forecasts.write": ["operator", "commander", "ml_engineer", "service"], "events.write": ["operator", "commander", "ml_engineer", "service"],
    "simulation.write": ["viewer", "operator", "commander", "ml_engineer", "integrator", "service"], "model.write": ["ml_engineer"],
  };
  if (["decision.approve", "workflow.execute"].includes(action) && current.clearance < 2) return false;
  const roles = rules[action];
  return roles ? roles.some((role) => current!.roles.includes(role)) : action.endsWith(".read");
}
