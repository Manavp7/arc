import {useEffect,useRef,useState} from 'react';
import {ApiError,explainError} from '../lib/api';
import * as session from '../lib/session';
import type {ReviewVideo,VideoAnalysis} from '../lib/review-types';
import {bookmarkAnalysis,bookmarkProblem,bookmarksApi,bookmarkScope,type BookmarkDraft,type ReviewBookmark} from '../lib/review-bookmarks';
import './footage-workbench.css';

interface Props {video:ReviewVideo;analysis:VideoAnalysis|null;atS:number;disabled?:boolean;request?:{atS:number;nonce:number};onOpen:(bookmark:ReviewBookmark)=>void;onDirtyChange:(dirty:boolean)=>void}
export function FootageBookmarks(props:Props) {
  const identity=session.useSession();
  // Remount on scope changes so a previous account's private notes never render.
  const scope=bookmarkScope(identity);
  return identity?<ScopedBookmarks key={scope} {...props} identity={identity}/>:null;
}
function ScopedBookmarks({video,analysis,atS,disabled=false,request,onOpen,onDirtyChange,identity}:Props&{identity:session.Session}) {
  const [items,setItems]=useState<ReviewBookmark[]>([]),[draft,setDraft]=useState<BookmarkDraft|null>(null),[editing,setEditing]=useState<ReviewBookmark|null>(null);
  const [pinned,setPinned]=useState<string|null>(null),[busy,setBusy]=useState(false),[loading,setLoading]=useState(true),[reload,setReload]=useState(0);
  const [error,setError]=useState<string|null>(null),[notice,setNotice]=useState(''),[conflict,setConflict]=useState(false),[truncated,setTruncated]=useState(false),[removeId,setRemoveId]=useState<string|null>(null);
  const mounted=useRef(true),currentDraft=useRef(draft),listVersion=useRef(0),listRequest=useRef<AbortController|null>(null);currentDraft.current=draft;
  const runId=bookmarkAnalysis(video.video_id,analysis),canWrite=session.can('review.write');
  useEffect(()=>{mounted.current=true;return()=>{mounted.current=false;};},[]);
  useEffect(()=>{onDirtyChange(Boolean(draft)||busy);return()=>onDirtyChange(false);},[draft,busy,onDirtyChange]);
  useEffect(()=>{
    if(busy)return;
    const controller=new AbortController(),version=++listVersion.current;
    listRequest.current=controller;setLoading(true);
    const current=()=>mounted.current&&!controller.signal.aborted&&version===listVersion.current;
    void bookmarksApi.list(video.video_id,controller.signal).then(result=>{if(!current())return;setItems(result.bookmarks);setTruncated(result.possibly_truncated);}).catch(cause=>{
      if(!current())return;
      if(cause instanceof ApiError&&[401,403,404].includes(cause.status)){setItems([]);setDraft(null);setEditing(null);setPinned(null);setRemoveId(null);setNotice('');}
      setError(explainError(cause));
    }).finally(()=>{if(current())setLoading(false);});
    return()=>controller.abort();
  },[video.video_id,reload,identity]);
  function begin(position:number) {
    if(currentDraft.current){setError('Save or discard the bookmark you are editing first.');return;}
    if(!runId||disabled||!canWrite||loading||busy)return;
    setEditing(null);setPinned(runId);setDraft({at_s:Math.max(0,Math.min(video.duration_s,position)),title:'',note:''});setError(null);setNotice('');setConflict(false);setRemoveId(null);
  }
  useEffect(()=>{if(request)begin(request.atS);},[request?.nonce]);
  function discard(){setDraft(null);setEditing(null);setPinned(null);setConflict(false);setError(null);setRemoveId(null);}
  function clearDenied(cause:unknown){
    if(cause instanceof ApiError&&[401,403,404].includes(cause.status)){setItems([]);setDraft(null);setEditing(null);setPinned(null);setRemoveId(null);setNotice('');}
  }
  async function save(){
    if(!draft||!pinned||busy||loading||disabled)return;
    const issue=bookmarkProblem(draft,video.duration_s);if(issue){setError(issue);return;}
    ++listVersion.current;listRequest.current?.abort();setLoading(false);setBusy(true);setError(null);
    try {
      const result=editing?await bookmarksApi.update(editing,draft):await bookmarksApi.create(video.video_id,pinned,draft);
      if(!mounted.current)return;
      setItems(value=>[result,...value.filter(item=>item.bookmark_id!==result.bookmark_id)].sort((a,b)=>a.at_s-b.at_s));
      const reused=!editing&&(result.title!==draft.title.trim()||result.note!==draft.note.trim());
      setNotice(reused?'A bookmark already exists at this exact position and analysis. Its saved title and note were preserved.':'Bookmark saved to your personal recording notes.');discard();
    } catch(cause){if(mounted.current){clearDenied(cause);setConflict(cause instanceof ApiError&&cause.status===409);setError(cause instanceof ApiError&&cause.status===409?'This bookmark changed elsewhere. Your draft is kept; reload the saved list before editing again.':explainError(cause));}}
    finally{if(mounted.current)setBusy(false);}
  }
  async function remove(item:ReviewBookmark){
    if(busy||loading||draft||disabled)return;++listVersion.current;listRequest.current?.abort();setLoading(false);setBusy(true);setError(null);
    try{await bookmarksApi.remove(item);if(!mounted.current)return;setItems(value=>value.filter(row=>row.bookmark_id!==item.bookmark_id));setRemoveId(null);setNotice('Bookmark removed. Recording and analysis files are unchanged.');}
    catch(cause){if(mounted.current){clearDenied(cause);setError(explainError(cause));}}
    finally{if(mounted.current)setBusy(false);}
  }
  return <section className="rv-editor-card fw-bookmarks" aria-label="Personal recording bookmarks">
    <div className="rv-section-title"><div><span className="rv-kicker">YOUR REVIEW / SAVED MOMENTS</span><h2>Keep a place in the footage.</h2></div><button className="rv-button" disabled={!canWrite||!runId||disabled||busy||loading||Boolean(draft)} onClick={()=>begin(atS)}>Bookmark current position</button></div>
    <p className="rv-small">Personal notes visible to your account. Each bookmark opens its exact retained analysis. Bookmarks are working notes and do not create a case or an evaluation label.</p>
    {error&&<p className="rv-error" role="alert">{error}</p>}{notice&&<p className="rv-notice" role="status">{notice}</p>}
    {!runId&&<p className="rv-small">Open a completed analysis to save a bookmark with stable evidence context.</p>}
    {draft&&<div className="fw-bookmark-editor"><strong>{editing?'Edit personal bookmark':'New personal bookmark'}</strong><p className="fw-reference">Pinned analysis {pinned}</p><div className="fw-controls"><label>Bookmark title<input autoFocus maxLength={120} value={draft.title} disabled={busy||loading||disabled} onChange={event=>setDraft({...draft,title:event.target.value})}/></label><label>Bookmark position (seconds)<input type="number" min="0" max={video.duration_s} step="0.001" value={Number.isFinite(draft.at_s)?draft.at_s:''} disabled={busy||loading||disabled} onChange={event=>setDraft({...draft,at_s:event.target.valueAsNumber})}/></label></div><label>Bookmark note<textarea rows={3} maxLength={2000} value={draft.note} disabled={busy||loading||disabled} onChange={event=>setDraft({...draft,note:event.target.value})}/></label><div className="rv-row"><button className="rv-button rv-primary" disabled={busy||loading||disabled||conflict||Boolean(bookmarkProblem(draft,video.duration_s))} onClick={()=>void save()}>{busy?'Saving bookmark…':'Save bookmark'}</button><button className="rv-button" disabled={busy} onClick={()=>{discard();if(conflict)setReload(value=>value+1);}}>{conflict?'Reload saved bookmarks (discard draft)':'Discard bookmark draft'}</button></div></div>}
    <div className="fw-bookmark-list">{items.map(item=><article key={item.bookmark_id} className="fw-bookmark"><div><button className="fw-bookmark-open" disabled={busy||loading||disabled||Boolean(draft)} onClick={()=>onOpen(item)}><span>{item.at_s.toFixed(3)}s</span><strong>{item.title}</strong><span aria-hidden="true">↗</span></button>{item.note&&<p>{item.note}</p>}<p className="fw-reference">Analysis {item.analysis_id}{item.analysis_id===analysis?.analysis_id?' · current view':''}<br/>Saved by {item.author} · {new Date(item.updated_at).toLocaleString()} · revision {item.revision}</p></div>{canWrite&&<div className="rv-row"><button className="rv-button" disabled={busy||loading||disabled||Boolean(draft)} onClick={()=>{setEditing(item);setPinned(item.analysis_id);setDraft({at_s:item.at_s,title:item.title,note:item.note});setConflict(false);setError(null);setRemoveId(null);}}>Edit bookmark</button>{removeId===item.bookmark_id?<><button className="rv-button" disabled={busy||loading||disabled||Boolean(draft)} onClick={()=>void remove(item)}>Confirm remove bookmark</button><button className="rv-button" disabled={busy} onClick={()=>setRemoveId(null)}>Keep bookmark</button></>:<button className="rv-button" disabled={busy||loading||disabled||Boolean(draft)} onClick={()=>setRemoveId(item.bookmark_id)}>Remove bookmark</button>}</div>}</article>)}</div>
    {loading&&<p className="rv-small">Loading your saved moments…</p>}{!loading&&!items.length&&<p className="rv-empty">No bookmarks for this recording yet. Pause on a useful moment and save it with a note.</p>}{truncated&&<p className="rv-notice">This list reached its scan limit; older bookmarks may not be shown.</p>}
    <button className="rv-button" disabled={busy||loading||Boolean(draft)} onClick={()=>setReload(value=>value+1)}>Refresh bookmarks</button>
  </section>;
}
