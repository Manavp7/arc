import {api} from './api';
import {reviewApi} from './review-api';
import type {CaseEvidenceItem, ReviewVideo, VideoAnalysis} from './review-types';

export interface ComparisonDraft {left_attachment_id:string|null;right_attachment_id:string|null;offset_s:number;note:string}
export interface CaseComparison extends ComparisonDraft {case_id:string;case_title:string;case_revision:number;saved_case_revision?:number|null;revision:number;updated_by?:string;updated_at?:string;evidence_items:CaseEvidenceItem[]}
export interface ComparisonEvidence {item:CaseEvidenceItem;video:ReviewVideo;analysis:VideoAnalysis}
export interface SharedInterval {start:number;end:number;duration:number}
export type ComparisonSide='left'|'right';
const path=(id:string)=>`/cases/${encodeURIComponent(id)}/comparison`;
export const comparisonApi={
  get:(id:string,signal?:AbortSignal)=>api.request<CaseComparison>(path(id),{signal}),
  save:(id:string,draft:ComparisonDraft,revision:number,caseRevision:number)=>api.request<CaseComparison>(path(id),{method:'PUT',body:JSON.stringify({...draft,revision,case_revision:caseRevision})}),
};
/** Source provenance can be machine events or human annotations; both pin real media/runs. */
export function comparisonItems(items:CaseEvidenceItem[]):CaseEvidenceItem[]{return items.filter(item=>Boolean(item.video_id&&item.analysis_id));}
export function comparisonDraft(record:CaseComparison):ComparisonDraft{return {left_attachment_id:record.left_attachment_id,right_attachment_id:record.right_attachment_id,offset_s:record.offset_s,note:record.note};}
export function sameComparison(a:ComparisonDraft,b:ComparisonDraft):boolean{return a.left_attachment_id===b.left_attachment_id&&a.right_attachment_id===b.right_attachment_id&&a.offset_s===b.offset_s&&a.note===b.note;}
export function comparisonProblem(draft:ComparisonDraft,items:CaseEvidenceItem[]):string|null{
  if(!draft.left_attachment_id||!draft.right_attachment_id)return 'Choose a left and a right evidence source.';
  if(draft.left_attachment_id===draft.right_attachment_id)return 'Choose two different evidence attachments.';
  const valid=new Set(comparisonItems(items).map(item=>item.attachment_id));
  if(!valid.has(draft.left_attachment_id)||!valid.has(draft.right_attachment_id))return 'Both sources must be available recording evidence attached to this case.';
  if(!Number.isFinite(draft.offset_s)||Math.abs(draft.offset_s)>180)return 'Set a finite offset between -180 and 180 seconds.';
  if(draft.note.length>4000)return 'Keep the alignment note within 4000 characters.';
  return null;
}
/** right_time = left_time + offset; intervals are expressed on the left clock. */
export function sharedInterval(leftDuration:number,rightDuration:number,offset:number):SharedInterval|null{
  if(![leftDuration,rightDuration,offset].every(Number.isFinite)||leftDuration<=0||rightDuration<=0)return null;
  const start=Math.max(0,-offset),end=Math.min(leftDuration,rightDuration-offset);
  return end>start?{start,end,duration:end-start}:null;
}
export function clampTime(value:number,duration:number):number{return Math.max(0,Math.min(Number.isFinite(duration)?Math.max(0,duration):0,Number.isFinite(value)?value:0));}
export function linkedTimes(value:number,side:ComparisonSide,interval:SharedInterval,offset:number):{left:number;right:number}{const left=Math.max(interval.start,Math.min(interval.end,side==='right'?value-offset:value));return {left,right:left+offset};}
export function swapComparison(draft:ComparisonDraft):ComparisonDraft{return {...draft,left_attachment_id:draft.right_attachment_id,right_attachment_id:draft.left_attachment_id,offset_s:draft.offset_s===0?0:-draft.offset_s};}
export function observedOffset(leftTime:number,rightTime:number):number{return Math.round((rightTime-leftTime)*1000)/1000;}
interface Seekable {currentTime:number;duration:number}
/** Reassigning currentTime even to the same value can initiate a browser seek. */
export function positionComparedPlayer(player:Seekable,at:number):boolean{
  const target=clampTime(at,player.duration);
  if(Math.abs(player.currentTime-target)<=.015)return false;
  player.currentTime=target;return true;
}
interface PreparingPlayer extends Seekable {seeking:boolean;readyState:number;error:unknown;addEventListener:(type:string,listener:EventListener)=>void;removeEventListener:(type:string,listener:EventListener)=>void}
/** Wait for both requested positions to be decoded before attempting grouped play. */
export function prepareComparedPlayback(targets:{player:PreparingPlayer;at:number}[],signal:AbortSignal,timeoutMs=4000):Promise<boolean>{
  if(signal.aborted||!targets.length)return Promise.resolve(false);
  try{for(const target of targets)positionComparedPlayer(target.player,target.at);}catch{return Promise.resolve(false);}
  return new Promise(resolve=>{
    const events=['seeked','canplay','loadeddata','error'];let settled=false;
    const finish=(ready:boolean)=>{if(settled)return;settled=true;clearTimeout(timer);signal.removeEventListener('abort',cancel);for(const {player} of targets)for(const event of events)player.removeEventListener(event,check);resolve(ready);};
    const cancel=()=>finish(false);
    const check=()=>{if(signal.aborted||targets.some(({player})=>Boolean(player.error)))finish(false);else if(targets.every(({player})=>!player.seeking&&player.readyState>=3))finish(true);};
    const timer=setTimeout(cancel,timeoutMs);
    signal.addEventListener('abort',cancel,{once:true});
    for(const {player} of targets)for(const event of events)player.addEventListener(event,check);
    check();
  });
}
export async function loadComparisonEvidence(item:CaseEvidenceItem,signal:AbortSignal):Promise<ComparisonEvidence>{
  if(!item.video_id||!item.analysis_id)throw new Error('This attachment has no pinned recording analysis.');
  const [video,analysis]=await Promise.all([reviewApi.video(item.video_id,signal),reviewApi.analysis(item.video_id,signal,item.analysis_id)]);
  if(video.video_id!==item.video_id||analysis.video_id!==item.video_id||analysis.analysis_id!==item.analysis_id||analysis.status!=='completed')throw new Error('The exact completed analysis for this attachment is unavailable.');
  return {item,video,analysis};
}
interface Playable {play:()=>Promise<void>;pause:()=>void}
/** Any rejection or a changed source/stop generation stops every attempted player. */
export async function startComparedPlayback(players:Playable[],isCurrent:()=>boolean):Promise<boolean>{
  const outcomes=await Promise.allSettled(players.map(player=>Promise.resolve().then(()=>player.play()).catch(cause=>{for(const attempted of players)attempted.pause();throw cause;})));
  if(!isCurrent()||outcomes.some(result=>result.status==='rejected')){for(const player of players)player.pause();return false;}
  return true;
}
