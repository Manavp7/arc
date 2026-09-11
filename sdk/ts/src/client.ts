/** Shared HTTP client for the console and integrations.
 * Query shapes come from OpenAPI; serialized response contracts live in contracts.ts.
 * Credentials are supplied by an identity provider, or minted for development callers. */

import { readSse } from "./sse.ts";
import type { paths } from "./generated/api.d.ts";

import type { Alert, Decision, Entity, SioEvent } from "./contracts.ts";
export type { Alert, Decision, Entity };
export type Event = SioEvent;
/** Query parameters for an endpoint, taken from the generated schema rather than restated. */
type Query<P extends keyof paths> = paths[P] extends { get: { parameters: { query?: infer Q } } }
  ? Q
  : never;

export interface SioClientOptions {
  url?: string;
  /** A token from your identity provider. Omit in dev and one is minted. */
  token?: string;
  /** Supply/renew credentials externally; never fall back to the dev issuer in this mode. */
  tokenProvider?: (forceRefresh: boolean) => Promise<string>;
  subject?: string;
  roles?: string[];
  clearance?: number;
  fetch?: typeof fetch;
}

/**
 * An error that keeps the API's own explanation.
 *
 * The API returns a reason for every denial — "decision.approve needs one of: admin, commander; you have
 * operator" — and a client that throws `Error("403")` discards the only useful part. A caller can then show
 * something actionable instead of a status code.
 */
export class SioApiError extends Error {
  constructor(
    readonly status: number,
    readonly detail: string | Record<string, unknown>,
    readonly url: string,
    readonly action?: string,
    readonly rule?: string,
  ) {
    super(typeof detail === "string" ? detail : String(detail.message ?? `HTTP ${status}`));
    this.name = "SioApiError";
  }

  get isPermissionError(): boolean {
    return this.status === 403;
  }

  get isAuthError(): boolean {
    return this.status === 401;
  }
}

/** How long before expiry a token is renewed, so a request never begins with one that dies mid-flight. */
const RENEW_MARGIN_MS = 120_000;

export class SioClient {
  private readonly url: string;
  private readonly fetchImpl: typeof fetch;
  private token: string;
  private expiresAt: number;
  private minting: Promise<string> | null = null;

  constructor(private readonly options: SioClientOptions = {}) {
    this.url = (options.url ?? "http://127.0.0.1:8000").replace(/\/$/, "");
    this.fetchImpl = options.fetch ?? globalThis.fetch.bind(globalThis);
    this.token = options.token ?? "";
    // A token handed in has its own renewal story; the SDK must not second-guess it.
    this.expiresAt = options.token ? Number.POSITIVE_INFINITY : 0;
  }

  /**
   * Obtain a token, reusing a valid one.
   *
   * The in-flight promise is shared, so five concurrent requests on a cold client mint **one** token rather than
   * five — which is wasteful and makes the audit trail read as five separate sign-ins.
   */
  async authenticate(forceRefresh = false): Promise<string> {
    if (this.options.tokenProvider) return this.options.tokenProvider(forceRefresh);
    if (this.options.token) return this.options.token;
    if (forceRefresh) { this.token = ""; this.expiresAt = 0; }
    if (this.token && Date.now() < this.expiresAt - RENEW_MARGIN_MS) return this.token;
    if (this.minting) return this.minting;

    this.minting = (async () => {
      const params = new URLSearchParams({
        subject: this.options.subject ?? "sdk",
        roles: (this.options.roles ?? ["operator"]).join(","),
        clearance: String(this.options.clearance ?? 1),
      });
      const response = await this.fetchImpl(`${this.url}/auth/dev/token?${params}`, {
        method: "POST",
      });
      if (!response.ok) {
        throw new SioApiError(
          response.status,
          "could not obtain a token. In a Keycloak deployment the dev issuer is disabled by design — " +
            "pass { token } instead.",
          `${this.url}/auth/dev/token`,
        );
      }
      const body = (await response.json()) as { access_token: string };
      this.token = body.access_token;
      this.expiresAt = claimsOf(body.access_token).exp * 1000 || Date.now() + 3_600_000;
      return this.token;
    })().finally(() => {
      this.minting = null;
    });

    return this.minting;
  }

