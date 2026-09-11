import { useEffect, useMemo, useRef, useState } from 'react';
import type { MouseEvent } from 'react';
import { explainError } from '../lib/api';
import { useProtectedMedia } from '../lib/review-media';
import { bookmarkScope } from '../lib/review-bookmarks';
import * as session from '../lib/session';
import type { NormalizedPoint, ReviewVideo, VideoAnalysis } from '../lib/review-types';
import { busiestSamples, countingLineLabels, downloadMovement, initialMovement, movementApi, movementKey, movementProblem, movementTime, reverseCountingLine, type MovementConfiguration, type MovementReport, type MovementSummary } from '../lib/movement';
import './movement.css';

export interface MovementPanelProps { video: ReviewVideo; analysis: VideoAnalysis; onSeek: (atS: number) => void; onDirtyChange?: (dirty: boolean) => void }
const defaultTitle = 'Movement observations';
const pageSize = 12;

export function MovementPanel(props: MovementPanelProps) {
  const identity = session.useSession();
  return identity ? <ScopedMovementPanel key={bookmarkScope(identity)} {...props} /> : null;
}
function ScopedMovementPanel({ video, analysis, onSeek, onDirtyChange }: MovementPanelProps) {
  const [configuration, setConfiguration] = useState<MovementConfiguration>(initialMovement);
  const [title, setTitle] = useState(defaultTitle), [reports, setReports] = useState<MovementReport[]>([]);
  const [selected, setSelected] = useState<MovementReport | null>(null), [summary, setSummary] = useState<MovementSummary | null>(null);
  const [drawing, setDrawing] = useState(false), [firstPoint, setFirstPoint] = useState<NormalizedPoint | null>(null);
  const [loading, setLoading] = useState(true), [listError, setListError] = useState<string | null>(null), [truncated, setTruncated] = useState(false);
  const [error, setError] = useState<string | null>(null), [notice, setNotice] = useState(''), [busy, setBusy] = useState('');
  const [occupancyPage, setOccupancyPage] = useState(0), [crossingPage, setCrossingPage] = useState(0), [reload, setReload] = useState(0);
  const operation = useRef<AbortController | null>(null), epoch = useRef(0), mounted = useRef(true), dirtyCallback = useRef(onDirtyChange);
  const listRequest = useRef<AbortController | null>(null), listEpoch = useRef(0);
  dirtyCallback.current = onDirtyChange;
  const baseline = selected?.configuration ?? initialMovement(), baselineTitle = selected?.title ?? defaultTitle;
  const dirty = movementKey(configuration) !== movementKey(baseline) || title !== baselineTitle || drawing;
  const problem = movementProblem(configuration, analysis.zones);
  const summaryMatches = Boolean(summary && movementKey(summary.configuration) === movementKey(configuration));
  const canWrite = session.can('review.write');
  const classes = useMemo(() => [...new Set(analysis.detections.flatMap(frame => frame.objects.map(object => object.class_name)))].sort(), [analysis]);
  const media = useProtectedMedia(analysis.detections[0]?.frame_url ?? video.poster_url, 'image');
  const labels = configuration.line ? countingLineLabels(configuration.line) : null;
  const peaks = summaryMatches && summary ? busiestSamples(summary.occupancy) : [];
  const duration = Math.max(video.duration_s, summary?.occupancy.at(-1)?.at_s ?? 0, .001);
  const plotMax = Math.max(1, summary?.peak_count ?? 1);

  useEffect(() => { onDirtyChange?.(dirty || Boolean(busy)); }, [dirty, busy, onDirtyChange]);
  useEffect(() => {
    mounted.current = true;
    return () => { mounted.current = false; epoch.current++; operation.current?.abort(); dirtyCallback.current?.(false); };
  }, []);
  useEffect(() => {
    const controller = new AbortController(), version = ++listEpoch.current; listRequest.current = controller; setLoading(true); setListError(null);
    void movementApi.list(video.video_id, analysis.analysis_id, controller.signal).then(result => {
      if (controller.signal.aborted || version !== listEpoch.current) return;
      setReports(result.reports); setTruncated(result.possibly_truncated);
    }).catch(cause => { if (!controller.signal.aborted) setListError(explainError(cause)); }).finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [video.video_id, analysis.analysis_id, reload]);

  function update(patch: Partial<MovementConfiguration>) {
    setConfiguration(current => ({ ...current, ...patch })); setSummary(null); setError(null); setNotice('');
    setOccupancyPage(0); setCrossingPage(0);
  }
  function restore(report: MovementReport | null) {
    setSelected(report); setConfiguration(structuredClone(report?.configuration ?? initialMovement()));
    setTitle(report?.title ?? defaultTitle); setSummary(report?.summary ?? null); setDrawing(false); setFirstPoint(null);
    setError(null); setNotice(''); setOccupancyPage(0); setCrossingPage(0);
  }
  function coordinate(endpoint: 'start' | 'end', axis: 0 | 1, value: number) {
    if (!configuration.line) return;
    const point: NormalizedPoint = [...configuration.line[endpoint]]; point[axis] = value;
    update({ line: { ...configuration.line, [endpoint]: point } });
  }
  function drawPoint(event: MouseEvent<SVGSVGElement>) {
    if (!drawing || busy || !media.url) return;
    const box = event.currentTarget.getBoundingClientRect();
    const point: NormalizedPoint = [Math.max(0, Math.min(1, (event.clientX - box.left) / box.width)), Math.max(0, Math.min(1, (event.clientY - box.top) / box.height))];
    if (!firstPoint) { setFirstPoint(point); return; }
    const next = { ...configuration, line: { name: configuration.line?.name ?? 'Counting line', start: firstPoint, end: point } };
    const invalid = movementProblem(next, analysis.zones);
    if (invalid) { setError(invalid); return; }
    update({ line: next.line }); setFirstPoint(null); setDrawing(false);
  }
  async function run(kind: 'preview' | 'save' | 'download') {
    if (busy || operation.current) return;
    if (kind !== 'download' && !canWrite) { setError('Calculating or saving movement observations requires review write access.'); return; }
    if (kind !== 'download' && (problem || drawing)) { setError(problem ?? 'Finish or cancel the counting line first.'); return; }
    if (kind === 'save' && (!title.trim() || title.length > 120)) { setError('Name the snapshot using 1–120 characters.'); return; }
    const currentEpoch = ++epoch.current, controller = new AbortController(); operation.current = controller;
    setBusy(kind); setError(null); setNotice('');
    const current = () => mounted.current && currentEpoch === epoch.current && !controller.signal.aborted;
    try {
      if (kind === 'download') {
        if (!selected || dirty) throw new Error('Save or discard changes before exporting a saved snapshot.');
        await downloadMovement(selected.report_id, controller.signal);
        if (current()) setNotice('Downloaded CSV for the selected saved snapshot.');
      } else if (kind === 'preview') {
        const result = await movementApi.preview(video.video_id, analysis.analysis_id, configuration, controller.signal);
        if (!current()) return;
        setSummary(result); setConfiguration(result.configuration); setOccupancyPage(0); setCrossingPage(0);
        setNotice('Preview calculated from this retained analysis. Save a snapshot to keep or export it.');
      } else {
        if (!canWrite || !summaryMatches) throw new Error('Calculate the current preview before saving a snapshot.');
        const result = await movementApi.save(video.video_id, analysis.analysis_id, configuration, title, controller.signal);
        if (!current()) return;
        listEpoch.current++; listRequest.current?.abort(); setLoading(false); setListError(null);
        setReports(items => [result, ...items.filter(item => item.report_id !== result.report_id)]); restore(result);
        setNotice('Snapshot saved with its line, filters, analysis and model.');
      }
    } catch (cause) { if (current()) setError(explainError(cause)); }
    finally { if (current()) { setBusy(''); operation.current = null; } }
  }
  function pager(page: number, length: number, change: (value: number) => void, label: string) {
    if (length <= pageSize) return null;
    return <div className="mv-pager"><button className="rv-button" disabled={!page} aria-label={`Previous ${label}`} onClick={() => change(page - 1)}>Previous</button><span>{page * pageSize + 1}–{Math.min((page + 1) * pageSize, length)} of {length}</span><button className="rv-button" disabled={(page + 1) * pageSize >= length} aria-label={`Next ${label}`} onClick={() => change(page + 1)}>Next</button></div>;
  }

  return <section className="mv-panel rv-editor-card" aria-label="Movement and occupancy">
    <header className="rv-section-title"><div><span className="rv-kicker">MOVEMENT / RETAINED OBSERVATIONS</span><h2>Count what passed through.</h2></div><span className={`rv-badge ${dirty ? 'rv-warning' : selected ? 'rv-positive' : ''}`}>{dirty ? 'Unsaved changes' : selected ? 'Saved snapshot' : summary ? 'Preview' : 'New snapshot'}</span></header>
    <p className="rv-muted">Count tracked crossings and objects visible in saved samples. These observations do not identify unique people or establish an incident.</p>
    <div className="mv-saved-row"><label>Saved movement snapshot<select value={selected?.report_id ?? ''} disabled={dirty || Boolean(busy) || loading} onChange={event => restore(reports.find(item => item.report_id === event.target.value) ?? null)}><option value="">New snapshot</option>{reports.map(report => <option key={report.report_id} value={report.report_id}>{report.title} · {new Date(report.created_at).toLocaleString()}</option>)}</select></label><button className="rv-button" disabled={Boolean(busy) || loading} onClick={() => setReload(value => value + 1)}>Refresh snapshots</button></div>
    {loading && <p className="rv-small" role="status">Loading saved snapshots…</p>}{listError && <p className="rv-error" role="alert">{listError}</p>}{truncated && <p className="rv-small">The snapshot list reached its server limit; some saved snapshots may not be listed.</p>}
    <fieldset className="rv-fieldset" disabled={Boolean(busy) || !canWrite}>
      <div className="mv-config-grid"><div className="mv-line-editor"><div className="mv-frame" style={{ aspectRatio: `${video.width || 16} / ${video.height || 9}` }}>
        {media.url ? <img src={media.url} alt="Protected saved frame for positioning a counting line" /> : <p className={media.error ? 'rv-error' : 'rv-small'}>{media.error ?? 'Loading protected frame…'}</p>}
        <svg viewBox="0 0 1000 1000" preserveAspectRatio="none" className={drawing ? 'is-drawing' : ''} onClick={drawPoint} role="img" aria-label={drawing ? firstPoint ? 'Choose the second counting line endpoint' : 'Choose the first counting line endpoint' : 'Counting line and retained zone overlay'}>
          {analysis.zones.filter(zone => zone.zone_id === configuration.zone_id).map(zone => <polygon key={zone.zone_id} points={zone.points.map(point => `${point[0] * 1000},${point[1] * 1000}`).join(' ')} className="mv-zone" />)}
          {configuration.line && labels && <><line x1={configuration.line.start[0] * 1000} y1={configuration.line.start[1] * 1000} x2={configuration.line.end[0] * 1000} y2={configuration.line.end[1] * 1000} className="mv-counting-line" /><circle cx={configuration.line.start[0] * 1000} cy={configuration.line.start[1] * 1000} r="10" className="mv-endpoint" /><rect x={configuration.line.end[0] * 1000 - 10} y={configuration.line.end[1] * 1000 - 10} width="20" height="20" className="mv-endpoint" /><text x={labels.a[0] * 1000} y={labels.a[1] * 1000} className="mv-side">A</text><text x={labels.b[0] * 1000} y={labels.b[1] * 1000} className="mv-side">B</text></>}
          {firstPoint && <circle cx={firstPoint[0] * 1000} cy={firstPoint[1] * 1000} r="12" className="mv-first-point" />}
        </svg>
      </div><div className="rv-row mv-line-actions"><button className="rv-button" disabled={!media.url || drawing} onClick={() => { setDrawing(true); setFirstPoint(null); setError(null); }}>Draw counting line</button>{drawing && <button className="rv-button" onClick={() => { setDrawing(false); setFirstPoint(null); setError(null); }}>Cancel drawing</button>}<button className="rv-button" disabled={!configuration.line || drawing} onClick={() => configuration.line && update({ line: reverseCountingLine(configuration.line) })}>Reverse A / B</button><button className="rv-button" disabled={!configuration.line || drawing} onClick={() => update({ line: null })}>Remove line</button></div>
        <p className="rv-small" role={drawing ? 'status' : undefined}>{drawing ? firstPoint ? 'First endpoint set. Click the second endpoint; only this finite segment counts crossings.' : 'Click two endpoints on the saved frame. You can also enter coordinates below.' : 'Circle = start; square = end. A is left of start → end, B is right. Without a line, only occupancy is calculated.'}</p>
        <details className="mv-coordinates"><summary>Set line coordinates without drawing</summary>{!configuration.line ? <button className="rv-button" disabled={drawing} onClick={() => update({ line: { name: 'Counting line', start: [.5, .1], end: [.5, .9] } })}>Add line using coordinates</button> : <><label>Counting line name<input value={configuration.line.name} maxLength={100} disabled={drawing} onChange={event => configuration.line && update({ line: { ...configuration.line, name: event.target.value } })} /></label><div className="mv-coordinate-grid">{(['start', 'end'] as const).flatMap(endpoint => ([0, 1] as const).map(axis => <label key={`${endpoint}-${axis}`}>{endpoint === 'start' ? 'Start' : 'End'} {axis === 0 ? 'X' : 'Y'} (0–1)<input type="number" min="0" max="1" step="0.01" disabled={drawing} value={Number.isFinite(configuration.line![endpoint][axis]) ? configuration.line![endpoint][axis] : ''} onChange={event => coordinate(endpoint, axis, event.target.valueAsNumber)} /></label>))}</div><p className="rv-small">X runs left to right; Y runs top to bottom. Coordinates are fractions of the full frame.</p></>}</details>
      </div><div className="mv-filters"><label>Movement object class<select value={configuration.class_name} onChange={event => update({ class_name: event.target.value })}><option value="">All observed classes</option>{classes.map(name => <option value={name} key={name}>{name}</option>)}</select></label><label>Movement zone<select value={configuration.zone_id} onChange={event => update({ zone_id: event.target.value })}><option value="">Whole frame</option>{analysis.zones.map(zone => <option value={zone.zone_id} key={zone.zone_id}>{zone.name}</option>)}</select></label><label className="mv-inline-check"><input type="checkbox" checked={configuration.min_confidence !== null} onChange={event => update({ min_confidence: event.target.checked ? .5 : null })} />Filter by confidence</label>{configuration.min_confidence !== null && <label>Movement minimum confidence<input type="number" min="0" max="1" step="0.05" value={Number.isFinite(configuration.min_confidence) ? configuration.min_confidence : ''} onChange={event => update({ min_confidence: event.target.valueAsNumber })} /></label>}<p className="rv-small">Confidence filtering excludes observations with no confidence score. Filters use this analysis’s saved classes and zones.</p><label>Movement snapshot title<input value={title} maxLength={120} onChange={event => { setTitle(event.target.value); setNotice(''); }} /></label><div className="mv-provenance"><span>Source analysis</span><strong>{analysis.model.name}</strong><code>{analysis.analysis_id}</code><small>{typeof analysis.model.sample_fps === 'number' && Number.isFinite(analysis.model.sample_fps) && analysis.model.sample_fps > 0 ? `${analysis.model.sample_fps} samples / second` : 'Sampling rate not recorded for this analysis'}</small></div></div></div>
      {problem && <p className="rv-error">{problem}</p>}<div className="rv-row mv-form-actions"><button className="rv-button rv-primary" disabled={Boolean(problem) || drawing} onClick={() => void run('preview')}>{busy === 'preview' ? 'Calculating…' : 'Calculate preview'}</button><button className="rv-button" disabled={!canWrite || !summaryMatches || Boolean(problem) || drawing || !title.trim() || Boolean(selected && !dirty)} onClick={() => void run('save')}>{busy === 'save' ? 'Saving…' : 'Save snapshot'}</button></div>
    </fieldset>
    <div className="rv-row mv-secondary-actions"><button className="rv-button" disabled={!selected || dirty || Boolean(busy)} onClick={() => void run('download')}>{busy === 'download' ? 'Downloading…' : 'Download saved CSV'}</button><button className="rv-button" disabled={!dirty || Boolean(busy)} onClick={() => restore(selected)}>Discard movement changes</button></div>
    {dirty && <p className="rv-small">Save or discard movement changes before switching snapshots or leaving this recording. CSV export uses the selected saved snapshot.</p>}{!canWrite && <p className="rv-small">Your role can read and export saved snapshots. Configuring, calculating previews and saving require review write access.</p>}
    {error && <p className="rv-error" role="alert">{error}</p>}{notice && <p className="rv-notice" role="status">{notice}</p>}
    {summaryMatches && summary && <div className="mv-summary" aria-label="Movement calculation results"><div className="mv-metrics"><div><span>Peak observed occupancy</span><strong>{summary.peak_count}</strong><small>{summary.peak_at_s == null ? 'No saved samples' : <button className="mv-time" onClick={() => onSeek(summary.peak_at_s!)}>{movementTime(summary.peak_at_s)}</button>}</small></div><div><span>Mean across samples</span><strong>{summary.mean_sampled_occupancy == null ? '—' : summary.mean_sampled_occupancy.toFixed(2)}</strong><small>{summary.sample_count} saved samples</small></div><div><span>A → B crossings</span><strong>{configuration.line ? summary.totals.a_to_b : '—'}</strong><small>{configuration.line?.name ?? 'No counting line'}</small></div><div><span>B → A crossings</span><strong>{configuration.line ? summary.totals.b_to_a : '—'}</strong><small>Tracked passage observations</small></div></div>
      {selected && !dirty && <p className="rv-small mv-report-id">Saved by {selected.created_by} · {new Date(selected.created_at).toLocaleString()} · <code>{selected.report_id}</code></p>}
      <h3>Occupancy at saved samples</h3><p className="rv-small">Each mark is one observation. Gaps between samples are unobserved; the chart does not imply continuous occupancy.</p>
      {summary.occupancy.length > 0 ? <><div className="mv-chart"><svg viewBox="0 0 700 160" role="img" aria-label={`Sampled occupancy: peak ${summary.peak_count}, ${summary.sample_count} saved samples`}><line x1="25" y1="135" x2="680" y2="135" className="mv-chart-axis" /><text x="3" y="20" className="mv-chart-label">{plotMax}</text><text x="5" y="139" className="mv-chart-label">0</text>{summary.occupancy.map((sample, index) => <g key={`${sample.frame_index}-${index}`}><title>{movementTime(sample.at_s)}: {sample.count} observed objects</title><line x1={25 + sample.at_s / duration * 650} x2={25 + sample.at_s / duration * 650} y1="135" y2={135 - sample.count / plotMax * 115} className="mv-chart-stem" /><circle cx={25 + sample.at_s / duration * 650} cy={135 - sample.count / plotMax * 115} r="3" className="mv-chart-dot" /></g>)}<text x="25" y="155" className="mv-chart-label">0:00</text><text x="680" y="155" textAnchor="end" className="mv-chart-label">{movementTime(duration)}</text></svg></div>{peaks.length > 0 && <div className="mv-peaks"><span>Busiest sampled moments</span>{peaks.map(sample => <button key={sample.frame_index} className="rv-button" onClick={() => onSeek(sample.at_s)}>{movementTime(sample.at_s)} · {sample.count} observed</button>)}</div>}<details className="mv-table-details"><summary>Inspect every occupancy sample ({summary.occupancy.length})</summary><div className="mv-table-wrap"><table><caption>Objects visible at each retained analysis sample</caption><thead><tr><th scope="col">Sample time</th><th scope="col">Observed objects</th></tr></thead><tbody>{summary.occupancy.slice(occupancyPage * pageSize, (occupancyPage + 1) * pageSize).map((sample, index) => <tr key={`${sample.frame_index}-${index}`}><td><button className="mv-time" onClick={() => onSeek(sample.at_s)}>{movementTime(sample.at_s)}</button></td><td>{sample.count}</td></tr>)}</tbody></table></div>{pager(occupancyPage, summary.occupancy.length, setOccupancyPage, 'occupancy samples')}</details></> : <p className="rv-empty">No saved samples are available for this calculation.</p>}
      <h3>Crossing observations</h3>{!configuration.line ? <p className="rv-small">Add a counting line to calculate crossings in each direction.</p> : !summary.crossings.length ? <p className="rv-small">No qualifying tracked crossings were observed across this line.</p> : <><p className="rv-small">The crossing happened within the displayed sample interval. Seek opens the later saved sample; exact crossing time is not measured.</p><div className="mv-table-wrap"><table><caption>{summary.totals.total} tracked crossing observations</caption><thead><tr><th scope="col">Observed interval</th><th scope="col">Direction</th><th scope="col">Object / track</th></tr></thead><tbody>{summary.crossings.slice(crossingPage * pageSize, (crossingPage + 1) * pageSize).map((crossing, index) => <tr key={`${crossing.track_id}-${crossing.at_s}-${index}`}><td><button className="mv-time" onClick={() => onSeek(crossing.to_s)}>{movementTime(crossing.from_s)}–{movementTime(crossing.to_s)}</button></td><td>{crossing.direction === 'a_to_b' ? 'A → B' : 'B → A'}</td><td>{crossing.class_name}<small>{crossing.track_id}</small></td></tr>)}</tbody></table></div>{pager(crossingPage, summary.crossings.length, setCrossingPage, 'crossing observations')}</>}
      <details className="mv-limitations"><summary>How to interpret these counts</summary><ul>{summary.limitations.map((limitation, index) => <li key={index}>{limitation}</li>)}</ul></details>
    </div>}
  </section>;
}
