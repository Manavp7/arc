import { useEffect, useRef, useState } from "react";
import { explainError } from "../lib/api";
import { reviewApi } from "../lib/review-api";
import { ReviewImage } from "../lib/review-media";
import { formatVideoTime } from "../lib/review-geometry";
import { attachmentRequest, caseEvidenceItems, evidenceOriginLabel, evidencePosition } from "../lib/case-attachments";
import type { CaseRecord, ReviewSearchResult, ReviewVideo } from "../lib/review-types";
import { evaluationApi, type EvaluationVersions } from "../lib/evaluation";
import { annotationCasesApi, frozenAnnotationCase, type AnnotationCaseSource } from "../lib/annotation-cases";
import * as session from "../lib/session";
import "./case-attachments.css";

export function CaseEvidenceAttachments({record, disabled, onAttached, onDraftChange, onBusyChange, onOpenVideo}: {record: CaseRecord; disabled: boolean; onAttached: (record: CaseRecord) => void; onDraftChange: (dirty: boolean) => void; onBusyChange: (busy: boolean) => void; onOpenVideo?: (id: string, atS?: number, analysisId?: string) => void}) {
  const [kind, setKind] = useState<"video_event" | "alert" | "reviewer_annotation">("video_event");
  const [query, setQuery] = useState("");
  const [results, setResults] = useState<ReviewSearchResult[]>([]);
  const [chosen, setChosen] = useState<ReviewSearchResult | null>(null);
  const [annotationSource, setAnnotationSource] = useState<AnnotationCaseSource | null>(null);
  const [pickerVersion, setPickerVersion] = useState(0);
  const mutation = useRef(false);
  const [note, setNote] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [message, setMessage] = useState("");
  const [busy, setBusy] = useState(false);
  const [searched, setSearched] = useState(false);
  const searchController = useRef<AbortController | null>(null);
  const items = caseEvidenceItems(record);
  const canAttach = session.can("case.write");
  useEffect(() => {setChosen(null); setAnnotationSource(null); setNote(""); setResults([]); setQuery(""); setSearched(false); setError(null); setMessage("");}, [record.case_id]);
  useEffect(() => () => searchController.current?.abort(), [record.case_id]);
  useEffect(() => {onDraftChange(Boolean(chosen || annotationSource || note.trim()));}, [chosen, annotationSource, note, onDraftChange]);
  async function search() {if (kind === "reviewer_annotation") return; searchController.current?.abort(); const controller = new AbortController(); searchController.current = controller; setBusy(true); setError(null); try {const response = await reviewApi.search({kind, q: query.trim(), limit: 20}, controller.signal); if (!controller.signal.aborted) {setResults(response.results); setSearched(true);}} catch (cause) {if (!controller.signal.aborted) setError(explainError(cause));} finally {if (!controller.signal.aborted) setBusy(false);}}
  async function attach() {
    if ((!chosen && !annotationSource) || mutation.current || disabled || busy) return;
    mutation.current = true; setBusy(true); onBusyChange(true); setError(null); setMessage("");
    try {
      const updated = annotationSource
        ? await annotationCasesApi.attach(record.case_id, annotationSource, record.revision, note)
        : await reviewApi.attachEvidence(record.case_id, attachmentRequest(chosen!, record.revision, note));
      onAttached(updated); setChosen(null); setAnnotationSource(null); setPickerVersion(value => value + 1); setNote("");
      setMessage("Evidence is saved with its source version, your identity and the attachment time. Identical sources are included only once and keep their original report context.");
    } catch (cause) {setError(explainError(cause));}
    finally {mutation.current = false; setBusy(false); onBusyChange(false);}
  }

  return <section className="rv-editor-card case-attachments"><div className="rv-section-title"><div><span className="rv-kicker">SOURCES / PINNED EVIDENCE</span><h2>Evidence timeline · {items.length}/20</h2></div></div><p className="rv-small">Platform sources use their recorded timestamps. Video seconds belong to each recording; where an absolute recording clock is unknown, the timeline uses the time evidence was attached.</p>
    <ol className="case-source-timeline">{items.map(item => <li key={item.attachment_id}><div className="case-source-heading"><span className="rv-badge">{item.attachment_id === "original" ? "Original" : "Attached"} · {evidenceOriginLabel(item)}</span><time>{new Date(item.timeline_at || item.attached_at).toLocaleString()}<small>{item.time_basis === "platform_source_time" ? "Platform source time" : "Attachment time · source clock unknown"}</small></time></div><h3>{item.title || item.evidence.event?.title || item.evidence.video?.title || item.alert_id || "Evidence source"}</h3><p className="rv-small">Added by {item.attached_by} · {new Date(item.attached_at).toLocaleString()}</p>{item.note && <p className="case-source-note">{item.note}</p>}{item.video_id && <><p className="rv-small">Recording {item.video_id}<br />Pinned analysis {item.analysis_id}<br />{item.evidence.source === "reviewer_annotation" ? <>Observation {item.annotation_id ?? item.evidence.annotation?.annotation_id} · {evidencePosition(item).toFixed(1)}–{(item.evidence.annotation?.end_s ?? evidencePosition(item)).toFixed(1)}s<br />Frozen labels {item.annotation_set_id ?? item.evidence.annotation_set?.annotation_set_id}</> : <>Event {item.event_id} · {formatVideoTime(evidencePosition(item))}</>}</p>{item.evidence.source === "reviewer_annotation" ? <div className="case-human-observation"><strong>{item.evidence.annotation?.event_type} · {item.evidence.annotation?.zone_id}</strong>{item.evidence.annotation?.note && <p>{item.evidence.annotation.note}</p>}<p className="rv-small">Frozen human observation · no detector confidence.{item.evidence.annotation_set?.frozen_by && <> Retained by {item.evidence.annotation_set.frozen_by}.</>}{item.evidence.annotation_set?.frozen_at && <> {new Date(item.evidence.annotation_set.frozen_at).toLocaleString()}.</>}</p>{item.evidence.evaluation_report ? <p className="rv-small">Evaluated miss in report {item.evidence.evaluation_report.report_id}. The report and exact analysis are retained with this source.</p> : <p className="rv-small">No evaluation report was linked when this source was added.</p>}</div> : <ReviewImage path={item.evidence.event?.frame_url} alt="Protected snapshot for attached recording event" />}<button className="rv-button" disabled={disabled || busy || Boolean(chosen || annotationSource || note.trim())} onClick={() => onOpenVideo?.(item.video_id!, evidencePosition(item), item.analysis_id ?? undefined)}>Open exact footage ↗</button></>}{item.alert_id && <p className="rv-small">Alert {item.alert_id}</p>}{item.evidence.resolved_frames?.length ? <div className="rv-evidence-grid">{item.evidence.resolved_frames.slice(0, 4).map(frame => <figure key={frame.frame_id}><ReviewImage path={frame.media_url} alt={`Protected source evidence from ${frame.source_id}`} /><figcaption>{frame.source_id} · {new Date(frame.ts).toLocaleString()}</figcaption></figure>)}</div> : null}{item.evidence.media_note && <p className="rv-small">{item.evidence.media_note}</p>}{item.evidence.unresolved_media?.length ? <p className="rv-small">{item.evidence.unresolved_media.length} historical media references are unavailable.</p> : null}</li>)}</ol>
    {canAttach && <div className="case-attach-form"><h3>Attach an existing evidence source</h3><p className="rv-small">Choose a persisted recording event, platform alert, or frozen human observation. The server copies its authoritative evidence; existing snapshots cannot be replaced or removed.</p>{disabled && <p className="rv-small">Save or discard case edits before attaching evidence.</p>}{error && <p className="rv-error" role="alert">{error}</p>}{message && <p className="rv-notice" role="status">{message}</p>}<fieldset className="rv-fieldset" disabled={disabled || busy || items.length >= 20}><label>Source type<select value={kind} onChange={event => {searchController.current?.abort(); setKind(event.target.value as typeof kind); setChosen(null); setAnnotationSource(null); setResults([]); setSearched(false); setError(null);}}><option value="video_event">Recorded event</option><option value="alert">Platform alert</option><option value="reviewer_annotation">Frozen human observation</option></select></label>
    {kind === "reviewer_annotation" ? <FrozenObservationPicker key={`${record.case_id}:${pickerVersion}`} onSelect={setAnnotationSource} /> : <><form className="case-attachment-search" onSubmit={event => {event.preventDefault(); void search();}}><label>Find source<input value={query} maxLength={200} onChange={event => setQuery(event.target.value)} placeholder="Event title or identifier" /></label><button className="rv-button" type="submit">{busy ? "Searching…" : "Search sources"}</button></form><div className="case-attachment-choices">{results.map((result, index) => <button key={`${result.kind}-${result.id}-${index}`} className={`rv-library-item ${chosen === result ? "is-selected" : ""}`} aria-pressed={chosen === result} onClick={() => setChosen(result)}><strong>{result.title}</strong><small>{result.id}{result.analysis_id ? ` · ${result.analysis_id}` : ""}{result.at_s != null ? ` · ${formatVideoTime(result.at_s)}` : ""}</small></button>)}{searched && !results.length && <p className="rv-empty">No visible sources match. Complete the recording analysis or narrow your search.</p>}</div>{chosen && <p className="rv-small">Selected: {chosen.title} · {chosen.id}</p>}</>}
    <label>Reason or context (optional)<textarea rows={2} maxLength={4000} value={note} onChange={event => setNote(event.target.value)} placeholder="How does this source relate to the investigation?" /></label><div className="rv-row rv-form-actions"><button className="rv-button rv-primary" disabled={!chosen && !annotationSource} onClick={() => void attach()}>{busy ? "Saving…" : "Attach immutable evidence"}</button><button className="rv-button" disabled={!chosen && !annotationSource && !note.trim()} onClick={() => {setChosen(null); setAnnotationSource(null); setPickerVersion(value => value + 1); setNote(""); setError(null);}}>Discard attachment draft</button></div></fieldset>{items.length >= 20 && <p className="rv-small">This case has reached its 20-source limit.</p>}</div>}
  </section>;
}

