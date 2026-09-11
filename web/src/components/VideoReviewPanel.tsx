import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ApiError, explainError } from "../lib/api";
import * as session from "../lib/session";
import { loadVideoReview, reviewApi, validateUpload } from "../lib/review-api";
import { reviewConfiguration } from "../lib/review-state";
import { useProtectedMedia } from "../lib/review-media";
import { detectionAt, formatVideoTime, normalizedPoint, polygonPoints, polygonProblem, trackTrails } from "../lib/review-geometry";
import type { NormalizedPoint, ReviewRule, ReviewVideo, ReviewZone, RulePreview, VideoAnalysis, VideoEvent, VideoLibrary } from "../lib/review-types";
import { RulePresetsPanel } from "./RulePresetsPanel";
import { AnalysisControls } from "./AnalysisControls";
import { FootageNavigation } from "./FootageNavigation";
import { FootageBookmarks } from "./FootageBookmarks";
import { MovementPanel } from "./MovementPanel";
import { analysisOptionsProblem, defaultAnalysisOptions, normalizedAnalysisOptions, sameAnalysisOptions } from "../lib/analysis-profiles";
import type { AnalysisOptions } from "../lib/review-types";
import type { ReviewBookmark } from "../lib/review-bookmarks";
import "./review.css";

export interface VideoReviewPanelProps { initialVideoId?: string; initialAtS?: number; initialAnalysisId?: string; onOpenCase?: (id: string) => void; onDirtyChange?: (dirty: boolean) => void }
const freshId = (prefix: string) => `${prefix}_${crypto.randomUUID().slice(0, 12)}`;
export function VideoReviewPanel({ initialVideoId, initialAtS = 0, initialAnalysisId, onOpenCase, onDirtyChange }: VideoReviewPanelProps) {
  session.useSession();
  const [pinnedAnalysis, setPinnedAnalysis] = useState(initialAnalysisId);
  const historical = Boolean(pinnedAnalysis);
  const [library, setLibrary] = useState<VideoLibrary | null>(null);
  const [selectedId, setSelectedId] = useState<string | null>(initialVideoId ?? null);
  const selectedRef = useRef(selectedId); selectedRef.current = selectedId;
  const [video, setVideo] = useState<ReviewVideo | null>(null);
  const [analysis, setAnalysis] = useState<VideoAnalysis | null>(null);
  const [zones, setZones] = useState<ReviewZone[]>([]);
  const [rules, setRules] = useState<ReviewRule[]>([]);
  const [dirty, setDirty] = useState(false);
  const [draftPoints, setDraftPoints] = useState<NormalizedPoint[]>([]);
  const [drawing, setDrawing] = useState(false);
  const [zoneName, setZoneName] = useState("");
  const [coordinatePoint, setCoordinatePoint] = useState<NormalizedPoint>([0.5, 0.5]);
  const [preview, setPreview] = useState<RulePreview | null>(null);
  const [atS, setAtS] = useState(initialAtS);
  const [showBoxes, setShowBoxes] = useState(true);
  const [showTrails, setShowTrails] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [statusError, setStatusError] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [reload, setReload] = useState(0);
  const [showPresets, setShowPresets] = useState(false);
  const [analysisOptions, setAnalysisOptions] = useState<AnalysisOptions>({mode:"motion",sample_fps:2});
  const [savedOptions, setSavedOptions] = useState<AnalysisOptions>({mode:"motion",sample_fps:2});
  const [bookmarkDirty, setBookmarkDirty] = useState(false);
  const [movementDirty, setMovementDirty] = useState(false);
  const [bookmarkRequest, setBookmarkRequest] = useState<{atS:number;nonce:number}>();
  const profileDirty = !sameAnalysisOptions(analysisOptions,savedOptions);
  const optionsProblem = analysisOptionsProblem(analysisOptions,library?.capabilities.analysis_profiles);
  const requestBookmark = useCallback((position:number) => setBookmarkRequest(value=>({atS:position,nonce:(value?.nonce??0)+1})), []);
  const [batch, setBatch] = useState<{name: string; state: string}[]>([]);
  const fileRef = useRef<HTMLInputElement | null>(null);
  const playerRef = useRef<HTMLVideoElement | null>(null);
  const pendingSeek = useRef(initialAtS);
  const uploadRef = useRef<AbortController | null>(null);
  const media = useProtectedMedia(video?.media_url, "video");
  const poster = useProtectedMedia(video?.poster_url, "image");
  const canWrite = session.can("review.write");
  const running = !historical && (analysis?.status === "running" || analysis?.status === "queued" || video?.status === "analyzing" || video?.status === "queued");
  const locked = !canWrite || Boolean(busy) || running || historical || bookmarkDirty || movementDirty;
  const workbenchDirty = dirty || drawing || bookmarkDirty || movementDirty || profileDirty || Boolean(busy);
  useEffect(() => { onDirtyChange?.(workbenchDirty); return () => onDirtyChange?.(false); }, [workbenchDirty,onDirtyChange]);
  useEffect(() => { if (!workbenchDirty) return; const protect = (event:BeforeUnloadEvent) => { event.preventDefault(); event.returnValue=""; }; window.addEventListener("beforeunload",protect); return () => window.removeEventListener("beforeunload",protect); }, [workbenchDirty]);

  useEffect(() => { if (initialVideoId) { setSelectedId(initialVideoId); setPinnedAnalysis(initialAnalysisId); pendingSeek.current = initialAtS; setAtS(initialAtS); } }, [initialVideoId, initialAtS, initialAnalysisId]);
  useEffect(() => {
    const controller = new AbortController();
    void reviewApi.videos(controller.signal).then(result => {
      setLibrary(result); setSelectedId(current => current ?? result.videos[0]?.video_id ?? null);
    }).catch(cause => { if (!controller.signal.aborted) setError(explainError(cause)); });
    return () => controller.abort();
  }, [reload]);
  useEffect(() => () => uploadRef.current?.abort(), []);
  useEffect(() => {
    const controller = new AbortController();
    setVideo(null); setAnalysis(null); setPreview(null); setBookmarkRequest(undefined); setDirty(false); setDrawing(false); setDraftPoints([]); setError(null); setStatusError(null);
    if (!selectedId) return () => controller.abort();
    setLoading(true);
    void loadVideoReview(selectedId, controller.signal, pinnedAnalysis).then(([record, result]) => {
      if (controller.signal.aborted) return;
      const configuration = reviewConfiguration(record, result, Boolean(pinnedAnalysis));
      setVideo(record); setZones(configuration.zones); setRules(configuration.rules); setAnalysis(result); setAtS(pendingSeek.current); setMessage(null); const options=defaultAnalysisOptions(undefined,result); setAnalysisOptions(options); setSavedOptions(options);
    }).catch(cause => { if (!controller.signal.aborted) setError(explainError(cause)); }).finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [selectedId, pinnedAnalysis, reload]);
  useEffect(() => {
    if (!selectedId || !running) return;
    const controller = new AbortController(); let timer: ReturnType<typeof setTimeout>;
    const poll = async () => {
      try {
        const [result, record] = await Promise.all([reviewApi.analysis(selectedId, controller.signal), reviewApi.video(selectedId, controller.signal)]);
        if (controller.signal.aborted) return;
        setAnalysis(result); setVideo(record); setStatusError(null);
        if (result.status === "completed") setMessage(`Analysis completed. ${result.events.length} ${result.events.length === 1 ? "finding is" : "findings are"} ready for review.`);
        else if (result.status === "failed") setMessage("Analysis failed. Review the error below before running it again.");
        else if (result.status === "interrupted") setMessage("Analysis was interrupted. Run it again to complete this recording.");
        else if (result.status === "cancelled") setMessage("Analysis was cancelled. Its earlier completed versions remain available.");
        setLibrary(current => current ? { ...current, videos: current.videos.map(row => row.video_id === record.video_id ? record : row) } : current);
      } catch (cause) { if (!controller.signal.aborted) setStatusError(`Processing status could not refresh. ${explainError(cause)}`); }
      finally { if (!controller.signal.aborted) timer = setTimeout(() => void poll(), 2000); }
    };
    timer = setTimeout(() => void poll(), 1000);
    return () => { controller.abort(); clearTimeout(timer); };
  }, [selectedId, running]);

  const currentFrame = useMemo(() => detectionAt(analysis?.detections ?? [], atS), [analysis?.detections, atS]);
  const trails = useMemo(() => trackTrails(analysis?.detections ?? [], atS), [analysis?.detections, atS]);
  const overlayZones = drawing || dirty || preview || !analysis ? zones : analysis.zones ?? [];
  const differentConfiguration = Boolean(analysis && (JSON.stringify(video?.zones) !== JSON.stringify(analysis.zones) || JSON.stringify(video?.rules) !== JSON.stringify(analysis.rules)));
  const events = preview?.events ?? analysis?.events ?? [];
  const model = preview?.model ?? analysis?.model;
  const targetClasses = analysisOptions.mode === "onnx" ? [...new Set(["any", ...(library?.capabilities.analysis_profiles?.models.find(item=>item.mode==="onnx")?.classes??[]), ...(analysis?.model.mode==="onnx"?analysis.detections.flatMap(frame=>frame.objects.map(object=>object.class_name)):[])])] : ["any", "motion"];
  function changeZones(next: ReviewZone[]) { setZones(next); setDirty(true); setPreview(null); }
  function updateRule(id: string, update: Partial<ReviewRule>) { setRules(current => current.map(rule => rule.rule_id === id ? { ...rule, ...update } : rule)); setDirty(true); setPreview(null); }
  function seek(seconds: number) { const value = Math.max(0, Math.min(video?.duration_s ?? seconds, seconds)); pendingSeek.current = value; setAtS(value); if (playerRef.current) { playerRef.current.currentTime = value; playerRef.current.pause(); } }
  function select(id: string) {
    if (dirty || drawing || bookmarkDirty || movementDirty || profileDirty) { setError("Save or discard your configuration, bookmark or movement draft, and queue or reset changed analysis settings before opening another clip."); return; }
    if (busy) return;
    pendingSeek.current = 0; setAtS(0); setPinnedAnalysis(undefined); setSelectedId(id);
  }
  function openBookmark(bookmark: ReviewBookmark) {
    if (workbenchDirty) { setError("Save or discard current edits before opening a saved bookmark."); return; }
    if (analysis?.analysis_id === bookmark.analysis_id) { seek(bookmark.at_s); return; }
    pendingSeek.current=bookmark.at_s; setAtS(bookmark.at_s); setPinnedAnalysis(bookmark.analysis_id);
  }
  async function upload(files: File[]) {
    if (files.length > 20) { setError("Choose up to 20 recordings per upload batch."); if (fileRef.current) fileRef.current.value = ""; return; }
    const controller = new AbortController(); uploadRef.current = controller;
    const originalSelection = selectedRef.current;
    setBusy("upload"); setError(null); setBatch(files.map(file => ({name: file.name, state: "Waiting"})));
    let firstId: string | undefined; let completed = 0;
    try {
      for (const [index, file] of files.entries()) {
        if (controller.signal.aborted) break;
        const state = (value: string) => setBatch(current => current.map((row, i) => i === index ? {...row, state: value} : row));
        state("Checking / uploading"); setMessage(`Preparing recording ${index + 1} of ${files.length}: ${file.name}`);
        try {
          await validateUpload(file, library?.capabilities.max_bytes ?? 104857600, library?.capabilities.max_duration_s ?? 180);
          if (controller.signal.aborted) break;
          const record = await reviewApi.upload(file, controller.signal);
          if (controller.signal.aborted) break;
          setLibrary(current => current ? { ...current, videos: [record, ...current.videos.filter(row => row.video_id !== record.video_id)] } : current);
          firstId ??= record.video_id; completed++; state("Ready");
        } catch (cause) { if (!controller.signal.aborted) state(explainError(cause)); }
      }
      if (controller.signal.aborted) setBatch(current => current.map(row => ["Waiting", "Checking / uploading"].includes(row.state) ? {...row, state: "Cancelled"} : row));
      if (firstId && selectedRef.current === originalSelection) { pendingSeek.current = 0; setPinnedAnalysis(undefined); setSelectedId(firstId); }
      setMessage(`${completed} of ${files.length} recordings prepared. ${controller.signal.aborted ? "Remaining uploads cancelled." : "Save zones and rules on each recording, then submit analyses to the queue."}`);
    } finally { if (uploadRef.current === controller) { uploadRef.current = null; setBusy(null); } if (fileRef.current) fileRef.current.value = ""; }
  }
  function closePolygon() {
    const issue = polygonProblem(draftPoints);
    if (issue) { setError(issue); return; }
    if (!zoneName.trim()) { setError("Give this zone a name."); return; }
    changeZones([...zones, { zone_id: freshId("zone"), name: zoneName.trim(), points: draftPoints }]);
    setDrawing(false); setDraftPoints([]); setZoneName(""); setError(null);
  }
  async function save() {
    if (!video) return;
    const id = video.video_id; setBusy("save"); setError(null);
    try {
      const result = await reviewApi.configure(id, video.revision, zones, rules);
      if (selectedRef.current !== id) return;
      setVideo(result); setDirty(false); setMessage("Configuration saved. Run analysis to create a new recorded result.");
    } catch (cause) { setError(cause instanceof ApiError && cause.status === 409 ? "This configuration changed elsewhere. Reload the saved version before editing again; your draft is still visible." : explainError(cause)); }
    finally { setBusy(null); }
  }
  async function run() {
    if (!video) return; if (optionsProblem) { setError(optionsProblem); return; } setBusy("run"); setError(null); setPreview(null);
    try { const result = await reviewApi.analyze(video.video_id,normalizedAnalysisOptions(analysisOptions)); setSavedOptions(normalizedAnalysisOptions(analysisOptions)); setAnalysis(result); setVideo(current => current ? { ...current, status: "queued", analysis_id: result.analysis_id } : current); setMessage("Analysis queued. Open Processing queue to follow progress or cancel the job."); }
    catch (cause) { setError(explainError(cause)); } finally { setBusy(null); }
  }
  async function previewRules() {
    if (!video) return; setBusy("preview"); setError(null);
    try { setPreview(await reviewApi.preview(video.video_id, zones, rules)); setMessage("Draft preview uses existing detections. Save and run analysis to persist these events."); }
    catch (cause) { setError(explainError(cause)); } finally { setBusy(null); }
  }
  async function createCase(event: VideoEvent) {
    if (!video || !analysis) return; setBusy(event.event_id); setError(null);
    try { const record = await reviewApi.createCase({ video_id: video.video_id, analysis_id: analysis.analysis_id, event_id: event.event_id }); onOpenCase?.(record.case_id); }
    catch (cause) { setError(explainError(cause)); } finally { setBusy(null); }
  }
  return <div className="review-shell">
    <aside className="review-library" aria-label="Recorded footage library">
      <div className="rv-kicker">RECORDED REVIEW</div><h2>Footage</h2><p className="rv-muted">A clip. A defined boundary.<br />An accountable finding.</p>
      <input ref={fileRef} className="rv-file-input" type="file" multiple accept="video/mp4,.mp4" aria-label="Choose MP4 footage" onChange={event => { const files = Array.from(event.target.files ?? []); if (files.length) void upload(files); }} />
      <button className="rv-button rv-primary" disabled={!canWrite || library?.capabilities.upload === false || Boolean(busy) || dirty || drawing || bookmarkDirty || movementDirty || profileDirty} onClick={() => fileRef.current?.click()}>{busy === "upload" ? "Preparing footage…" : "＋ Upload MP4"}</button>
      {busy === "upload" && <button className="rv-button" onClick={() => uploadRef.current?.abort()}>Cancel remaining uploads</button>}
      {batch.length > 0 && <div className="rv-small" role="status" aria-label="Upload batch progress">{batch.map((row, index) => <p key={`${index}-${row.name}`}><strong>{row.name}</strong><br />{row.state}</p>)}{!busy && <button className="rv-button" onClick={() => setBatch([])}>Dismiss upload results</button>}</div>}
      <p className="rv-small">Up to {Math.round((library?.capabilities.max_bytes ?? 104857600) / 1048576)} MB · {library?.capabilities.max_duration_s ?? 180}s<br />Protected playback only</p>
      {library?.capabilities.upload === false && <p className="rv-error">Upload is unavailable on this server. Check its video-processing dependencies.</p>}
      <div className="rv-library-head"><span>{library?.videos.length ?? 0} {library?.videos.length === 1 ? "recording" : "recordings"}</span><button onClick={() => setReload(value => value + 1)} disabled={dirty || drawing || bookmarkDirty || movementDirty || profileDirty || Boolean(busy)}>Refresh</button></div>
      {!library && !error && <p className="rv-muted">Loading recordings…</p>}
      {library?.videos.length === 0 && <p className="rv-empty">Upload a short recording to begin reviewing its events.</p>}
      <div className="rv-library-list">{library?.videos.map(record => <button key={record.video_id} className={`rv-library-item ${selectedId === record.video_id ? "is-selected" : ""}`} disabled={Boolean(busy)} onClick={() => select(record.video_id)} aria-current={selectedId === record.video_id ? "true" : undefined}><strong>{record.title}</strong><span>{formatVideoTime(record.duration_s)} <i>·</i> {record.status}</span></button>)}</div>
      <p className="rv-small rv-library-foot">Recorded-file analysis is separate from the live site. Image zones describe pixels, not ground coordinates.</p>
    </aside>
    <main className="review-main">
      <header className="rv-page-head"><div><span className="rv-kicker">FOOTAGE / INSPECT</span><h1>{video?.title ?? "Review a recording"}</h1></div><div className="rv-head-actions">{video && <span className="rv-badge">{video.privacy === "full_frame_pixelation" ? "Full-frame pixelation" : video.privacy}</span>}<span className="rv-badge">Recorded file</span></div></header>
      {error && <div role="alert" className="rv-error">{error}</div>}{message && <div role="status" className="rv-notice">{message}</div>}
      {loading && <p className="rv-empty">Opening the saved recording…</p>}
      {!video && !loading && <div className="rv-blank"><span>01 / CHOOSE FOOTAGE</span><h2>See what crossed the line.</h2><p>Upload an MP4, mark an area in the image, and review entry or dwell events against the original timing.</p><p className="rv-small">Only a pixelated derivative is available in the console. Original footage has no public playback route.</p></div>}
      {video && <>
        {historical && <div className="rv-notice rv-historical"><span>Historical analysis · {analysis?.analysis_id}. Zones, rules and findings below belong to this saved run. Configuration editing is disabled.</span><button className="rv-button" disabled={bookmarkDirty || movementDirty || Boolean(busy)} onClick={() => { setPinnedAnalysis(undefined); setReload(value => value + 1); }}>Return to latest</button></div>}
        {!historical && differentConfiguration && <p className="rv-notice">Findings and detection overlays show the last analysis run. The current configuration differs; save and run analysis to update findings.</p>}
        <div className="rv-video-layout"><section className="rv-player-section">
          <div className="rv-player" style={{ aspectRatio: `${video.width || 16} / ${video.height || 9}` }}>
            {media.url ? <video ref={playerRef} src={media.url} poster={poster.url ?? undefined} controls={!drawing} playsInline preload="metadata" onLoadedMetadata={() => { if (playerRef.current) playerRef.current.currentTime = pendingSeek.current; }} onTimeUpdate={event => setAtS(event.currentTarget.currentTime)} aria-label="Protected recorded footage" /> : poster.url ? <img src={poster.url} alt="Protected recording preview" /> : <div className="rv-media-placeholder">{media.error ?? "Loading protected playback…"}</div>}
            <svg viewBox="0 0 1000 1000" preserveAspectRatio="none" className={`rv-overlay ${drawing ? "is-drawing" : ""}`} aria-label={drawing ? "Click the image to add polygon points" : "Recorded detection and zone overlays"} onClick={event => {
              if (!drawing) return; if (draftPoints.length >= 20) { setError("A zone can have at most 20 points."); return; }
              const point = normalizedPoint(event.clientX, event.clientY, event.currentTarget.getBoundingClientRect());
              setDraftPoints(current => [...current, point]);
            }}>
              {overlayZones.map(zone => <g key={zone.zone_id}><polygon points={polygonPoints(zone)} className="rv-zone" /><text x={(zone.points[0]?.[0] ?? 0) * 1000 + 8} y={(zone.points[0]?.[1] ?? 0) * 1000 + 24} className="rv-zone-label">{zone.name}</text></g>)}
              {showTrails && !drawing && trails.map(trail => <polyline key={trail.id} points={trail.points} className="rv-trail" />)}
              {showBoxes && !drawing && currentFrame?.objects.map(object => <g key={object.track_id}><rect x={object.bbox[0] * 1000} y={object.bbox[1] * 1000} width={(object.bbox[2] - object.bbox[0]) * 1000} height={(object.bbox[3] - object.bbox[1]) * 1000} className="rv-detection" /><text x={object.bbox[0] * 1000 + 4} y={Math.max(22, object.bbox[1] * 1000 - 8)} className="rv-zone-label">{object.class_name}{object.confidence == null ? "" : ` ${Math.round(object.confidence * 100)}%`}</text></g>)}
              {drawing && <><polyline points={draftPoints.map(([x, y]) => `${x * 1000},${y * 1000}`).join(" ")} className="rv-draft-line" />{draftPoints.map(([x, y], index) => <circle key={index} cx={x * 1000} cy={y * 1000} r="7" className="rv-draft-point" />)}</>}
            </svg>
          </div>
          {media.error && <p className="rv-error">{media.error}</p>}
          <div className="rv-player-meta"><span className="rv-clock">{formatVideoTime(atS)} <small>/ {formatVideoTime(video.duration_s)}</small></span><label><input type="checkbox" checked={showBoxes} onChange={event => setShowBoxes(event.target.checked)} /> Detections</label><label><input type="checkbox" checked={showTrails} onChange={event => setShowTrails(event.target.checked)} /> Recent trails</label></div>
          <FootageNavigation video={video} analysis={analysis} playerRef={playerRef} atS={atS} onSeek={seek} mediaKey={media.url} disabled={!media.url||Boolean(media.error)||loading||Boolean(busy)} drawing={drawing} shortcutGuard={bookmarkDirty||movementDirty||showPresets} onBookmark={canWrite&&analysis?.status==="completed"&&!preview&&!dirty&&!profileDirty&&!bookmarkDirty&&!movementDirty?requestBookmark:undefined} />
          <p className="rv-small">Zone overlay: {drawing || dirty || preview ? "draft configuration" : analysis ? "saved analysis configuration" : "saved configuration"}. Playback is fully pixelated and audio is removed; pixelation is not a guarantee of anonymization.</p>
          <div className="rv-event-track" aria-label="Events along recording">{events.map(event => <button key={event.event_id} style={{ left: `${Math.min(99, event.at_s / Math.max(1, video.duration_s) * 100)}%` }} title={`${formatVideoTime(event.at_s)} · ${event.title}`} aria-label={`Seek to ${event.title} at ${formatVideoTime(event.at_s)}`} onClick={() => seek(event.at_s)} />)}<span className="rv-playhead" style={{ left: `${Math.min(100, atS / Math.max(1, video.duration_s) * 100)}%` }} /></div>
          <div className="rv-model-note"><strong>{model?.mode === "pending" ? "Loading detector" : model?.mode === "onnx" ? model.name : "Motion regions"}</strong><span>{model?.mode === "pending" ? "The active model will be reported when initialization finishes." : model?.mode === "onnx" ? "Object classes come from the active detector." : "Motion is not object recognition. Regions and track IDs do not identify people or object classes."}{model?.warning ? ` ${model.warning}` : ""}</span></div>
        </section>
        <section className="rv-results"><div className="rv-section-title"><div><span className="rv-kicker">{preview ? "DRAFT / PREVIEW EVENTS" : historical ? "SAVED RUN / EVENTS" : "LAST RUN / EVENTS"}</span><h2>{events.length} {events.length === 1 ? "finding" : "findings"}</h2></div>{preview && <span className="rv-badge rv-warning">Draft preview</span>}</div>
          {running && <div className="rv-processing" role="status"><strong>{analysis?.status === "queued" ? "Queued for analysis" : "Processing recording"}</strong><progress max="1" value={analysis?.progress ?? 0} /><span>{Math.round((analysis?.progress ?? 0) * 100)}% processed</span></div>}
          {statusError && <p className="rv-error">{statusError}</p>}
          {analysis?.error && <p className="rv-error">{analysis.error}</p>}
          {!events.length && !running && <p className="rv-empty">{analysis?.status === "completed" ? "No events matched this analysis. Check your zones and enabled rules." : "Save a zone and rule, then run analysis. Events will appear here with their recorded timestamps."}</p>}
          <div className="rv-event-list">{events.map(event => <article key={event.event_id} className={`rv-event-card ${Math.abs(event.at_s - atS) < 1 ? "is-current" : ""}`}><button className="rv-event-seek" onClick={() => seek(event.at_s)}><time>{formatVideoTime(event.at_s)}</time><strong>{event.title}</strong><span>{(preview ? zones : analysis?.zones ?? []).find(zone => zone.zone_id === event.zone_id)?.name ?? event.zone_id} · {event.track_id}</span></button><button className="rv-button" disabled={workbenchDirty || Boolean(preview) || analysis?.status !== "completed" || !session.can("case.write")} onClick={() => void createCase(event)}>{busy === event.event_id ? "Creating…" : "Create case ↗"}</button></article>)}</div>
          {preview && <p className="rv-small">Preview events are temporary. Save and run analysis before creating a case.</p>}
        </section></div>
        <FootageBookmarks key={video.video_id} video={video} analysis={analysis} atS={atS} disabled={dirty||drawing||running||Boolean(busy)||Boolean(preview)||profileDirty||movementDirty} request={bookmarkRequest} onOpen={openBookmark} onDirtyChange={setBookmarkDirty} />
        {analysis?.status === "completed" && <MovementPanel key={analysis.analysis_id} video={video} analysis={analysis} onSeek={seek} onDirtyChange={setMovementDirty} />}
        <AnalysisControls capabilities={library?.capabilities.analysis_profiles} options={analysisOptions} analysis={analysis} disabled={locked||drawing} historical={historical} changed={profileDirty} onChange={setAnalysisOptions} onReset={()=>setAnalysisOptions(savedOptions)} />
        <div className="rv-editor-grid"><section className="rv-editor-card"><div className="rv-section-title"><div><span className="rv-kicker">02 / IMAGE ZONES</span><h2>Mark an area</h2></div><button className="rv-button" disabled={locked || drawing} onClick={() => { playerRef.current?.pause(); setDrawing(true); setDraftPoints([]); setZoneName(`Zone ${zones.length + 1}`); }}>＋ Draw zone</button></div><p className="rv-small">These boundaries use normalized image coordinates. They are not geographic zones or camera calibration.</p>
          {drawing && <div className="rv-drawing-tools"><label>Zone name<input value={zoneName} onChange={event => setZoneName(event.target.value)} maxLength={100} /></label><p className="rv-small">{draftPoints.length}/20 points · click around the boundary in the player, or add exact image coordinates below (0–1).</p><div className="rv-form-grid"><label>Point X<input type="number" min="0" max="1" step="0.01" value={coordinatePoint[0]} onChange={event => setCoordinatePoint(current => [Number(event.target.value), current[1]])} /></label><label>Point Y<input type="number" min="0" max="1" step="0.01" value={coordinatePoint[1]} onChange={event => setCoordinatePoint(current => [current[0], Number(event.target.value)])} /></label></div><button className="rv-button" disabled={draftPoints.length >= 20 || coordinatePoint.some(value => !Number.isFinite(value) || value < 0 || value > 1)} onClick={() => setDraftPoints(current => [...current, [...coordinatePoint]])}>Add coordinate point</button><div className="rv-row"><button className="rv-button" disabled={!draftPoints.length} onClick={() => setDraftPoints(current => current.slice(0, -1))}>Undo point</button><button className="rv-button rv-primary" disabled={draftPoints.length < 3} onClick={closePolygon}>Close polygon</button><button className="rv-button" onClick={() => { setDrawing(false); setDraftPoints([]); }}>Cancel</button></div></div>}
          <div className="rv-zone-list">{zones.map(zone => <div className="rv-zone-row" key={zone.zone_id}><span><strong>{zone.name}</strong><small>{zone.points.length} image points</small></span><button className="rv-text-button" disabled={locked} onClick={() => { changeZones(zones.filter(row => row.zone_id !== zone.zone_id)); setRules(current => current.filter(rule => rule.zone_id !== zone.zone_id)); }}>Delete</button></div>)}</div>{!zones.length && !drawing && <p className="rv-empty">Draw your first area in the paused image above.</p>}
        </section><section className="rv-editor-card"><div className="rv-section-title"><div><span className="rv-kicker">03 / RULES</span><h2>Define a finding</h2></div><button className="rv-button" disabled={locked || !zones.length || drawing} onClick={() => { setRules(current => [...current, { rule_id: freshId("rule"), name: `Rule ${current.length + 1}`, zone_id: zones[0]!.zone_id, event_type: "entry", target_class: "any", threshold_s: 5, cooldown_s: 10, enabled: true }]); setDirty(true); setPreview(null); }}>＋ Add rule</button></div>
          {rules.map(rule => <div className="rv-rule-card" key={rule.rule_id}><div className="rv-rule-head"><label className="rv-inline-label"><input type="checkbox" checked={rule.enabled} disabled={locked} onChange={event => updateRule(rule.rule_id, { enabled: event.target.checked })} /> Enabled</label><button className="rv-text-button" disabled={locked} onClick={() => { setRules(current => current.filter(row => row.rule_id !== rule.rule_id)); setDirty(true); setPreview(null); }}>Remove</button></div><div className="rv-form-grid"><label>Rule name<input value={rule.name} disabled={locked} onChange={event => updateRule(rule.rule_id, { name: event.target.value })} /></label><label>Image zone<select value={rule.zone_id} disabled={locked} onChange={event => updateRule(rule.rule_id, { zone_id: event.target.value })}>{zones.map(zone => <option key={zone.zone_id} value={zone.zone_id}>{zone.name}</option>)}</select></label><label>Trigger<select value={rule.event_type} disabled={locked} onChange={event => updateRule(rule.rule_id, { event_type: event.target.value as ReviewRule["event_type"] })}><option value="entry">Entry into zone</option><option value="dwell">Dwell in zone</option></select></label><label>Target class<select value={rule.target_class} disabled={locked} onChange={event => updateRule(rule.rule_id, { target_class: event.target.value })}>{[...new Set([...targetClasses, rule.target_class])].map(name => <option key={name} value={name}>{name === "any" ? "Any detected region" : name}</option>)}</select></label>{rule.event_type === "dwell" && <label>Dwell threshold (s)<input type="number" min="0" max="180" step="0.5" value={rule.threshold_s} disabled={locked} onChange={event => updateRule(rule.rule_id, { threshold_s: Number(event.target.value) })} /></label>}<label>Cooldown (s)<input type="number" min="0" max="180" value={rule.cooldown_s} disabled={locked} onChange={event => updateRule(rule.rule_id, { cooldown_s: Number(event.target.value) })} /></label></div></div>)}{!rules.length && <p className="rv-empty">Choose an image zone, then add an entry or dwell rule.</p>}
        </section></div>
        <button className="rv-button rp-toggle" disabled={Boolean(busy)} aria-expanded={showPresets} onClick={() => setShowPresets(value => !value)}>{showPresets ? "Hide rule presets" : "Reusable rule presets"}</button>
        {showPresets && <RulePresetsPanel key={video.video_id} sourceVideoId={video.video_id} disabled={historical || dirty || drawing || running || Boolean(busy)} onBusyChange={value => setBusy(value ? "presets" : null)} onApplied={async ids => { if (selectedRef.current && ids.includes(selectedRef.current)) { const record = await reviewApi.video(selectedRef.current); if (record.video_id !== selectedRef.current) return; setVideo(record); setZones(record.zones); setRules(record.rules); setDirty(false); setPreview(null); setLibrary(current => current ? { ...current, videos: current.videos.map(item => item.video_id === record.video_id ? record : item) } : current); } }} />}
        <footer className="rv-save-bar"><span>{historical ? "Historical configuration · read only" : dirty ? "Unsaved configuration" : `Saved configuration · revision ${video.revision}`}</span><div className="rv-row"><button className="rv-button" disabled={locked || drawing || !analysis?.detections.length || !rules.length} onClick={() => void previewRules()}>{busy === "preview" ? "Previewing…" : "Preview draft"}</button><button className="rv-button" disabled={historical || Boolean(busy) || running || movementDirty || bookmarkDirty || (!dirty && !drawing)} onClick={() => { setZones(video.zones); setRules(video.rules); setDirty(false); setDrawing(false); setDraftPoints([]); setPreview(null); setReload(value => value + 1); }}>Discard / reload</button><button className="rv-button" disabled={locked || drawing || !dirty} onClick={() => void save()}>{busy === "save" ? "Saving…" : "Save configuration"}</button><button className="rv-button rv-primary" disabled={locked || dirty || drawing || Boolean(optionsProblem) || !rules.some(rule => rule.enabled)} onClick={() => void run()}>{running ? "Analysis running…" : "Run analysis"}</button></div></footer>
      </>}
    </main>
  </div>;
}
