import { useEffect, useMemo, useRef, useState, type FormEvent } from "react";
import { explainError } from "../lib/api";
import { footageTimestamp } from "../lib/footage-navigation";
import { bookmarkScope } from "../lib/review-bookmarks";
import { useProtectedMedia } from "../lib/review-media";
import * as session from "../lib/session";
import { changeObjectFilter, emptyObjectFilters, nextObjectOffset, objectConfidence, objectExplorerApi, objectFilterOptions, objectFilterProblem, objectFramePath, objectObservations, objectPageLabel, objectQuery, objectTrackKey, reconcileObjectFilters, sameObjectFilters, type ObjectCatalog, type ObjectFilters, type ObjectObservation, type ObjectPage, type RecordedObject } from "../lib/object-explorer";
import "./review.css";
import "./object-explorer.css";

interface Props { onOpenFootage: (videoId: string, atS: number, analysisId: string) => void }
export function ObjectExplorerPanel(props: Props) {
  const identity = session.useSession();
  return identity ? <ScopedObjectExplorer key={bookmarkScope(identity)} {...props} /> : null;
}
function ObjectTrack({ row, onOpenFootage }: Props & { row: RecordedObject }) {
  const observations = useMemo(() => objectObservations(row), [row]);
  const [sample, setSample] = useState(() => Math.max(0, observations.findIndex(observation => observation.at_s === row.at_s)));
  const observation = observations[sample] ?? observations[0];
  const first = observations[0], last = observations[observations.length - 1];
  const minimumConfidence = objectConfidence(row.confidence_min), maximumConfidence = objectConfidence(row.confidence_max);
  const confidence = minimumConfidence === maximumConfidence ? maximumConfidence : `${minimumConfidence}–${maximumConfidence}`;
  const open = () => { if (observation) onOpenFootage(row.video_id, observation.at_s, row.analysis_id); };
  return <article className="oe-track" aria-label={`${row.class_name} track ${row.track_id} in ${row.video_title}`}>
    <ObjectThumbnail key={`${observation?.frame_index}:${observation?.at_s}`} row={row} observation={observation} onOpen={open} />
    <div className="oe-track-body">
      <div className="oe-track-heading"><div><span className="rv-badge">{row.class_name}</span><h2>{row.video_title}</h2></div><span className="oe-track-id" title={row.track_id}>TRACK {row.track_id}</span></div>
      <p className="oe-provenance">Analysis {row.analysis_id}</p>
      <dl className="oe-track-facts"><div><dt>First matching sample</dt><dd>{footageTimestamp(row.first_s)}</dd></div><div><dt>Last matching sample</dt><dd>{footageTimestamp(row.last_s)}</dd></div><div><dt>Matched samples</dt><dd>{row.sample_count}</dd></div><div><dt>Confidence</dt><dd>{confidence}</dd></div></dl>
      <p className="oe-zones">{row.zone_names.length ? row.zone_names.join(" · ") : "No matching zone membership"}</p>
      <div className="oe-track-actions"><label>Saved sample<select value={sample} disabled={!observations.length} onChange={event => setSample(Number(event.target.value))}>{observations.map((item, index) => <option key={`${item.frame_index}:${item.at_s}`} value={index}>{footageTimestamp(item.at_s)}{index === 0 ? " · first" : ""}{index === observations.length - 1 ? " · last" : ""}</option>)}</select></label><div className="rv-row"><button className="rv-button" disabled={!first || sample === 0} onClick={() => setSample(0)}>First</button><button className="rv-button" disabled={!last || sample === observations.length - 1} onClick={() => setSample(observations.length - 1)}>Last</button><button className="rv-button rv-primary" disabled={!observation} onClick={open}>Open footage ↗</button></div></div>
      {row.sample_count > observations.length && <p className="rv-small">{observations.length} saved sample links returned from {row.sample_count} matches{first?.at_s === row.first_s && last?.at_s === row.last_s ? ", including the first and last" : ""}.</p>}
    </div>
  </article>;
}
function ObjectThumbnail({ row, observation, onOpen }: { row: RecordedObject; observation?: ObjectObservation; onOpen: () => void }) {
  const element = useRef<HTMLButtonElement | null>(null);
  const [visible, setVisible] = useState(false);
  const [size, setSize] = useState<{ width: number; height: number } | null>(null);
  useEffect(() => {
    if (!element.current) return;
    if (typeof IntersectionObserver === "undefined") { setVisible(true); return; }
    const observer = new IntersectionObserver(entries => { if (entries.some(entry => entry.isIntersecting)) { setVisible(true); observer.disconnect(); } }, { rootMargin: "200px" });
    observer.observe(element.current); return () => observer.disconnect();
  }, []);
  const path = objectFramePath(row, observation);
  const image = useProtectedMedia(visible ? path : null, "image");
  const box = observation?.bbox;
  const validBox = box?.length === 4 && box.every(value => Number.isFinite(value) && value >= 0 && value <= 1) && box[2]! > box[0]! && box[3]! > box[1]!;
  return <button ref={element} className="oe-thumbnail" onClick={onOpen} disabled={!observation} aria-label={`Open ${row.class_name} sample at ${footageTimestamp(observation?.at_s ?? row.at_s)} in ${row.video_title}`} title={image.error ?? undefined}>
    {image.url ? <img src={image.url} alt={`${row.class_name} track ${row.track_id} at ${footageTimestamp(observation?.at_s ?? row.at_s)}`} onLoad={event => setSize({ width: event.currentTarget.naturalWidth, height: event.currentTarget.naturalHeight })} /> : <span>{image.loading || !visible && path ? "Loading saved frame…" : image.error ? "Frame unavailable" : "No saved thumbnail"}</span>}
    {image.url && size && validBox && <svg className="oe-object-box" viewBox={`0 0 ${size.width} ${size.height}`} preserveAspectRatio="xMidYMid meet" aria-hidden="true"><rect x={box[0]! * size.width} y={box[1]! * size.height} width={(box[2]! - box[0]!) * size.width} height={(box[3]! - box[1]!) * size.height} /></svg>}
    {observation && <time>{footageTimestamp(observation.at_s)} <span aria-hidden="true">↗</span></time>}
  </button>;
}
function ScopedObjectExplorer({ onOpenFootage }: Props) {
  const [catalog, setCatalog] = useState<ObjectCatalog | null>(null);
  const [catalogError, setCatalogError] = useState<string | null>(null);
  const [catalogLoading, setCatalogLoading] = useState(true);
  const [draft, setDraft] = useState<ObjectFilters>(emptyObjectFilters);
  const [applied, setApplied] = useState<ObjectFilters>(emptyObjectFilters);
  const [offset, setOffset] = useState(0), [reload, setReload] = useState(0);
  const [response, setResponse] = useState<{ key: string; page: ObjectPage } | null>(null);
  const [failure, setFailure] = useState<{ key: string; message: string } | null>(null);
  const [settled, setSettled] = useState("");
  const requestKey = `${reload}:${objectQuery(applied, offset)}`;
  const page = response?.key === requestKey ? response.page : null;
  const error = failure?.key === requestKey ? failure.message : null;
  const loading = settled !== requestKey;
  const options = objectFilterOptions(catalog, draft);
  const selectedVideo = catalog?.videos.find(video => video.video_id === draft.video_id);
  const problem = objectFilterProblem(draft, selectedVideo?.duration_s);
  const changed = !sameObjectFilters(draft, applied);

  useEffect(() => {
    const controller = new AbortController();
    setCatalogLoading(true); setCatalogError(null);
    void objectExplorerApi.catalog(controller.signal).then(value => {
      if (controller.signal.aborted) return;
      setCatalog(value);
      // A refreshed catalog can change a scoped version or class; its result set starts anew.
      setOffset(0);
      setDraft(current => reconcileObjectFilters(current, value));
      setApplied(current => { const next = reconcileObjectFilters(current, value); return sameObjectFilters(current, next) ? current : next; });
    }).catch(cause => { if (!controller.signal.aborted) { setCatalog(null); setCatalogError(explainError(cause)); } }).finally(() => { if (!controller.signal.aborted) setCatalogLoading(false); });
    return () => controller.abort();
  }, [reload]);
  useEffect(() => {
    const controller = new AbortController();
    void objectExplorerApi.search(applied, offset, controller.signal).then(value => {
      if (!controller.signal.aborted) { setResponse({ key: requestKey, page: value }); setFailure(null); }
    }).catch(cause => { if (!controller.signal.aborted) { setResponse(null); setFailure({ key: requestKey, message: explainError(cause) }); } }).finally(() => { if (!controller.signal.aborted) setSettled(requestKey); });
    return () => controller.abort();
  }, [requestKey, applied, offset]);

  function update(key: keyof ObjectFilters, value: string) { setDraft(current => changeObjectFilter(current, key, value)); }
  function apply(event: FormEvent) { event.preventDefault(); if (problem) return; setOffset(0); setApplied({ ...draft }); setReload(value => value + 1); }
  function clear() { const empty = emptyObjectFilters(); setDraft(empty); setApplied(empty); setOffset(0); setReload(value => value + 1); }
  const next = page ? nextObjectOffset(page) : null;
  const appliedVideo = catalog?.videos.find(video => video.video_id === applied.video_id);

  return <main className="review-main oe-panel">
    <header className="rv-page-head"><div><span className="rv-kicker">RECORDED FOOTAGE / OBJECT EXPLORER</span><h1>Find an object. Follow its samples.</h1><p className="rv-muted">Search saved detections, then open the exact recording and analysis that produced them.</p></div><button className="rv-button" disabled={loading || catalogLoading} onClick={() => { setOffset(0); setReload(value => value + 1); }}>Refresh objects</button></header>
    <form className="rv-editor-card oe-filters" aria-label="Object search filters" onSubmit={apply}>
      <div className="oe-filter-head"><span className="rv-kicker">01 / NARROW THE FOOTAGE</span>{changed && <span className="rv-badge rv-warning">Filter edits not applied</span>}</div>
      <div className="oe-filter-grid">
        <label className="oe-wide">Recording<select value={draft.video_id} disabled={catalogLoading || !catalog} onChange={event => update("video_id", event.target.value)}><option value="">All accessible recordings</option>{catalog?.videos.map(video => <option key={video.video_id} value={video.video_id}>{video.title}</option>)}</select></label>
        <label className="oe-wide">Analysis version<select value={draft.analysis_id} disabled={!draft.video_id || catalogLoading || !options.analyses.length} onChange={event => update("analysis_id", event.target.value)}><option value="">Newest completed analysis{options.analyses[0] ? ` · ${options.analyses[0].model.name}` : " per recording"}</option>{options.analyses.map(analysis => <option key={analysis.analysis_id} value={analysis.analysis_id}>{new Date(analysis.created_at).toLocaleString()} · {analysis.model.name} · {analysis.analysis_id}</option>)}</select></label>
        <label>Object class<select value={draft.class_name} disabled={catalogLoading || !catalog} onChange={event => update("class_name", event.target.value)}><option value="">All detected classes</option>{options.classes.map(name => <option key={name}>{name}</option>)}</select></label>
        <label>Zone<select value={draft.zone_id} disabled={!draft.video_id || catalogLoading || !options.zones.length} onChange={event => update("zone_id", event.target.value)}><option value="">{draft.video_id ? "Any zone or outside" : "Choose a recording first"}</option>{options.zones.map(zone => <option key={zone.zone_id} value={zone.zone_id}>{zone.name}</option>)}</select></label>
        <label>Minimum confidence<input type="number" min="0" max="1" step="0.01" value={draft.min_confidence} placeholder="Any measured or unmeasured" onChange={event => update("min_confidence", event.target.value)} /></label>
        <label>Clip start (seconds)<input type="number" min="0" max={selectedVideo?.duration_s ?? 180} step="0.001" value={draft.start_s} placeholder="Start of recording" onChange={event => update("start_s", event.target.value)} /></label>
        <label>Clip end (seconds)<input type="number" min="0" max={selectedVideo?.duration_s ?? 180} step="0.001" value={draft.end_s} placeholder={selectedVideo ? `${selectedVideo.duration_s}s` : "End of recording"} onChange={event => update("end_s", event.target.value)} /></label>
      </div>
      <p className="rv-small">Times are offsets within each clip. A confidence filter excludes detections without a measured confidence. Zone filters use the selected analysis's saved zones.</p>
      {problem && <p className="rv-error" role="alert">{problem}</p>}
      <div className="rv-row"><button className="rv-button rv-primary" type="submit" disabled={Boolean(problem) || catalogLoading}>Search objects</button><button className="rv-button" type="button" onClick={clear}>Clear filters</button></div>
    </form>
    {catalogError && <p className="rv-error" role="alert">Recording filters could not load: {catalogError}</p>}
    {catalog?.possibly_truncated && <p className="rv-notice">The recording catalog is bounded. Older analysis versions may be omitted from these choices. {catalog.note}</p>}
    <section className="oe-results" aria-label="Object search results" aria-busy={loading}>
      <div className="rv-section-title"><div><span className="rv-kicker">02 / MATCHING TRACKS</span><h2 aria-live="polite">{loading ? "Searching saved detections…" : page ? objectPageLabel(page) : "Object results unavailable"}</h2></div>{page?.scan_truncated && <span className="rv-badge rv-warning">Bounded scan</span>}</div>
      <p className="rv-small oe-result-scope">{appliedVideo?.title ?? (applied.video_id || "All accessible recordings")} · {applied.analysis_id ? `Exact analysis ${applied.analysis_id}` : "Newest completed analysis per recording"}{applied.class_name ? ` · ${applied.class_name}` : " · all detected classes"}{applied.zone_id ? ` · Zone ${applied.zone_id}` : ""}{applied.min_confidence !== "" ? ` · confidence ≥ ${objectConfidence(Number(applied.min_confidence))}` : ""}{applied.start_s !== "" || applied.end_s !== "" ? ` · clip ${applied.start_s || "0"}s to ${applied.end_s ? `${applied.end_s}s` : "end"}` : ""}{changed ? " · Search objects to apply filter edits above." : ""}</p>
      {error && <p className="rv-error" role="alert">{error}</p>}
      {page?.note && <p className="rv-small">{page.note}</p>}
      {page?.scan_truncated && <p className="rv-notice">The scan reached its limit. Counts may omit older tracks. Choose one recording or narrow the time window.</p>}
      {page && !page.results.length && <div className="rv-blank"><span>NO MATCHES IN THIS SCOPE</span><h2>{page.offset ? "This page is empty." : "No saved tracks match these filters."}</h2><p>{page.offset ? "Results may have changed since the previous page. Return to the first page or refresh." : "Try another class, remove a zone or confidence filter, or run an analysis from Footage."}</p>{page.offset > 0 && <button className="rv-button" onClick={() => setOffset(0)}>Return to first page</button>}</div>}
      <div className="oe-track-list">{page?.results.map(row => <ObjectTrack key={`${requestKey}:${objectTrackKey(row)}`} row={row} onOpenFootage={onOpenFootage} />)}</div>
      {page && (page.offset > 0 || next !== null) && <nav className="oe-pagination" aria-label="Object result pages"><button className="rv-button" disabled={page.offset === 0 || loading || catalogLoading || changed} onClick={() => setOffset(Math.max(0, page.offset - page.limit))}>Previous tracks</button><span>{objectPageLabel(page)}</span><button className="rv-button" disabled={next === null || loading || catalogLoading || changed} onClick={() => { if (next !== null) setOffset(next); }}>Next tracks</button></nav>}
      <p className="oe-footnote">Each track belongs to one analysis of one recording. Gaps can split one object into several tracks; track counts do not establish unique people or vehicles across recordings.</p>
    </section>
  </main>;
}
