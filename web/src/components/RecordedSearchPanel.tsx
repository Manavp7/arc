import { useEffect, useRef, useState, type FormEvent } from 'react';
import { explainError } from '../lib/api';
import { footageTimestamp } from '../lib/footage-navigation';
import { bookmarkScope, bookmarksApi } from '../lib/review-bookmarks';
import { reviewApi } from '../lib/review-api';
import { useProtectedMedia } from '../lib/review-media';
import { recordedFramePath, recordedSearchApi, sampleReference, similarityLabel, type RecordedMatch, type RecordedSample, type RecordedSearchCatalog, type RecordedSearchPage } from '../lib/recorded-search';
import * as session from '../lib/session';
import './review.css';
import './recorded-search.css';

interface Props { onOpenVideo: (videoId: string, atS: number, analysisId: string) => void; onOpenCase?: (caseId: string) => void }
export function RecordedSearchPanel(props: Props) {
  const identity = session.useSession();
  return identity ? <ScopedRecordedSearch key={bookmarkScope(identity)} {...props} /> : null;
}

function MatchCard({ row, onOpenVideo, onOpenCase, onSimilar }: Props & { row: RecordedMatch; onSimilar: (row: RecordedMatch) => void }) {
  const canBookmark = session.can('review.write'), canCreateCase = session.can('case.write');
  const media = useProtectedMedia(recordedFramePath(row), 'image');
  const [busy, setBusy] = useState(false), [message, setMessage] = useState('');
  const [eventId, setEventId] = useState(row.events[0]?.event_id ?? '');
  const active = useRef(true);
  useEffect(() => { active.current = true; return () => { active.current = false; }; }, []);
  async function bookmark() {
    setBusy(true); setMessage('');
    try {
      await bookmarksApi.create(row.video_id, row.analysis_id, { at_s: row.at_s, title: `Visual search lead at ${footageTimestamp(row.at_s)}`, note: 'Saved for human review from CLIP visual similarity search. This is not a confirmed detection or incident.' });
      setMessage('Private review bookmark saved.');
    } catch (error) { setMessage(explainError(error)); } finally { setBusy(false); }
  }
  async function makeCase() {
    if (!eventId) return;
    setBusy(true); setMessage('');
    try {
      const result = await reviewApi.createCase({ video_id: row.video_id, analysis_id: row.analysis_id, event_id: eventId });
      if (!active.current) return;
      if (onOpenCase) onOpenCase(result.case_id); else setMessage(`Case ${result.case_id} created.`);
    } catch (error) { setMessage(explainError(error)); } finally { setBusy(false); }
  }
  return <article className="rs-match">
    <button className="rs-image" onClick={() => onOpenVideo(row.video_id, row.at_s, row.analysis_id)} aria-label={`Open ${row.video_title} at ${footageTimestamp(row.at_s)}`}>
      {media.url ? <img src={media.url} alt={`Pixelated sample from ${row.video_title}`} /> : <span>{media.error ?? 'Loading protected sample…'}</span>}
      <time>{footageTimestamp(row.at_s)} ↗</time>
    </button>
    <div className="rs-match-body"><div className="rv-row"><h3>{row.video_title}</h3><span className="rv-badge" title="Cosine similarity, not confidence or probability">Similarity {similarityLabel(row.similarity)}</span></div>
      <p className="rv-small rs-id">{row.analysis_id}</p>
      <div className="rv-row"><button className="rv-button rv-primary" onClick={() => onOpenVideo(row.video_id, row.at_s, row.analysis_id)}>Open footage ↗</button><button className="rv-button" onClick={() => onSimilar(row)}>Find similar</button>{canBookmark && <button className="rv-button" disabled={busy} onClick={() => void bookmark()}>Save bookmark</button>}</div>
      {row.events.length > 0 ? <div className="rs-case">{canCreateCase ? <><label>Existing event at this moment<select value={eventId} onChange={event => setEventId(event.target.value)}>{row.events.map(event => <option key={event.event_id} value={event.event_id}>{event.event_type} · {event.event_id.slice(-8)}</option>)}</select></label><button className="rv-button" disabled={busy || !eventId} onClick={() => void makeCase()}>Create case from event</button><p className="rv-small">Case evidence uses this retained event, not the similarity score.</p></> : <p className="rv-small">{row.events.length} saved event{row.events.length === 1 ? '' : 's'} overlap this moment. Open footage to review them.</p>}</div> : <p className="rv-small">No saved detector event overlaps this sample. Open footage to review, then use frozen human annotations to build a case.</p>}
      {message && <p className="rv-notice" role="status">{message}</p>}
    </div>
  </article>;
}

