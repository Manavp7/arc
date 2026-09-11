import {useCallback,useEffect,useMemo,useRef,useState,type RefObject} from 'react';
import {explainError} from '../lib/api';
import {useProtectedMedia} from '../lib/review-media';
import type {VideoAnalysis} from '../lib/review-types';
import {boundedFootageTime,footageSeekTarget,footageShortcut,footageThumbnails,footageTimestamp,playbackFrameStep,type FootageCommand,type FootageThumbnailSlot,type NavigationVideo} from '../lib/footage-navigation';
import './footage-navigation.css';

export interface FootageNavigationProps {
  video:NavigationVideo;
  analysis:VideoAnalysis|null;
  playerRef:RefObject<HTMLVideoElement|null>;
  atS:number;
  onSeek:(seconds:number)=>void;
  onBookmark?:(atS:number)=>void;
  disabled?:boolean;
  drawing?:boolean;
  shortcutGuard?:boolean;
  active?:boolean;
  /** The protected blob URL identifies when the parent has mounted/replaced its player. */
  mediaKey?:string|null;
}
interface ThumbnailProps {slot:FootageThumbnailSlot;current:boolean;enabled:boolean;canSeek:boolean;stripRef:RefObject<HTMLOListElement|null>;onSeek:(at:number)=>void}
function FootageThumbnail({slot,current,enabled,canSeek,stripRef,onSeek}:ThumbnailProps){
  const element=useRef<HTMLLIElement|null>(null);
  const [visible,setVisible]=useState(false);
  useEffect(()=>{
    if(!enabled||!element.current)return;
    if(typeof IntersectionObserver==='undefined'){setVisible(true);return;}
    const observer=new IntersectionObserver(entries=>{if(entries.some(entry=>entry.isIntersecting)){setVisible(true);observer.disconnect();}},{root:stripRef.current,rootMargin:'160px'});
    observer.observe(element.current);return()=>observer.disconnect();
  },[enabled,stripRef]);
  const media=useProtectedMedia(enabled&&visible?slot.image_path:null,'image');
  const description=!slot.frame?'No saved sample':!slot.image_path?'No matching saved image':media.error?'Image unavailable':media.loading?'Loading sample…':'Saved sample';
  return <li ref={element} className={`fn-thumbnail${current?' is-current':''}`}>
    <button type="button" disabled={!slot.frame||!canSeek} onClick={()=>{if(slot.frame)onSeek(slot.frame.at_s);}} aria-label={slot.frame?`Seek to saved sample at ${footageTimestamp(slot.frame.at_s)}`:`No saved sample from ${footageTimestamp(slot.start_s)} to ${footageTimestamp(slot.end_s)}`} aria-current={current?'true':undefined} title={media.error??undefined}>
      <span className="fn-thumbnail-image">{media.url?<img src={media.url} alt={`Protected saved sample at ${footageTimestamp(slot.frame!.at_s)}`} loading="lazy"/>:<span>{description}</span>}</span>
      <span className="fn-thumbnail-time">{slot.frame?footageTimestamp(slot.frame.at_s):'—'}</span>
      <span className="fn-thumbnail-range">{footageTimestamp(slot.start_s)}–{footageTimestamp(slot.end_s)}</span>
    </button>
  </li>;
}