function FrozenObservationPicker({onSelect}: {onSelect: (source: AnnotationCaseSource | null) => void}) {
  const [videos, setVideos] = useState<ReviewVideo[]>([]), [videoId, setVideoId] = useState("");
  const [versions, setVersions] = useState<EvaluationVersions | null>(null);
  const [setId, setSetId] = useState(""), [analysisId, setAnalysisId] = useState(""), [annotationId, setAnnotationId] = useState("");
  const [loading, setLoading] = useState(false), [error, setError] = useState("");
  const frozen = versions?.annotation_sets.find(item => item.annotation_set_id === setId);
  useEffect(() => {
    const controller = new AbortController();
    void reviewApi.videos(controller.signal).then(result => {if (!controller.signal.aborted) setVideos(result.videos);}).catch(cause => {if (!controller.signal.aborted) setError(explainError(cause));});
    return () => controller.abort();
  }, []);
  useEffect(() => {
    const controller = new AbortController(); setVersions(null); setSetId(""); setAnalysisId(""); setAnnotationId(""); setError(""); onSelect(null);
    if (videoId) {setLoading(true); void evaluationApi.versions(videoId, controller.signal).then(result => {if (!controller.signal.aborted) setVersions(result);}).catch(cause => {if (!controller.signal.aborted) setError(explainError(cause));}).finally(() => {if (!controller.signal.aborted) setLoading(false);});}
    else setLoading(false);
    return () => controller.abort();
  }, [videoId, onSelect]);
  useEffect(() => {
    if (!frozen || !annotationId || !analysisId || versions?.video.video_id !== videoId) {onSelect(null); return;}
    try {onSelect(frozenAnnotationCase(videoId, frozen, annotationId, versions?.analyses.find(item => item.analysis_id === analysisId)));}
    catch (cause) {onSelect(null); setError(explainError(cause));}
  }, [videoId, frozen, annotationId, analysisId, versions, onSelect]);
  return <div className="case-frozen-picker"><p className="rv-small">Save and freeze labels in the Evaluation lab first. Select a retained analysis as context; this does not claim the detector emitted an event or identify an evaluated miss.</p>{error && <p className="rv-error" role="alert">{error}</p>}<label>Observation recording<select value={videoId} onChange={event => {onSelect(null); setVideoId(event.target.value);}}><option value="">Choose recording</option>{videos.map(video => <option key={video.video_id} value={video.video_id}>{video.title}</option>)}</select></label>{loading && <p className="rv-small" role="status">Loading frozen labels…</p>}{versions && <><label>Frozen annotation version<select value={setId} onChange={event => {onSelect(null); setSetId(event.target.value); setAnnotationId("");}}><option value="">Choose frozen labels</option>{versions.annotation_sets.map(item => <option key={item.annotation_set_id} value={item.annotation_set_id}>{item.name} · draft r{item.draft_revision}</option>)}</select></label><label>Retained analysis context<select value={analysisId} onChange={event => {onSelect(null); setAnalysisId(event.target.value);}}><option value="">Choose completed analysis</option>{versions.analyses.filter(item => item.status === "completed").map(item => <option key={item.analysis_id} value={item.analysis_id}>{item.model?.name ?? "Analysis"} · {item.analysis_id}</option>)}</select></label>{frozen && <label>Frozen human observation<select value={annotationId} onChange={event => {onSelect(null); setAnnotationId(event.target.value);}}><option value="">Choose observed incident</option>{frozen.annotations.map(item => <option key={item.annotation_id} value={item.annotation_id}>{item.event_type} · {item.zone_id} · {item.start_s.toFixed(1)}–{item.end_s.toFixed(1)}s · {item.note}</option>)}</select></label>}{!versions.annotation_sets.length && <p className="rv-empty">No frozen annotation versions. Save and freeze reviewed labels in the Evaluation lab.</p>}</>}</div>;
}