  /**
   * One request, authenticated, retried once on a 401.
   *
   * Once, not in a loop: a misconfigured secret would otherwise become a request storm against the token
   * endpoint. The browser console's first version did exactly that.
   */
  async request<T>(
    method: string,
    path: string,
    init: { query?: Record<string, unknown>; body?: unknown; raw?: RequestInit } = {},
  ): Promise<T> {
    for (const attempt of [1, 2]) {
      const query = new URLSearchParams();
      for (const [key, value] of Object.entries(init.query ?? {})) {
        if (value !== undefined && value !== null) query.set(key, String(value));
      }
      const suffix = query.toString() ? `?${query}` : "";
      const response = await this.fetchImpl(`${this.url}${path}${suffix}`, {
        ...init.raw,
        method,
        headers: new Headers({
          ...(init.body === undefined ? {} : { "content-type": "application/json" }),
          ...Object.fromEntries(new Headers(init.raw?.headers)),
          Authorization: `Bearer ${await this.authenticate(attempt > 1)}`,
        }),
        body: init.body === undefined ? init.raw?.body : JSON.stringify(init.body),
      });

      if (response.status === 401 && attempt === 1) {
        if (this.options.token && !this.options.tokenProvider) throw await errorFrom(response);
        continue;
      }
      if (!response.ok) throw await errorFrom(response);
      if (response.status === 204) return undefined as T;
      return (await response.json()) as T;
    }
    throw new Error("unreachable");
  }

  /**
   * Entities on site.
   *
   * Defaults to the last five minutes and excludes static fixtures, which together mean "things that are here
   * and moving". This platform deletes nothing, so an unfiltered query legitimately returns every entity that has
   * ever existed — and a client whose default is all of history produces a first experience of confusing volume
   * with no clue that the default was the problem. Pass `active_within_s: null` for the full record.
   */
  async entities(query: Query<"/api/entities"> = {}): Promise<Entity[]> {
    return this.request<Entity[]>("GET", "/api/entities", {
      query: { active_within_s: 300, include_static: false, limit: 100, ...query },
    });
  }

  async entity(entityId: string): Promise<Entity> {
    return this.request<Entity>("GET", `/api/entities/${encodeURIComponent(entityId)}`);
  }

  async events(query: Query<"/api/events"> = {}): Promise<Event[]> {
    return this.request<Event[]>("GET", "/api/events", { query: { limit: 50, ...query } });
  }

  async alerts(query: { state?: string; limit?: number } = {}): Promise<Alert[]> {
    const payload = await this.request<{ alerts: Alert[] }>("GET", "/api/alerts", {
      query: { limit: 50, grouped: false, ...query },
    });
    return payload.alerts ?? [];
  }

  async zones(): Promise<unknown[]> {
    return this.request<unknown[]>("GET", "/api/spatial/zones");
  }

  async decisions(query: { approval?: string; limit?: number } = {}): Promise<Decision[]> {
    const payload = await this.request<{ decisions: Decision[] }>("GET", "/api/decisions", {
      query: { approval: "pending", limit: 20, ...query },
    });
    return payload.decisions ?? [];
  }

  /**
   * Approve a recommendation, which is what authorises the platform to act.
   *
   * Governed by the same policy as the console: `decision.approve` needs a commander. A client built with the
   * default `["operator"]` gets a 403 with the reason, which is the correct outcome — an SDK is another caller,
   * not a back door.
   */
  async approve(decisionId: string, optionId?: string): Promise<unknown> {
    return this.request("POST", `/api/decisions/${encodeURIComponent(decisionId)}/approve`, {
      body: { option_id: optionId, approved_by: this.options.subject ?? "sdk" },
    });
  }

  /** Ask the copilot. Slow by nature: a local model takes seconds, not milliseconds. */
  async ask(question: string): Promise<CopilotAnswer> {
    const payload = await this.request<Record<string, unknown>>("POST", "/api/copilot/ask", {
      body: { question },
    });
    const trace = (payload.trace ?? {}) as { tools_used?: string[] };
    return {
      question,
      text: String(payload.answer ?? ""),
      confidence: Number(payload.confidence ?? 0),
      toolsUsed: trace.tools_used ?? [],
      redaction: (payload.redaction as string | null) ?? null,
      explanation: (payload.explanation ?? {}) as Record<string, unknown>,
    };
  }

