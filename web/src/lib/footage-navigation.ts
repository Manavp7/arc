import type {DetectionFrame,VideoAnalysis} from './review-types';

export interface NavigationVideo {video_id:string;duration_s:number;playback_fps?:number|null}
export type FootageCommand='toggle'|'pause'|'backward'|'forward'|'previous_frame'|'next_frame'|'bookmark';
interface ShortcutEvent {key:string;defaultPrevented?:boolean;ctrlKey?:boolean;metaKey?:boolean;altKey?:boolean;shiftKey?:boolean;isComposing?:boolean;repeat?:boolean;target?:unknown}
interface ShortcutTarget {tagName?:string;isContentEditable?:boolean;closest?:(selector:string)=>unknown}
interface ShortcutContext {active?:boolean;disabled?:boolean;drawing?:boolean;guarded?:boolean;dialogOpen?:boolean;canBookmark?:boolean}
/** Keyboard commands never replace editing, modal, or focused native-control behavior. */
export function footageShortcut(event:ShortcutEvent,context:ShortcutContext={}):FootageCommand|null{
  if(context.active===false||context.disabled||context.drawing||context.guarded||context.dialogOpen||event.defaultPrevented||event.ctrlKey||event.metaKey||event.altKey||event.shiftKey||event.isComposing)return null;
  const target=event.target as ShortcutTarget|null|undefined;
  const tag=target?.tagName?.toLowerCase();
  if(target?.isContentEditable||['input','textarea','select','option','dialog'].includes(tag??'')||target?.closest?.('input,textarea,select,[contenteditable]:not([contenteditable="false"]),[role="textbox"],[role="combobox"],[role="listbox"],[role="slider"],dialog,[role="dialog"],[role="alertdialog"]'))return null;
  const key=event.key.toLowerCase();
  if((key===' '||key==='spacebar'||key==='arrowleft'||key==='arrowright')&&(['video','audio','button','a','summary'].includes(tag??'')||target?.closest?.('video,audio,button,a[href],summary,[role="button"],[role="link"]')))return null;
  const command:FootageCommand|null=key===' '||key==='spacebar'?'toggle':key==='k'?'pause':key==='j'?'backward':key==='l'?'forward':key==='arrowleft'?'previous_frame':key==='arrowright'?'next_frame':key==='b'&&context.canBookmark?'bookmark':null;
  return event.repeat&&(command==='toggle'||command==='bookmark')?null:command;
}
export function playbackFrameStep(fps:number|null|undefined):number|null{
  if(typeof fps!=='number'||!Number.isFinite(fps)||fps<=0)return null;
  const step=1/fps;return Number.isFinite(step)&&step>0?step:null;
}
export function boundedFootageTime(at:number,duration:number):number{
  return Math.max(0,Math.min(Number.isFinite(duration)?Math.max(0,duration):0,Number.isFinite(at)?at:0));
}
export function footageSeekTarget(command:FootageCommand,at:number,video:NavigationVideo):number|null{
  const frame=playbackFrameStep(video.playback_fps);
  const delta=command==='backward'?-5:command==='forward'?5:command==='previous_frame'&&frame!==null?-frame:command==='next_frame'&&frame!==null?frame:null;
  return delta===null?null:boundedFootageTime(at+delta,video.duration_s);
}
export function footageTimestamp(at:number):string{
  const hundredths=Math.round(Math.max(0,Number.isFinite(at)?at:0)*100);
  return `${Math.floor(hundredths/6000)}:${((hundredths%6000)/100).toFixed(2).padStart(5,'0')}`;
}
export interface FootageThumbnailSlot {index:number;start_s:number;end_s:number;frame:DetectionFrame|null;image_path:string|null}
export interface FootageThumbnails {status:'ready'|'no_analysis'|'not_completed'|'wrong_video'|'no_frames';analysis_id:string|null;slots:FootageThumbnailSlot[]}
function retainedFramePath(videoId:string,analysisId:string,frame:DetectionFrame):string|null{
  if(!Number.isInteger(frame.frame_index)||frame.frame_index<0)return null;
  const expected=`/api/review/videos/${encodeURIComponent(videoId)}/frames/${encodeURIComponent(analysisId)}/${frame.frame_index}`;
  return frame.frame_url===expected?frame.frame_url:null;
}
/** Use one actual saved sample per time bucket; never repeat images to fill gaps. */
export function footageThumbnails(video:NavigationVideo,analysis:VideoAnalysis|null,limit=12):FootageThumbnails{
  if(!analysis)return {status:'no_analysis',analysis_id:null,slots:[]};
  if(analysis.video_id!==video.video_id)return {status:'wrong_video',analysis_id:null,slots:[]};
  if(analysis.status!=='completed')return {status:'not_completed',analysis_id:analysis.analysis_id,slots:[]};
  const duration=video.duration_s;
  const frames=analysis.detections.filter(frame=>Number.isFinite(frame.at_s)&&frame.at_s>=0&&frame.at_s<=duration).slice().sort((a,b)=>a.at_s-b.at_s||a.frame_index-b.frame_index);
  if(!Number.isFinite(duration)||duration<=0||!frames.length)return {status:'no_frames',analysis_id:analysis.analysis_id,slots:[]};
  const count=Math.min(12,Math.max(1,Math.floor(Number.isFinite(limit)?limit:12)),Math.max(1,Math.ceil(duration)));
  const slots:FootageThumbnailSlot[]=Array.from({length:count},(_,index)=>({index,start_s:duration*index/count,end_s:duration*(index+1)/count,frame:null,image_path:null}));
  for(const frame of frames){
    const slot=slots[Math.min(count-1,Math.floor(frame.at_s/duration*count))]!;
    const midpoint=(slot.start_s+slot.end_s)/2;
    if(!slot.frame||Math.abs(frame.at_s-midpoint)<Math.abs(slot.frame.at_s-midpoint)){
      slot.frame=frame;slot.image_path=retainedFramePath(video.video_id,analysis.analysis_id,frame);
    }
  }
  return {status:'ready',analysis_id:analysis.analysis_id,slots};
}
