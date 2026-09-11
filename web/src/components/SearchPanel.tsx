import { useEffect, useState, type FormEvent } from "react";
import { explainError } from "../lib/api";
import * as session from "../lib/session";
import { reviewApi } from "../lib/review-api";
import { localDateInput } from "../lib/review-state";
import type { ReviewSearchResult, SavedSearch, SearchFilters } from "../lib/review-types";
import "./review.css";
export interface SearchPanelProps { onSelectResult?: (kind: string, id: string, atS?: number, analysisId?: string) => void }
const kinds = ["entity", "event", "alert", "video", "video_event", "case", "site"];
export function SearchPanel({ onSelectResult }: SearchPanelProps) {
  session.useSession();
  const [draft, setDraft] = useState<SearchFilters>({ limit: 100 });
  const [applied, setApplied] = useState<SearchFilters>({ limit: 100 });
  const [saved, setSaved] = useState<SavedSearch[]>([]);
  const [rows, setRows] = useState<ReviewSearchResult[]>([]);
  const [name, setName] = useState("");
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [note, setNote] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const [truncated, setTruncated] = useState(false);
  useEffect(() => {
    const controller = new AbortController();
    void reviewApi.savedSearches(controller.signal).then(result => { if (!controller.signal.aborted) setSaved(result.saved_searches); }).catch(cause => { if (!controller.signal.aborted) setError(explainError(cause)); });
    return () => controller.abort();
  }, []);
  useEffect(() => {
    const controller = new AbortController(); setLoading(true); setError(null); setRows([]);
    void reviewApi.search(applied, controller.signal).then(result => { if (!controller.signal.aborted) { setRows(result.results); setTruncated(Boolean(result.possibly_truncated) || result.count >= result.limit); setNote(result.note ?? null); } }).catch(cause => { if (!controller.signal.aborted) setError(explainError(cause)); }).finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [applied]);
  function search(event: FormEvent) {
    event.preventDefault();
    if (draft.since && draft.until && new Date(draft.since) > new Date(draft.until)) { setError("The start time must be earlier than the end time."); return; }
    setMessage(null); setApplied({ ...draft });
  }
  function open(row: ReviewSearchResult) {
    if (row.kind === "video_event" && row.video_id) onSelectResult?.("video", row.video_id, row.at_s ?? undefined, row.analysis_id ?? undefined);
    else onSelectResult?.(row.kind, row.id, row.at_s ?? undefined, row.analysis_id ?? undefined);
  }
  async function save(event: FormEvent) {
    event.preventDefault(); if (!name.trim()) return; setSaving(true); setError(null);
    try { const record = await reviewApi.saveSearch(name.trim(), applied); setSaved(current => [record, ...current]); setName(""); setMessage("Search saved for your signed-in account."); }
    catch (cause) { setError(explainError(cause)); } finally { setSaving(false); }
  }
  return <div className="review-shell"><aside className="review-library"><span className="rv-kicker">FIND / RETURN / CONTINUE</span><h2>Search</h2><p className="rv-muted">Follow a name, identifier or boundary across the workspace.</p><div className="rv-library-head"><span>Saved searches</span></div><div className="rv-library-list">{saved.map(row => <button className="rv-library-item" key={row.search_id} onClick={() => { setDraft(row.query); setApplied({ ...row.query }); setMessage(`Opened saved search: ${row.name}`); }}><strong>{row.name}</strong><span>{row.query.kind || "All records"} · {row.query.q || "Filtered view"}</span></button>)}</div>{!saved.length && <p className="rv-empty">Save a useful query to return to the same filters later.</p>}<form className="rv-saved-search-form" onSubmit={event => void save(event)}><label>Name this search<input maxLength={100} value={name} onChange={event => setName(event.target.value)} placeholder="e.g. North entrance review" disabled={!session.can("review.write")} /></label><button className="rv-button" disabled={!name.trim() || saving || loading || !session.can("review.write")}>{saving ? "Saving…" : "Save applied filters"}</button><p className="rv-small">Saves the filters from the latest search. Queries are private to your account.</p></form></aside><main className="review-main"><header className="rv-page-head"><div><span className="rv-kicker">WORKSPACE / LITERAL SEARCH</span><h1>Search saved records</h1></div><span className="rv-badge">Names, IDs and labels</span></header><p className="rv-muted">Search persisted records using literal text and exact source or zone IDs. This does not perform visual or semantic matching.</p>{error && <div className="rv-error" role="alert">{error}</div>}{message && <p className="rv-notice" role="status">{message}</p>}<form className="rv-search-form rv-editor-card" onSubmit={search}><label className="rv-search-query">Search text<input type="search" value={draft.q ?? ""} onChange={event => setDraft(current => ({ ...current, q: event.target.value }))} placeholder="A case title, event label or record ID…" /></label><div className="rv-search-filters"><label>Record type<select value={draft.kind ?? ""} onChange={event => setDraft(current => ({ ...current, kind: event.target.value }))}><option value="">All records</option>{kinds.map(kind => <option key={kind} value={kind}>{kind.replace(/_/g, " ")}</option>)}</select></label><label>Source ID<input value={draft.source_id ?? ""} onChange={event => setDraft(current => ({ ...current, source_id: event.target.value }))} placeholder="Exact source ID" /></label><label>Zone ID<input value={draft.zone_id ?? ""} onChange={event => setDraft(current => ({ ...current, zone_id: event.target.value }))} placeholder="Exact zone ID" /></label><label>From (local time)<input type="datetime-local" value={localDateInput(draft.since)} onChange={event => setDraft(current => ({ ...current, since: event.target.value ? new Date(event.target.value).toISOString() : undefined }))} /></label><label>Until (local time)<input type="datetime-local" value={localDateInput(draft.until)} onChange={event => setDraft(current => ({ ...current, until: event.target.value ? new Date(event.target.value).toISOString() : undefined }))} /></label></div><div className="rv-row"><button type="submit" className="rv-button rv-primary" disabled={loading}>{loading ? "Searching…" : "Search records"}</button><button type="button" className="rv-button" onClick={() => { const empty = { limit: 100 }; setDraft(empty); setApplied(empty); }}>Clear filters</button></div></form><div className="rv-section-title"><h2>{loading ? "Searching records" : `${rows.length} ${rows.length === 1 ? "result" : "results"}`}</h2>{truncated && <span className="rv-badge rv-warning">Bounded result set</span>}</div>{note && <p className="rv-small">{note}</p>}{truncated && <p className="rv-notice">The search may omit older records. Narrow the filters to inspect a smaller set.</p>}<div className="rv-search-results">{rows.map(row => <button className="rv-search-result" key={`${row.kind}:${row.id}`} onClick={() => open(row)}><span className="rv-badge">{row.kind.replace(/_/g, " ")}</span><div><strong>{row.title}</strong><p>{row.summary || row.id}</p><small>{[row.source_id, row.zone_id].filter(Boolean).join(" · ")}</small></div><time>{new Date(row.ts).toLocaleString()}</time><span aria-hidden="true">↗</span></button>)}</div>{!loading && !rows.length && !error && <div className="rv-blank"><h2>No matching records.</h2><p>Try a shorter literal phrase or remove a source, zone or time filter.</p></div>}</main></div>;
}