  /** Project a what-if. Changes nothing on the live site. */
  async simulate(scenario: string, params: Record<string, unknown> = {}): Promise<unknown> {
    return this.request("POST", "/api/simulations", { body: { scenario, params } });
  }

  async analytics(hours = 24): Promise<unknown> {
    return this.request("GET", "/api/analytics/summary", { query: { hours } });
  }

  async forecasts(): Promise<unknown> {
    return this.request("GET", "/api/forecasts/latest");
  }

  /**
   * Live messages, as an async iterable.
   *
   *     for await (const message of sio.subscribe("events", "alerts")) {
   *       console.log(message.kind, message.payload);
   *     }
   *
   * Uses the same bearer-authenticated SSE parser as the browser console.
   * Malformed JSON frames are skipped; interrupted connections retry with bounded backoff.
   */
  async *subscribe(
    ...topics: string[]
  ): AsyncGenerator<StreamMessage, void, undefined> {
    const query = topics.length ? `?topics=${encodeURIComponent(topics.join(","))}` : "";
    let backoff = 500;
    let forceRefresh = false;

    for (;;) {
      try {
        const response = await this.fetchImpl(`${this.url}/stream${query}`, {
          headers: { Authorization: `Bearer ${await this.authenticate(forceRefresh)}` },
        });
        if (!response.ok || !response.body) throw await errorFrom(response);

        backoff = 500;
        forceRefresh = false;
        for await (const frame of readSse(response.body)) {
          try {
            const payload = JSON.parse(frame.data) as Record<string, unknown>;
            yield {
              kind: String(payload.kind ?? frame.event ?? "unknown"),
              payload: (payload.payload ?? payload) as Record<string, unknown>,
              raw: payload,
            };
          } catch { /* One malformed frame must not tear down the stream. */ }
        }

      } catch (error) {
        if (error instanceof SioApiError) {
          if (error.isPermissionError || (error.isAuthError && (this.options.token || forceRefresh))) throw error;
          if (error.isAuthError) forceRefresh = true;
        }
      }

      // Reconnect with backoff. A stream that gives up on the first network blip is one every caller has to
      // wrap in its own retry loop, which is the SDK failing to do its job.
      await new Promise((resolve) => setTimeout(resolve, backoff));
      backoff = Math.min(10_000, backoff * 2);
    }
  }
}

export interface StreamMessage {
  kind: string;
  payload: Record<string, unknown>;
  raw: Record<string, unknown>;
}

export interface CopilotAnswer {
  question: string;
  text: string;
  confidence: number;
  toolsUsed: string[];
  redaction: string | null;
  explanation: Record<string, unknown>;
}

async function errorFrom(response: Response): Promise<SioApiError> {
  let detail: string | Record<string, unknown> = response.statusText;
  let action: string | undefined;
  let rule: string | undefined;
  try {
    const body = (await response.json()) as Record<string, unknown>;
    if (typeof body.detail === "string" || (body.detail && typeof body.detail === "object")) detail = body.detail as string | Record<string, unknown>;
    if (typeof body.action === "string") action = body.action;
    if (typeof body.rule === "string") rule = body.rule;
  } catch {
    // A non-JSON error body is still an error; the status is the useful part.
  }
  return new SioApiError(response.status, detail, response.url, action, rule);
}

/**
 * Read a JWT's claims **without verifying it**.
 *
 * Safe, and worth being explicit about: the client is reading its own freshly issued token to learn when to
 * renew. It is not making a trust decision — the server verifies, always. A client that verified its own token
 * would be checking its own homework.
 */
function claimsOf(token: string): { exp: number } {
  try {
    const segment = token.split(".")[1] ?? "";
    const normalised = segment.replace(/-/g, "+").replace(/_/g, "/");
    const padded = normalised + "=".repeat((4 - (normalised.length % 4)) % 4);
    return JSON.parse(atob(padded)) as { exp: number };
  } catch {
    return { exp: 0 };
  }
}
