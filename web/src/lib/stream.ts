import * as session from "./session";
import { readSse } from "../../../sdk/ts/src/sse";
import type { StreamMessage } from "../types";
export { readSse };
export type StreamHandler = (message: StreamMessage) => void;
export type StatusHandler = (status: "connecting" | "live" | "reconnecting" | "closed") => void;
export interface StreamOptions {
  topics?: string[];
  onMessage: StreamHandler;
  onStatus?: StatusHandler;
  /** Reload the REST snapshot after connection/reconnection to fill gaps while disconnected. */
  onConnected?: (reconnected: boolean) => void;
  /** Transport activity includes heartbeat comments, independently of application messages. */
  onActivity?: (receivedAt: string) => void;
  inactivityTimeoutMs?: number;
}

export function connectStream({ topics, onMessage, onStatus, onConnected, onActivity, inactivityTimeoutMs = 45_000 }: StreamOptions): () => void {
  let closed = false;
  let attempt = 0;
  let connected = false;
  let timer: number | undefined;
  let renewalTimer: number | undefined;
  let inactivityTimer: number | undefined;
  let controller: AbortController | null = null;
  const open = async () => {
    if (closed) return;
    onStatus?.(attempt || connected ? "reconnecting" : "connecting");
    controller = new AbortController();
    const active = controller;
    let announced = false;
    const armInactivity = () => {
      window.clearTimeout(inactivityTimer);
      if (closed || active.signal.aborted) return;
      inactivityTimer = window.setTimeout(() => {
        if (closed) return;
        onStatus?.("reconnecting");
        active.abort();
      }, inactivityTimeoutMs);
    };
    const activity = () => {
      if (closed || active.signal.aborted) return;
      armInactivity();
      onActivity?.(new Date().toISOString());
      if (!announced) {
        announced = true;
        attempt = 0;
        onStatus?.("live");
        onConnected?.(connected);
        connected = true;
      }
    };
    try {
      let identity = await session.ensure();
      if (closed) return;
      armInactivity();
      const query = topics?.length ? `?topics=${encodeURIComponent(topics.join(","))}` : "";
      let response = await fetch(`/stream${query}`, { headers: session.headers(), signal: active.signal });
      if (response.status === 401) {
        identity = await session.ensure(true);
        response = await fetch(`/stream${query}`, { headers: session.headers(), signal: active.signal });
      }
      if (response.status === 401 || response.status === 403) {
        session.clear();
        throw new session.SignInRequired("Live updates were refused. Sign in again.");
      }
      if (!response.ok || !response.body) throw new Error(`Live stream unavailable (${response.status}).`);
      if (closed) { active.abort(); return; }
      if (!response.headers.get("Content-Type")?.toLowerCase().startsWith("text/event-stream")) {
        await response.body.cancel();
        throw new Error("Live endpoint did not return an event stream.");
      }
      renewalTimer = window.setTimeout(() => active.abort(), Math.max(1000, (identity.expiresAt - Date.now() / 1000 - 45) * 1000));
      for await (const frame of readSse(response.body, { signal: active.signal, onActivity: activity })) {
        if (closed) break;
        try { onMessage(JSON.parse(frame.data) as StreamMessage); }
        catch (error) { console.warn("stream: unparseable message", error); }
      }
    } catch (error) {
      if (error instanceof session.SignInRequired) { onStatus?.("closed"); return; }
    } finally {
      window.clearTimeout(renewalTimer);
      window.clearTimeout(inactivityTimer);
      active.abort();
    }
    if (closed) return;
    attempt += 1;
    onStatus?.("reconnecting");
    timer = window.setTimeout(() => void open(), Math.min(10_000, 500 * 2 ** Math.min(attempt, 5)));
  };
  void open();
  return () => {
    closed = true;
    window.clearTimeout(timer);
    window.clearTimeout(renewalTimer);
    window.clearTimeout(inactivityTimer);
    controller?.abort();
    onStatus?.("closed");
  };
}
