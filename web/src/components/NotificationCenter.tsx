import { useCallback, useEffect, useId, useRef, useState } from "react";
import { explainError } from "../lib/api";
import { notificationApi, notificationCount, notificationTime, unreadSnapshot, type NotificationPage, type NotificationTarget, type PersonalNotification } from "../lib/notifications";
import { can, useSession } from "../lib/session";
import "./notifications.css";

export function NotificationCenter({ onOpenTarget }: { onOpenTarget: (target: NotificationTarget) => void }) {
  const identity = useSession();
  const key = identity ? `${identity.tenant}:${identity.subject}:${identity.token}` : "";
  const id = useId();
  const button = useRef<HTMLButtonElement>(null);
  const panel = useRef<HTMLElement>(null);
  const request = useRef<AbortController | null>(null);
  const action = useRef<AbortController | null>(null);
  const [open, setOpen] = useState(false);
  const [unreadOnly, setUnreadOnly] = useState(true);
  const [page, setPage] = useState<NotificationPage | null>(null);
  const [pageIdentity, setPageIdentity] = useState("");
  const [before, setBefore] = useState<string | null>(null);
  const [cursors, setCursors] = useState<(string | null)[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [busy, setBusy] = useState(false);
  const [refresh, setRefresh] = useState(0);
  const close = useCallback(() => { setOpen(false); button.current?.focus(); }, []);
  const enabled = Boolean(identity && can("notifications.read"));

  useEffect(() => {
    action.current?.abort(); setBusy(false); setPage(null); setBefore(null); setCursors([]); setError(null); setOpen(false);
    return () => { action.current?.abort(); request.current?.abort(); };
  }, [key]);

  useEffect(() => {
    if (!enabled) return;
    let alive = true;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const load = async () => {
      request.current?.abort();
      const controller = new AbortController(); request.current = controller;
      setLoading(true);
      try {
        const result = await notificationApi.list(unreadOnly, before, controller.signal);
        if (alive && !controller.signal.aborted) { setPage(result); setPageIdentity(key); setError(null); }
      } catch (cause) { if (alive && !controller.signal.aborted) { setPage(null); setError(explainError(cause)); } }
      finally {
        if (alive && !controller.signal.aborted) setLoading(false);
        if (alive) timer = setTimeout(() => { void load(); }, open ? 15000 : 30000);
      }
    };
    void load();
    return () => { alive = false; clearTimeout(timer); request.current?.abort(); };
  }, [key, enabled, unreadOnly, before, open, refresh]);

  useEffect(() => {
    if (!open) return;
    const onKey = (event: KeyboardEvent) => { if (event.key === "Escape") { event.preventDefault(); close(); } };
    const outside = (event: PointerEvent) => { if (!panel.current?.contains(event.target as Node) && !button.current?.contains(event.target as Node)) setOpen(false); };
    document.addEventListener("keydown", onKey); document.addEventListener("pointerdown", outside);
    panel.current?.querySelector<HTMLButtonElement>("button")?.focus();
    return () => { document.removeEventListener("keydown", onKey); document.removeEventListener("pointerdown", outside); };
  }, [open, close]);

  async function mark(records: PersonalNotification[]) {
    if (busy || !unreadSnapshot(records).length) return;
    const controller = new AbortController(); action.current = controller;
    setBusy(true); setError(null);
    try {
      await notificationApi.mark(records, controller.signal);
      if (!controller.signal.aborted) setRefresh(value => value + 1);
    } catch (cause) { if (!controller.signal.aborted) { setError(explainError(cause)); setRefresh(value => value + 1); } }
    finally { if (!controller.signal.aborted) setBusy(false); }
  }
  async function openTarget(record: PersonalNotification) {
    if (busy) return;
    const controller = new AbortController(); action.current = controller;
    setBusy(true); setError(null);
    try {
      // Resolve again immediately before navigation: ownership and zone access may have changed.
      const result = await notificationApi.target(record.notification_id, controller.signal);
      if (controller.signal.aborted) return;
      onOpenTarget(result.target); close();
    } catch (cause) { if (!controller.signal.aborted) { setError(explainError(cause)); setRefresh(value => value + 1); } }
    finally { if (!controller.signal.aborted) setBusy(false); }
  }
  if (!enabled) return null;
  const currentPage = pageIdentity === key ? page : null;
  const visible = currentPage?.notifications ?? [];
  const unread = currentPage?.unread_count ?? 0;
  return <div className="notification-center">
    <button ref={button} className={`notification-bell ${open ? "is-open" : ""}`} type="button" aria-label={`Notifications, ${currentPage ? notificationCount(unread, currentPage.counts_complete) : "loading"} unread`} aria-haspopup="dialog" aria-controls={id} aria-expanded={open} onClick={() => setOpen(value => !value)} title="Notifications">
      <svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" aria-hidden="true"><path d="M18 8a6 6 0 0 0-12 0c0 7-3 7-3 9h18c0-2-3-2-3-9" /><path d="M10 21h4" /></svg>
      {(unread > 0 || currentPage?.counts_complete === false) && <span className="notification-badge">{notificationCount(unread, currentPage?.counts_complete ?? true)}</span>}
    </button>
    {open && <section ref={panel} id={id} className="notification-panel" role="dialog" aria-modal="false" aria-labelledby={`${id}-title`}>
      <header className="notification-heading"><div><span>PERSONAL ACTIVITY</span><h2 id={`${id}-title`}>Notifications</h2></div><button type="button" onClick={close} aria-label="Close notifications">×</button></header>
      <div className="notification-controls"><div role="group" aria-label="Notification filter"><button type="button" aria-pressed={unreadOnly} onClick={() => { setUnreadOnly(true); setBefore(null); setCursors([]); }}>Unread</button><button type="button" aria-pressed={!unreadOnly} onClick={() => { setUnreadOnly(false); setBefore(null); setCursors([]); }}>All</button></div><button type="button" disabled={busy || loading || !unreadSnapshot(visible).length} onClick={() => void mark(visible)}>Mark visible read</button></div>
      {error && <p className="notification-error" role="alert">{error}</p>}
      {currentPage?.counts_complete === false && <p className="notification-bound">Showing a bounded history. The unread count may be incomplete.</p>}
      <div className="notification-items" aria-busy={loading}>
        {!currentPage && loading ? <p className="notification-empty">Loading your activity…</p> : visible.length === 0 ? <p className="notification-empty">{unreadOnly ? "No unread notifications in this view." : "No notifications in this view."}<small>Assignments, deadlines, analysis and export results appear here.</small></p> : visible.map(record => <article key={record.notification_id} className={`notification-item ${record.read_at ? "is-read" : "is-unread"} severity-${record.severity}`}>
          <div className="notification-item-meta"><span>{record.read_at ? "Read" : "Unread"}</span><time dateTime={record.created_at} title={new Date(record.created_at).toLocaleString()}>{notificationTime(record.created_at)}</time></div>
          <button type="button" className="notification-open" disabled={busy} onClick={() => void openTarget(record)}><strong>{record.title}</strong><span>{record.message}</span></button>
          {!record.read_at && <button type="button" className="notification-mark" disabled={busy} onClick={() => void mark([record])}>Mark read</button>}
        </article>)}
      </div>
      <footer className="notification-footer"><span>In app · checks every 30s</span><div><button type="button" disabled={loading || cursors.length === 0} onClick={() => { setBefore(cursors.at(-1) ?? null); setCursors(values => values.slice(0, -1)); }}>Newer</button><button type="button" disabled={loading || !currentPage?.has_more} onClick={() => { setCursors(values => [...values, before]); setBefore(currentPage?.next_cursor ?? null); }}>Older</button><button type="button" disabled={loading} onClick={() => setRefresh(value => value + 1)} aria-label="Refresh notifications">↻</button></div></footer>
    </section>}
  </div>;
}