export function FootageNavigation({video,analysis,playerRef,atS,onSeek,onBookmark,disabled=false,drawing=false,shortcutGuard=false,active=true,mediaKey}:FootageNavigationProps){
  const [ready,setReady]=useState(false),[playing,setPlaying]=useState(false),[starting,setStarting]=useState(false),[buffering,setBuffering]=useState(false);
  const [rate,setRate]=useState(1),[error,setError]=useState<string|null>(null);
  const stripRef=useRef<HTMLOListElement|null>(null),boundPlayer=useRef<HTMLVideoElement|null>(null);
  const generation=useRef(0),pending=useRef<{id:number;player:HTMLVideoElement}|null>(null),rateRef=useRef(rate);
  rateRef.current=rate;
  const state=useRef({video,analysis,atS,onSeek,onBookmark,disabled,drawing,shortcutGuard,active,ready});
  state.current={video,analysis,atS,onSeek,onBookmark,disabled,drawing,shortcutGuard,active,ready};
  const thumbnails=useMemo(()=>footageThumbnails(video,analysis),[video.video_id,video.duration_s,analysis]);
  const frameStep=playbackFrameStep(video.playback_fps);
  const canOperate=active&&!disabled&&!drawing;
  const canSeek=canOperate&&ready;
  const canBookmark=Boolean(onBookmark&&analysis?.status==='completed'&&analysis.video_id===video.video_id);
  const stop=useCallback(()=>{generation.current++;boundPlayer.current?.pause();setPlaying(false);setStarting(false);setBuffering(false);},[]);

  useEffect(()=>{
    const player=playerRef.current;generation.current++;pending.current=null;boundPlayer.current=player;
    setError(null);setStarting(false);setBuffering(false);setPlaying(false);setReady(false);
    if(!player)return;
    player.pause();player.playbackRate=rateRef.current;
    const syncReady=()=>setReady(player.readyState>=2&&!player.seeking&&!player.error);
    const play=()=>{if(!state.current.active||state.current.disabled||state.current.drawing){stop();return;}setPlaying(!player.paused);};
    const pause=()=>{setPlaying(false);setBuffering(false);};
    const waiting=()=>{setBuffering(!player.paused);syncReady();};
    const resumed=()=>{setBuffering(false);play();syncReady();};
    const failed=()=>{stop();setReady(false);setError('Protected playback could not be decoded. Reload the recording to retry.');};
    const emptied=()=>{stop();setReady(false);};
    const speed=()=>{if(Number.isFinite(player.playbackRate))setRate(player.playbackRate);};
    const listeners:[string,()=>void][]=[['loadedmetadata',syncReady],['loadeddata',syncReady],['canplay',syncReady],['seeked',syncReady],['seeking',syncReady],['play',play],['playing',resumed],['pause',pause],['waiting',waiting],['ended',pause],['error',failed],['emptied',emptied],['ratechange',speed]];
    for(const [event,listener]of listeners)player.addEventListener(event,listener);
    syncReady();
    return()=>{generation.current++;pending.current=null;for(const [event,listener]of listeners)player.removeEventListener(event,listener);player.pause();if(boundPlayer.current===player)boundPlayer.current=null;};
  },[video.video_id,mediaKey,playerRef,stop]);
  useEffect(()=>{if(!active||disabled||drawing)stop();},[active,disabled,drawing,stop]);
  useEffect(()=>{const hidden=()=>{if(document.hidden)stop();};document.addEventListener('visibilitychange',hidden);return()=>document.removeEventListener('visibilitychange',hidden);},[stop]);

  const togglePlay=useCallback(async()=>{
    const current=state.current,player=boundPlayer.current;
    if(!player||!current.active||current.disabled||current.drawing)return;
    if(!player.paused||pending.current){stop();return;}
    if(!current.ready||player.error)return;
    const id=++generation.current;pending.current={id,player};setStarting(true);setError(null);
    try{
      await player.play();
      if(generation.current!==id){if(pending.current?.id===id||boundPlayer.current!==player)player.pause();return;}
      setPlaying(!player.paused);
    }catch(cause){if(generation.current===id){player.pause();setPlaying(false);setError(`Playback stayed paused. ${explainError(cause)} Press Play to retry.`);}}
    finally{if(pending.current?.id===id){pending.current=null;setStarting(false);}}
  },[stop]);
  const seek=useCallback((at:number)=>{
    const current=state.current;
    if(!current.active||current.disabled||current.drawing||!current.ready||!boundPlayer.current)return false;
    stop();current.onSeek(boundedFootageTime(at,current.video.duration_s));return true;
  },[stop]);
  const perform=useCallback((command:FootageCommand)=>{
    const current=state.current,player=boundPlayer.current;
    if(!player||!current.active||current.disabled||current.drawing)return false;
    if(command==='pause'){stop();return true;}
    if(command==='toggle'){if(!current.ready&&player.paused&&!pending.current)return false;void togglePlay();return true;}
    const at=Number.isFinite(player.currentTime)?player.currentTime:current.atS;
    if(command==='bookmark'){
      if(!current.ready||!current.onBookmark||current.analysis?.status!=='completed'||current.analysis.video_id!==current.video.video_id)return false;
      stop();current.onBookmark(boundedFootageTime(at,current.video.duration_s));return true;
    }
    const target=footageSeekTarget(command,at,current.video);return target!==null&&seek(target);
  },[seek,stop,togglePlay]);
  useEffect(()=>{
    if(!active)return;
    const keydown=(event:KeyboardEvent)=>{
      const current=state.current;
      if(document.hidden)return;
      const dialogOpen=Boolean(document.querySelector('dialog[open],[role="dialog"]:not([hidden]),[role="alertdialog"]:not([hidden])'));
      const command=footageShortcut(event,{active:current.active,disabled:current.disabled,drawing:current.drawing,guarded:current.shortcutGuard,dialogOpen,canBookmark:Boolean(current.onBookmark&&current.analysis?.status==='completed'&&current.analysis.video_id===current.video.video_id)});
      if(command&&perform(command))event.preventDefault();
    };
    document.addEventListener('keydown',keydown);return()=>document.removeEventListener('keydown',keydown);
  },[active,perform]);

  const emptyMessage=thumbnails.status==='no_analysis'?'No saved analysis yet. The sample strip appears after analysis completes.':thumbnails.status==='not_completed'?'This analysis is not complete. The sample strip uses only completed saved runs.':thumbnails.status==='wrong_video'?'The loaded analysis belongs to another recording. Its images are not displayed.':'This completed analysis has no saved samples to display.';
  return <section className="fn-navigation" aria-label="Footage review navigation">
    <div className="fn-heading"><div><span className="rv-kicker">REVIEW / TRANSPORT</span><h3>Move through the recording.</h3></div><span className="fn-state" role="status">{error?'Playback error':drawing?'Zone drawing':starting?'Starting…':buffering?'Buffering…':playing?'Playing':ready?'Paused':'Loading playback'}</span></div>
    <div className="fn-transport"><button type="button" className="rv-button rv-primary fn-play" disabled={!canOperate||(!ready&&!playing&&!starting)} onClick={()=>void togglePlay()} aria-keyshortcuts="Space">{playing||starting?'Pause':'Play'} <kbd>Space</kbd></button><div className="fn-button-group" aria-label="Seek by seconds"><button type="button" className="rv-button" disabled={!canSeek} onClick={()=>perform('backward')} aria-keyshortcuts="J">−5s <kbd>J</kbd></button><button type="button" className="rv-button" disabled={!canSeek} onClick={()=>perform('forward')} aria-keyshortcuts="L">+5s <kbd>L</kbd></button></div><div className="fn-button-group" aria-label="Approximate playback frame steps"><button type="button" className="rv-button" disabled={!canSeek||frameStep===null} onClick={()=>perform('previous_frame')} aria-keyshortcuts="ArrowLeft">← 1 playback frame</button><button type="button" className="rv-button" disabled={!canSeek||frameStep===null} onClick={()=>perform('next_frame')} aria-keyshortcuts="ArrowRight">1 playback frame →</button></div><label className="fn-speed">Speed<select value={rate} disabled={!canOperate||!ready} onChange={event=>{const value=Number(event.target.value);const player=boundPlayer.current;if(player){player.playbackRate=value;setRate(value);}}}>{![.25,.5,1,1.5,2].includes(rate)&&<option value={rate}>{rate}×</option>}<option value="0.25">0.25×</option><option value="0.5">0.5×</option><option value="1">1×</option><option value="1.5">1.5×</option><option value="2">2×</option></select></label>{onBookmark&&<button type="button" className="rv-button fn-bookmark" disabled={!canSeek||!canBookmark} onClick={()=>perform('bookmark')} aria-keyshortcuts="B">＋ Bookmark <kbd>B</kbd></button>}</div>
    <p className="fn-help">{frameStep===null?'Playback frame rate is unavailable; frame-step controls are disabled.':`Approximate playback-frame seek: ${Math.round(frameStep*10000)/10000}s at ${video.playback_fps} fps. Browser decoding and variable frame timing can differ from exact source frames.`} <kbd>K</kbd> pauses. Shortcuts are inactive while typing, drawing zones or using a dialog.</p>
    {error&&<p className="rv-error" role="alert">{error}</p>}
    <div className="fn-strip-heading"><span>SAVED ANALYSIS SAMPLES</span><small>{thumbnails.status==='ready'?`${thumbnails.slots.filter(slot=>slot.frame).length} time points · scroll to scan`:'No sample strip'}</small></div>
    {thumbnails.status==='ready'?<><ol ref={stripRef} className="fn-thumbnail-strip" aria-label="Saved analysis thumbnail timeline">{thumbnails.slots.map(slot=><FootageThumbnail key={`${video.video_id}:${thumbnails.analysis_id}:${slot.index}:${slot.image_path??'missing'}`} slot={slot} current={atS>=slot.start_s&&(atS<slot.end_s||slot.index===thumbnails.slots.length-1&&atS===slot.end_s)} enabled={active&&!disabled} canSeek={canSeek} stripRef={stripRef} onSeek={seek}/>)}</ol><p className="fn-provenance">Analysis {thumbnails.analysis_id}. Each image is a retained sample at its labeled timestamp. Empty intervals and missing images do not mean that no incident occurred.</p></>:<p className="fn-empty">{emptyMessage}</p>}
  </section>;
}