function ScopedRecordedSearch({ onOpenVideo, onOpenCase }: Props) {
  const canIndex = session.can('review.write');
  const [catalog, setCatalog] = useState<RecordedSearchCatalog | null>(null);
  const [catalogError, setCatalogError] = useState(''), [queryError, setQueryError] = useState('');
  const [revision, setRevision] = useState(0), [busy, setBusy] = useState(false), [searching, setSearching] = useState(false);
  const [videoId, setVideoId] = useState(''), [analysisId, setAnalysisId] = useState(''), [consent, setConsent] = useState(false);
  const [description, setDescription] = useState(''), [scope, setScope] = useState('');
  const [page, setPage] = useState<RecordedSearchPage | null>(null), [applied, setApplied] = useState('');
  const queryAbort = useRef<AbortController | null>(null);
  const querySerial = useRef(0);
  const selected = catalog?.videos.find(video => video.video_id === videoId);
  const selectedAnalysis = analysisId || selected?.analyses[0]?.analysis_id || '';
  useEffect(() => {
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout> | undefined;
    void recordedSearchApi.catalog(controller.signal).then(next => {
      if (controller.signal.aborted) return;
      setCatalog(next); setCatalogError('');
      if (next.busy) timer = setTimeout(() => setRevision(value => value + 1), 2500);
    }).catch(error => { if (!controller.signal.aborted) setCatalogError(explainError(error)); });
    return () => { controller.abort(); clearTimeout(timer); };
  }, [revision]);
  useEffect(() => {
    if (!catalog) return;
    const video = catalog.videos.find(row => row.video_id === videoId);
    if (videoId && !video) { setVideoId(''); setAnalysisId(''); setConsent(false); }
    else if (analysisId && !video?.analyses.some(row => row.analysis_id === analysisId)) { setAnalysisId(''); setConsent(false); }
    if (scope && !catalog.videos.some(row => row.video_id === scope)) setScope('');
  }, [catalog, videoId, analysisId, scope]);
  useEffect(() => () => { querySerial.current++; queryAbort.current?.abort(); }, []);
  async function index() {
    if (!videoId || !selectedAnalysis || !consent) return;
    setBusy(true); setCatalogError('');
    try { await recordedSearchApi.index(videoId, selectedAnalysis, consent); setConsent(false); setPage(null); setRevision(value => value + 1); }
    catch (error) { setCatalogError(explainError(error)); } finally { setBusy(false); }
  }
  async function remove() {
    if (!videoId) return;
    setBusy(true); setCatalogError('');
    try { await recordedSearchApi.remove(videoId); setPage(null); setRevision(value => value + 1); }
    catch (error) { setCatalogError(explainError(error)); } finally { setBusy(false); }
  }
  async function search(sample: RecordedSample | null = null, label = description.trim()) {
    queryAbort.current?.abort(); const controller = new AbortController(); queryAbort.current = controller;
    const serial = ++querySerial.current;
    const scopeLabel = catalog?.videos.find(video => video.video_id === scope)?.title ?? 'All accessible indexed recordings';
    setSearching(true); setQueryError(''); setPage(null); setApplied(`${label} · ${scopeLabel}`);
    try { const response = await recordedSearchApi.query(description, sample, scope, controller.signal); if (serial === querySerial.current && !controller.signal.aborted) setPage(response); }
    catch (error) { if (serial === querySerial.current && !controller.signal.aborted) setQueryError(explainError(error)); }
    finally { if (serial === querySerial.current && !controller.signal.aborted) { setSearching(false); setRevision(value => value + 1); } }
  }
  function submit(event: FormEvent) { event.preventDefault(); void search(); }
  const ready = catalog?.videos.filter(video => video.index?.status === 'ready').length ?? 0;
  return <main className="review-main rs-panel">
    <header className="rv-page-head"><div><span className="rv-kicker">RECORDED FOOTAGE / VISUAL SEARCH</span><h1>Describe a moment. Find its footage.</h1><p className="rv-muted">Search local recording samples with CLIP, then review the exact retained analysis.</p></div><button className="rv-button" onClick={() => setRevision(value => value + 1)}>Refresh index status</button></header>
    {catalogError && <p className="rv-error" role="alert">{catalogError}</p>}
    {catalog && !catalog.model.available && <p className="rv-notice">Visual search needs the pinned local CLIP model. {catalog.model.reason}</p>}
    <section className="rv-editor-card rs-index" aria-label="Recording search index"><div className="rv-section-title"><div><span className="rv-kicker">01 / CHOOSE WHAT TO INDEX</span><h2>{ready} searchable recording{ready === 1 ? '' : 's'}</h2></div>{catalog?.busy && <span className="rv-badge rv-warning">Local indexing in progress</span>}</div>
      <div className="rs-fields"><label>Recording<select value={videoId} onChange={event => { setVideoId(event.target.value); setAnalysisId(''); setConsent(false); }}><option value="">Choose a retained recording</option>{catalog?.videos.map(video => <option key={video.video_id} value={video.video_id}>{video.title} · {video.index?.status ?? 'not indexed'}</option>)}</select></label><label>Retained analysis<select value={selectedAnalysis} disabled={!selected} onChange={event => { setAnalysisId(event.target.value); setConsent(false); }}>{!selected && <option value="">Choose a recording first</option>}{selected?.analyses.map(analysis => <option key={analysis.analysis_id} value={analysis.analysis_id}>{new Date(analysis.created_at).toLocaleString()} · {analysis.analysis_id.slice(-8)}</option>)}</select></label></div>
      {selected?.index && <div className="rs-status"><strong>{selected.index.status} · {Math.round(selected.index.progress * 100)}%</strong><span>{selected.index.sample_count} samples · one index per recording</span>{selected.index.error && <p className="rv-error">{selected.index.error}</p>}</div>}
      {canIndex ? <><label className="rs-consent"><input type="checkbox" checked={consent} onChange={event => setConsent(event.target.checked)} />Allow this recording's private original to be processed locally for visual search. Search embeddings stay private; shared previews remain pixelated.</label>
      <div className="rv-row"><button className="rv-button rv-primary" disabled={busy || !consent || !selectedAnalysis || !catalog?.model.available || catalog.busy} onClick={() => void index()}>{selected?.index ? 'Rebuild selected index' : 'Index selected recording'}</button><button className="rv-button" disabled={busy || !selected?.index || selected.index.status === 'removed' || catalog?.busy} onClick={() => void remove()}>Remove search index</button></div></> : <p className="rv-small">You can search existing permitted indexes. An operator can prepare or remove recording indexes.</p>}
      <p className="rv-small">Opt-in processing samples at most one frame every two seconds, up to 90 frames per recording. Rebuilding replaces only this recording's search index. Original media and saved evidence are preserved.</p>
    </section>
    <form className="rv-editor-card rs-query" onSubmit={submit}><span className="rv-kicker">02 / SEARCH INDEXED MOMENTS</span><div className="rs-fields"><label>Describe what to find<input value={description} maxLength={300} onChange={event => setDescription(event.target.value)} placeholder="A red truck near a gate" /></label><label>Search scope<select value={scope} onChange={event => setScope(event.target.value)}><option value="">All accessible indexed recordings</option>{catalog?.videos.map(video => <option key={video.video_id} value={video.video_id}>{video.title}</option>)}</select></label></div><button className="rv-button rv-primary" disabled={searching || !description.trim() || !catalog?.model.available || catalog.busy || !ready}>Search footage</button></form>
    <section aria-label="Visual search results" aria-busy={searching}><div className="rv-section-title"><h2>{searching ? 'Comparing local samples…' : page ? `${page.results.length} ranked moments` : 'Follow a visual lead'}</h2>{applied && <span className="rv-small">{applied}</span>}</div>{queryError && <p className="rv-error" role="alert">{queryError}</p>}<p className="rv-small">{page?.note ?? catalog?.note ?? 'Similarity is a retrieval aid. Review the footage before making an incident judgment.'}</p>
      {page && <p className="rv-small">Compared {page.total_samples} samples across {page.searched_recordings} recordings. {page.skipped_recordings} recordings had no current accessible index.{page.possibly_truncated ? ' The recording scan reached its bound.' : ''}</p>}
      {page && !page.results.length && <div className="rv-blank"><h3>No indexed samples in this scope.</h3><p>Choose an indexed recording or rebuild a stale index.</p></div>}
      <div className="rs-results">{page?.results.map(row => <MatchCard key={`${applied}:${row.video_id}:${row.analysis_id}:${row.frame_index}`} row={row} onOpenVideo={onOpenVideo} onOpenCase={onOpenCase} onSimilar={match => void search(sampleReference(match), `Similar to ${match.video_title} at ${footageTimestamp(match.at_s)}`)} />)}</div>
    </section>
  </main>;
}
