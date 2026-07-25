/**
 * The console's session: one token, used by both transports.
 *
 * Phase 5 made a principal mandatory on every endpoint, which broke the console completely — every fetch
 * 401'd and the live map went blank. Two problems had to be solved, and only one of them is obvious.
 *
 * **`fetch` is easy**: attach an `Authorization` header.
 *
 * **`EventSource` cannot send headers.** The API is specified by browsers with no way to set them, so the
 * SSE feed the entire live map depends on cannot carry a bearer token. The options are a token in the query
 * string, a cookie, or abandoning `EventSource` for a `fetch`-based stream reader.
 *
 * A **cookie** is the choice here, and the query string is rejected for a specific reason rather than on
 * principle: URLs end up in access logs, in `Referer` headers and in browser history, so a token in a query
 * string is a credential written to three places nobody is auditing. The cookie is `SameSite=Strict` and
 * scoped to the app's own origin, and the middleware verifies it exactly as it verifies a header — same
 * signature check, same expiry, same algorithm assertion.
 *
 * **The dev issuer is the only source of tokens here.** In a Keycloak deployment the console redirects to
 * the identity provider and this module is replaced by an OIDC client; the shape of what it exposes —
 * `headers()`, `ensure()` — is what the rest of the app depends on, so that swap does not reach into the
 * panels.
 */

const TOKEN_KEY = "sio.token";
const COOKIE_NAME = "sio_token";

/** Renew this long before expiry, so a request never starts with a token that dies mid-flight. */
const RENEW_MARGIN_S = 120;

export interface Session {
  token: string;
  subject: string;
  tenant: string;
  roles: string[];
  expiresAt: number;
}

let current: Session | null = null;
let inflight: Promise<Session> | null = null;

function decodeExpiry(token: string): number {
  try {
    const body = token.split(".")[1];
    if (!body) return 0;
    const json = JSON.parse(atob(body.replace(/-/g, "+").replace(/_/g, "/")));
    return typeof json.exp === "number" ? json.exp : 0;
  } catch {
    return 0;
  }
}

function setCookie(token: string, expiresAt: number): void {
  // SameSite=Strict: this cookie is only ever sent by the console to its own origin, so there is no
  // cross-site request it should accompany. Not `Secure`, because the dev console runs on plain http —
  // a production deployment terminates TLS and should add it.
  const maxAge = Math.max(60, expiresAt - Math.floor(Date.now() / 1000));
  document.cookie = `${COOKIE_NAME}=${token}; Path=/; Max-Age=${maxAge}; SameSite=Strict`;
}

/**
 * Obtain a session, reusing a valid one.
 *
 * Concurrent callers share one request. Without that, the console's first render fires five parallel loads
 * and mints five tokens — harmless but wasteful, and it makes the audit trail read as five sign-ins.
 */
export async function ensure(): Promise<Session> {
  const now = Math.floor(Date.now() / 1000);
  if (current && current.expiresAt - RENEW_MARGIN_S > now) return current;
  if (inflight) return inflight;

  inflight = (async () => {
    const stored = window.localStorage.getItem(TOKEN_KEY);
    if (stored) {
      const expiresAt = decodeExpiry(stored);
      if (expiresAt - RENEW_MARGIN_S > now) {
        const session = describe(stored, expiresAt);
        current = session;
        setCookie(stored, expiresAt);
        return session;
      }
    }

    // The dev issuer. A Keycloak deployment replaces this module with an OIDC client.
    const response = await fetch(
      "/auth/dev/token?subject=console&roles=operator,commander&clearance=2",
      { method: "POST" },
    );
    if (!response.ok) {
      throw new Error(
        `could not obtain a token (HTTP ${response.status}). ` +
          "Is the API running, and is SIO_AUTH_MODE=dev?",
      );
    }
    const body = (await response.json()) as { access_token: string };
    const expiresAt = decodeExpiry(body.access_token);
    window.localStorage.setItem(TOKEN_KEY, body.access_token);
    setCookie(body.access_token, expiresAt);
    const session = describe(body.access_token, expiresAt);
    current = session;
    return session;
  })().finally(() => {
    inflight = null;
  });

  return inflight;
}

function describe(token: string, expiresAt: number): Session {
  try {
    const body = token.split(".")[1] ?? "";
    const claims = JSON.parse(atob(body.replace(/-/g, "+").replace(/_/g, "/")));
    return {
      token,
      subject: String(claims.sub ?? "unknown"),
      tenant: String(claims.tenant ?? ""),
      roles: Array.isArray(claims.roles) ? claims.roles.map(String) : [],
      expiresAt,
    };
  } catch {
    return { token, subject: "unknown", tenant: "", roles: [], expiresAt };
  }
}

/** Authorization header for `fetch`. Empty when there is no session yet. */
export function headers(): Record<string, string> {
  return current ? { Authorization: `Bearer ${current.token}` } : {};
}

/** The current session without minting one, for UI that wants to show who it is acting as. */
export function peek(): Session | null {
  return current;
}

/**
 * Forget the session.
 *
 * Clears both stores, because clearing one and not the other leaves a cookie that authenticates SSE while
 * `fetch` behaves as though signed out — a split state that presents as "the map updates but nothing else
 * works", which is a genuinely confusing bug to be handed.
 */
export function clear(): void {
  current = null;
  window.localStorage.removeItem(TOKEN_KEY);
  document.cookie = `${COOKIE_NAME}=; Path=/; Max-Age=0; SameSite=Strict`;
}
