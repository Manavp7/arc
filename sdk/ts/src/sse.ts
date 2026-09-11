/** SSE framing shared by browser and SDK; handles named, multiline and chunk-split frames. */
export interface SseFrame { event: string; data: string; id?: string }
export interface SseReadOptions { signal?: AbortSignal; onActivity?: () => void }
export async function* readSse(body: ReadableStream<Uint8Array>, options: SseReadOptions = {}): AsyncGenerator<SseFrame> {
  const reader = body.getReader();
  const cancel = () => { void reader.cancel(options.signal?.reason).catch(() => undefined); };
  options.signal?.addEventListener("abort", cancel, { once: true });
  if (options.signal?.aborted) cancel();
  const decoder = new TextDecoder();
  let buffer = "";
  let event = "message";
  let id: string | undefined;
  let data: string[] = [];
  try {
    for (;;) {
      const result = await reader.read();
      if (!result.done && result.value.byteLength) options.onActivity?.();
      if (options.signal?.aborted) break;
      buffer += result.done ? decoder.decode() : decoder.decode(result.value, { stream: true });
      let newline: number;
      while ((newline = buffer.indexOf("\n")) !== -1) {
        const line = buffer.slice(0, newline).replace(/\r$/, "");
        buffer = buffer.slice(newline + 1);
        if (!line) {
          if (data.length) yield { event, data: data.join("\n"), id };
          event = "message";
          data = [];
          continue;
        }
        if (line.startsWith(":")) continue;
        const colon = line.indexOf(":");
        const field = colon === -1 ? line : line.slice(0, colon);
        const value = colon === -1 ? "" : line.slice(colon + 1).replace(/^ /, "");
        if (field === "event") event = value || "message";
        else if (field === "data") data.push(value);
        else if (field === "id" && !value.includes("\0")) id = value;
      }
      if (result.done) break;
    }
  } finally {
    options.signal?.removeEventListener("abort", cancel);
    await reader.cancel().catch(() => undefined);
    reader.releaseLock();
  }
}
