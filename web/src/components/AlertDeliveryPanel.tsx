import { useCallback, useEffect, useRef, useState } from "react";
import { alertDeliveryApi, canRetryDelivery, deliveryTime, mergeDeliveryHistory, type AlertDelivery, type DeliveryHistory, type AlertDeliveryList, type DeliveryStatus } from "../lib/alert-delivery";
import { explainError } from "../lib/api";
import { useSession } from "../lib/session";
import "./AlertDeliveryPanel.css";

function DeliveryHistoryView({delivery}: {delivery: AlertDelivery}) {
  const [older, setOlder] = useState<DeliveryHistory[]>(delivery.history);
  useEffect(() => {setOlder(rows => mergeDeliveryHistory(delivery.history, rows));}, [delivery.history]);
  const [cursor, setCursor] = useState<string | null | undefined>();
  const [busy, setBusy] = useState(false), [error,setError] = useState("");
  const request = useRef<AbortController | null>(null);
  useEffect(() => () => request.current?.abort(), []);
  const next = cursor === undefined ? delivery.history_next_cursor : cursor;
  async function loadOlder() {
    if (!next || busy) return;
    const controller = new AbortController(); request.current = controller; setBusy(true); setError("");
    try { const page = await alertDeliveryApi.history(delivery.delivery_id,next,controller.signal); if (!controller.signal.aborted) { setOlder(rows => mergeDeliveryHistory(rows,page.history)); setCursor(page.next_cursor); } }
    catch(cause) { if (!controller.signal.aborted) setError(explainError(cause)); }
    finally { if (!controller.signal.aborted) setBusy(false); }
  }
  const rows = mergeDeliveryHistory(delivery.history,older);
  return <details><summary>Delivery history · {rows.length} loaded</summary><code className="delivery-id">{delivery.delivery_id}</code>
    <ol className="delivery-history">{rows.map((item,index) => <li key={item.history_id ?? `${item.at}-${index}`}><strong>{item.kind.replaceAll("_", " ")}</strong> · {deliveryTime(item.at)}{item.attempt > 0 && ` · attempt ${item.attempt}`}{item.actor && <span> by {item.actor}</span>}{item.reason && <p>{item.reason}</p>}{item.error && <p>{item.error}</p>}</li>)}</ol>
    {error && <p className="delivery-error" role="alert">{error}</p>}{next && <button disabled={busy} onClick={() => void loadOlder()}>{busy ? "Loading history…" : "Load older attempts"}</button>}
  </details>;
}

