/**
 * Live update stream (Server-Sent Events).
 *
 * SSE rather than WebSockets for the default path: the flow is one-directional (server →
 * console), it survives proxies, and the browser reconnects on its own. The API also exposes a
 * WebSocket for future bidirectional use.
 */

import type { StreamMessage } from "../types";

export type StreamHandler = (message: StreamMessage) => void;
export type StatusHandler = (status: "connecting" | "live" | "reconnecting" | "closed") => void;

export interface StreamOptions {
  topics?: string[];
  onMessage: StreamHandler;
  onStatus?: StatusHandler;
}

export function connectStream({ topics, onMessage, onStatus }: StreamOptions): () => void {
  let source: EventSource | null = null;
  let closed = false;
  let attempt = 0;
  let timer: number | undefined;

  const open = () => {
    if (closed) return;
    const query = topics?.length ? `?topics=${encodeURIComponent(topics.join(","))}` : "";
    onStatus?.(attempt === 0 ? "connecting" : "reconnecting");
    source = new EventSource(`/stream${query}`);

    source.onopen = () => {
      attempt = 0;
      onStatus?.("live");
    };

    source.onmessage = (event) => {
      try {
        onMessage(JSON.parse(event.data) as StreamMessage);
      } catch (error) {
        // One malformed frame must not tear down the live picture.
        console.warn("stream: unparseable message", error);
      }
    };

    source.onerror = () => {
      source?.close();
      source = null;
      if (closed) return;
      // Explicit backoff instead of the browser's built-in retry: the API may be restarting
      // under `just dev`, and hammering it every second makes the logs unreadable.
      attempt += 1;
      const delay = Math.min(10_000, 500 * 2 ** Math.min(attempt, 5));
      onStatus?.("reconnecting");
      timer = window.setTimeout(open, delay);
    };
  };

  open();

  return () => {
    closed = true;
    if (timer) window.clearTimeout(timer);
    source?.close();
    onStatus?.("closed");
  };
}
