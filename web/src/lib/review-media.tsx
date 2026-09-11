import { useEffect, useState } from "react";
import { explainError } from "./api";
import { protectedBlob } from "./review-api";
export function useProtectedMedia(path: string | null | undefined, kind: "video" | "image") {
  const [state, setState] = useState<{ url: string | null; error: string | null; loading: boolean }>({ url: null, error: null, loading: false });
  useEffect(() => {
    if (!path) { setState({ url: null, error: null, loading: false }); return; }
    const controller = new AbortController(); let url: string | null = null;
    setState({ url: null, error: null, loading: true });
    void protectedBlob(path, controller.signal, kind === "image" ? 12 * 1024 * 1024 : undefined).then(blob => {
      if (controller.signal.aborted) return;
      if (!blob.type.startsWith(`${kind}/`)) throw new Error(`The protected file is not a ${kind}.`);
      url = URL.createObjectURL(blob); setState({ url, error: null, loading: false });
    }).catch(cause => { if (!controller.signal.aborted) setState({ url: null, error: explainError(cause), loading: false }); });
    return () => { controller.abort(); if (url) URL.revokeObjectURL(url); };
  }, [path, kind]);
  return state;
}
export function ReviewImage({ path, alt }: { path?: string | null; alt: string }) {
  const media = useProtectedMedia(path, "image");
  if (!path) return null;
  return media.url ? <img className="rv-evidence-image" src={media.url} alt={alt} /> : <p className={media.error ? "rv-error" : "rv-muted"}>{media.error ?? "Loading protected evidence…"}</p>;
}