export function AlertDeliveryPanel() {
  const identity = useSession();
  const [data, setData] = useState<AlertDeliveryList | null>(null);
  const [pageCursor,setPageCursor] = useState<string | undefined>();
  const [previous,setPrevious] = useState<(string | undefined)[]>([]);
  const [alertDraft,setAlertDraft] = useState(""), [alertId,setAlertId] = useState("");
  const [filter, setFilter] = useState<DeliveryStatus | "">("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [retryId, setRetryId] = useState<string | null>(null);
  const [reason, setReason] = useState("");
  const [notice, setNotice] = useState<string | null>(null);
  const generation = useRef(0);
  const requestId = useRef(0);
  const pending = useRef<AbortController | null>(null);
  const identityKey = `${identity?.tenant ?? ""}:${identity?.subject ?? ""}:${identity?.roles.join(",") ?? ""}`;

  const load = useCallback(async (signal?: AbortSignal) => {
    const token = ++requestId.current;
    const expected = generation.current;
    try {
      const result = await alertDeliveryApi.list(filter, signal, {cursor:pageCursor,alert_id:alertId});
      if (signal?.aborted || token !== requestId.current || expected !== generation.current) return;
      setData(result); setError(null);
    } catch (cause) {
      if (!signal?.aborted && token === requestId.current && expected === generation.current) setError(explainError(cause));
    }
  }, [filter,pageCursor,alertId]);

  useEffect(() => {
    const controller = new AbortController();
    generation.current += 1;
    pending.current?.abort();
    setData(null); setError(null); setNotice(null); setRetryId(null); setReason(""); setBusy(false);
    if (identityKey !== "::") void load(controller.signal);
    const timer = window.setInterval(() => {
      if (identityKey !== "::") void load(controller.signal);
    }, 10_000);
    return () => {
      generation.current += 1; controller.abort(); pending.current?.abort(); window.clearInterval(timer);
    };
  }, [identityKey, load]);

  const retry = async (event: React.FormEvent) => {
    event.preventDefault();
    if (!retryId || reason.trim().length < 3 || busy) return;
    const expected = generation.current;
    const controller = new AbortController(); pending.current = controller;
    setBusy(true); setNotice(null); setError(null);
    try {
      await alertDeliveryApi.retry(retryId, reason, controller.signal);
      if (controller.signal.aborted || generation.current !== expected) return;
      setRetryId(null); setReason(""); setNotice("Retry queued. Delivery status will update after the next attempt.");
      await load(controller.signal);
    } catch (cause) {
      if (!controller.signal.aborted && generation.current === expected) setError(explainError(cause));
    } finally {
      if (generation.current === expected) setBusy(false);
    }
  };

  return <section className="alert-delivery-panel" aria-label="Alert delivery history">
    <header className="delivery-heading">
      <div><h2>Alert deliveries</h2><p>Track whether raised and escalated alerts reached the configured webhook.</p></div>
      <button type="button" onClick={() => void load()}>Refresh</button>
    </header>
    {error && <p role="alert" className="delivery-error">{error}</p>}
    {notice && <p role="status" className="delivery-notice">{notice}</p>}
    {data && <div className={`delivery-config ${data.configured ? "" : "is-unconfigured"}`}>
      <strong>{data.configured ? `Endpoint: ${data.destination}` : "Webhook is not configured"}</strong>
      <span>{data.configured
        ? `Up to ${data.max_attempts} attempts per retry cycle. Changes to the endpoint pause queued deliveries.`
        : "An administrator can configure SIO_ALERT_WEBHOOK_URL and restart the alerts service. New alerts are not queued while it is unset."}</span>
    </div>}
    <div className="delivery-filters"><label>Status <select aria-label="Delivery status" value={filter} disabled={busy} onChange={e => {setFilter(e.target.value as DeliveryStatus | "");setPageCursor(undefined);setPrevious([]);}}>
      <option value="">All statuses</option><option value="pending">Pending</option><option value="sending">Sending</option>
      <option value="delivered">Delivered</option><option value="failed">Failed</option><option value="blocked">Blocked</option>
    </select></label><form onSubmit={event => {event.preventDefault();setAlertId(alertDraft.trim());setPageCursor(undefined);setPrevious([]);}}><label>Alert ID<input value={alertDraft} disabled={busy} placeholder="All alerts" onChange={event => setAlertDraft(event.target.value)} /></label><button disabled={busy}>Filter alert</button></form><span>Up to 100 deliveries per page{filter ? ` · ${filter}` : ""}</span></div>
    {!data && !error && <p role="status">Loading delivery history…</p>}
    {data?.deliveries.length === 0 && <p className="delivery-empty">No {filter ? `${filter} ` : ""}deliveries recorded.</p>}
    <div className="delivery-list">{data?.deliveries.map(row => <article className="delivery-card" key={row.delivery_id}>
      <div className="delivery-card-title"><strong>{row.title}</strong><span className={`delivery-status is-${row.status}`}>{row.status}</span></div>
      <p className="delivery-meta">{row.action} · {row.destination} · {deliveryTime(row.created_at)}</p>
      <dl className="delivery-facts">
        <div><dt>Attempts</dt><dd>{row.attempts} total · {row.cycle_attempts}/{row.max_attempts} this cycle</dd></div>
        <div><dt>{row.status === "delivered" ? "Delivered" : "Next attempt"}</dt><dd>{deliveryTime(row.status === "delivered" ? row.delivered_at : row.next_attempt_at)}</dd></div>
        {row.status_code !== null && <div><dt>Last response</dt><dd>HTTP {row.status_code}</dd></div>}
      </dl>
      {row.error && <p className="delivery-failure">{row.error}</p>}
      {row.status === "blocked" && !row.can_retry && <p className="delivery-meta">Restore the original endpoint configuration to retry this delivery. It will never be redirected automatically.</p>}
      <DeliveryHistoryView key={row.delivery_id} delivery={row} />
      {canRetryDelivery(row, identity?.roles ?? []) && (retryId === row.delivery_id ? <form onSubmit={retry} className="delivery-retry">
        <label>Reason for retry<input value={reason} minLength={3} maxLength={240} required placeholder="What changed at the endpoint?" onChange={event => setReason(event.target.value)} disabled={busy} /></label>
        <div><button type="submit" disabled={busy || reason.trim().length < 3}>{busy ? "Queuing…" : "Queue retry"}</button>
          <button type="button" disabled={busy} onClick={() => { setRetryId(null); setReason(""); }}>Cancel</button></div>
      </form> : <button className="delivery-retry-button" type="button" disabled={busy} onClick={() => { setRetryId(row.delivery_id); setReason(""); }}>Retry delivery</button>)}
    </article>)}</div>
    <nav className="delivery-filters" aria-label="Alert delivery pages"><button disabled={busy || !data || !previous.length} onClick={() => {setPageCursor(previous.at(-1));setPrevious(rows => rows.slice(0,-1));}}>Previous page</button><span>Page {previous.length+1}</span><button disabled={busy || !data?.next_cursor} onClick={() => {setPrevious(rows => [...rows,pageCursor]);setPageCursor(data?.next_cursor ?? undefined);}}>Next page</button></nav>
    <p className="delivery-footnote">Interrupted requests may be delivered again. Receivers should deduplicate the stable delivery ID. A confirmed successful delivery cannot be retried.</p>
  </section>;
}
