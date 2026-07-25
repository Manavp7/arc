/**
 * Live update stream (Server-Sent Events).
 *
 * SSE rather than WebSockets for the default path: the flow is one-directional (server →
 * console), it survives proxies, and the browser reconnects on its own. The API also exposes a
 * WebSocket for future bidirectional use.
 */

import * as session from "./session";
import type { StreamMessage } from "../types";

export type StreamHandler = (message: StreamMessage) => void;
export type StatusHandler = (status: "connecting" | "live" | "reconnecting" | "closed") => void;

export interface StreamOptions {
  topics?: string[];
  onMessage: StreamHandler;
  onStatus?: StatusHandler;
}

/**
 * Payload kinds the API emits as *named* SSE events (`event: Entity`).
 *
 * This list is not decoration. `EventSource.onmessage` fires only for frames with **no** event
 * name, so a named frame is silently dropped unless a listener is registered for that exact name.
 * That failed invisibly: the connection opened, the badge said "live", 790 frames per run arrived
 * on the wire, and the map stayed frozen on its initial snapshot with nothing in the console.
 *
 * Named events are worth keeping — they let a consumer subscribe to one kind — so the fix is to
 * register a listener per kind, plus `onmessage` for anything unnamed.
 */
const MESSAGE_KINDS = [
  "Entity",
  "Event",
  "Alert",
  "Decision",
  "Forecast",
  "Track",
  "Mission",
  "WorkflowRun",
  "Relationship",
] as const;

export function connectStream({ topics, onMessage, onStatus }: StreamOptions): () => void {
  let source: EventSource | null = null;
  let closed = false;
  let attempt = 0;
  let timer: number | undefined;

  const open = async () => {
    if (closed) return;
    // The session cookie must exist before the EventSource opens.
    //
    // `EventSource` cannot send an Authorization header — the browser API provides no way to set one — so
    // the SSE feed authenticates by cookie, which `session.ensure()` writes. Opening the stream first
    // would 401, and since EventSource reconnects automatically that would become a silent retry loop with
    // a permanently blank map.
    try {
      await session.ensure();
    } catch (error) {
      onStatus?.("closed");
      console.warn("stream: could not obtain a session", error);
      return;
    }
    if (closed) return;
    const query = topics?.length ? `?topics=${encodeURIComponent(topics.join(","))}` : "";
    onStatus?.(attempt === 0 ? "connecting" : "reconnecting");
    source = new EventSource(`/stream${query}`);

    source.onopen = () => {
      attempt = 0;
      onStatus?.("live");
    };

    const handle = (event: MessageEvent<string>) => {
      try {
        onMessage(JSON.parse(event.data) as StreamMessage);
      } catch (error) {
        // One malformed frame must not tear down the live picture.
        console.warn("stream: unparseable message", error);
      }
    };

    // Unnamed frames…
    source.onmessage = handle;
    // …and every named kind the API emits.
    for (const kind of MESSAGE_KINDS) {
      source.addEventListener(kind, handle as EventListener);
    }

    source.onerror = () => {
      source?.close();
      source = null;
      if (closed) return;
      // Explicit backoff instead of the browser's built-in retry: the API may be restarting
      // under `just dev`, and hammering it every second makes the logs unreadable.
      attempt += 1;
      const delay = Math.min(10_000, 500 * 2 ** Math.min(attempt, 5));
      onStatus?.("reconnecting");
      timer = window.setTimeout(() => void open(), delay);
    };
  };

  void open();

  return () => {
    closed = true;
    if (timer) window.clearTimeout(timer);
    source?.close();
    onStatus?.("closed");
  };
}
