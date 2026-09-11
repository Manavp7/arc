import { api } from './api';
import type { ReviewVideo, ReviewZone, VideoAnalysis, VideoEvent } from './review-types';
export interface EvaluationInterval { start_s:number; end_s:number }
export interface EvaluationScope { zone_id:string; event_type:'entry'|'dwell' }
export interface EvaluationAnnotation extends EvaluationInterval, EvaluationScope { annotation_id:string; note:string }
export interface EvaluationDraft { revision:number; name:string; partition:'development'|'validation'|'held_out'; footage_origin:'real'|'authored'|'unknown'; scopes:EvaluationScope[]; coverage:EvaluationInterval[]; annotations:EvaluationAnnotation[]; note:string; video_id?:string; video_title?:string; duration_s?:number }
export interface AnnotationSet extends EvaluationDraft { annotation_set_id:string; annotation_hash:string; draft_revision:number; frozen_at:string }
export interface EvaluationMetrics { true_positive:number; false_positive:number; false_negative:number; precision:number|null; recall:number|null; false_alarms_per_reviewed_hour:number|null; reviewed_seconds:number; median_signed_delay_s:number|null; matched_delay_samples:number }
export interface ClipEvaluation { video_id:string; analysis_id:string; annotation_set_id:string; metrics:EvaluationMetrics; matches:{annotation:EvaluationAnnotation;event:VideoEvent;signed_delay_s:number}[]; false_alarms:VideoEvent[]; misses:EvaluationAnnotation[]; excluded_events:VideoEvent[]; unreviewed_seconds:number; provenance:{model:{name:string;mode:string};configuration_hash:string;annotation_hash:string} }
export interface EvaluationReport { report_id:string; title:string; created_at:string; partition:string; footage_origins:string[]; candidates:{candidate:number;clips:ClipEvaluation[];metrics:EvaluationMetrics}[]; limitations:string[]; tolerance_s:number }
export interface EvaluationSelection { annotation_set_id:string; analysis_ids:string[]; video_id:string; title:string; partition:string }
export interface EvaluationVersions { video:ReviewVideo; draft:EvaluationDraft|null; annotation_sets:AnnotationSet[]; analyses:VideoAnalysis[]; note:string }
const base='/review/evaluations';
export const evaluationApi={
  overview:(signal?:AbortSignal)=>api.request<{reports:EvaluationReport[];limitations:string[]}>(base,{signal}),
  versions:(id:string,signal?:AbortSignal)=>api.request<EvaluationVersions>(`${base}/videos/${encodeURIComponent(id)}`,{signal}),
  save:(id:string,draft:EvaluationDraft)=>api.request<EvaluationDraft>(`${base}/videos/${encodeURIComponent(id)}/annotations`,{method:'PUT',body:JSON.stringify(draft)}),
  freeze:(id:string,revision:number)=>api.request<AnnotationSet>(`${base}/videos/${encodeURIComponent(id)}/freeze`,{method:'POST',body:JSON.stringify({revision})}),
  report:(inputs:EvaluationSelection[],tolerance_s:number)=>api.request<EvaluationReport>(`${base}/reports`,{method:'POST',body:JSON.stringify({title:inputs.length===1 ? `${inputs[0]?.title??"Clip"} evaluation` : `${inputs.length} clip comparison`,inputs:inputs.map(({annotation_set_id,analysis_ids})=>({annotation_set_id,analysis_ids})),tolerance_s})}),
  getReport:(id:string)=>api.request<EvaluationReport>(`${base}/reports/${encodeURIComponent(id)}`),
};
export function initialEvaluation(video:ReviewVideo):EvaluationDraft { return {revision:0,name:`${video.title} labels`,partition:'development',footage_origin:'unknown',scopes:[],coverage:[],annotations:[],note:''}; }
export function reviewedSeconds(intervals:EvaluationInterval[]):number { let end=0,total=0;for(const item of [...intervals].sort((a,b)=>a.start_s-b.start_s)){if(Number.isFinite(item.start_s)&&Number.isFinite(item.end_s)&&item.start_s>=0&&item.end_s>item.start_s){total+=Math.max(0,item.end_s-Math.max(end,item.start_s));end=Math.max(end,item.end_s);}}return total; }
export function evaluationProblem(draft:EvaluationDraft,duration:number):string|null {
  if(!draft.name.trim())return 'Give this annotation set a name.';
  if(!draft.scopes.length)return 'Select the zone and event types you reviewed.';
  if(!draft.coverage.length)return 'Mark at least one completely reviewed time interval.';
  for(const item of [...draft.coverage,...draft.annotations])if(!Number.isFinite(item.start_s)||!Number.isFinite(item.end_s)||item.start_s<0||item.end_s<=item.start_s||item.end_s>duration)return 'Each interval must end after its start and stay inside the clip.';
  const scopes=new Set(draft.scopes.map(s=>`${s.zone_id}:${s.event_type}`));
  for(const item of draft.annotations){if(!scopes.has(`${item.zone_id}:${item.event_type}`))return 'Every incident must belong to a reviewed scope.';
    const clipped=draft.coverage.map(range=>({start_s:Math.max(range.start_s,item.start_s),end_s:Math.min(range.end_s,item.end_s)}));
    if(reviewedSeconds(clipped)+1e-8<item.end_s-item.start_s)return 'Every incident must be fully inside reviewed coverage.';}
  return null;
}
export function addEvaluationSelection(existing:EvaluationSelection[],item:EvaluationSelection):EvaluationSelection[]{
  const others=existing.filter(row=>row.video_id!==item.video_id);
  if(others.some(row=>row.partition!==item.partition))throw new Error('Compare development, validation and held-out clips in separate reports.');
  if(others.some(row=>row.analysis_ids.length!==item.analysis_ids.length))throw new Error('Choose the same number of candidate versions for each clip.');
  if(others.length>=20)throw new Error('A comparison can include up to 20 clips.');
  return [...others,item];
}
export function allEvaluationZones(data:EvaluationVersions):ReviewZone[]{return [...new Map([...data.video.zones,...data.analyses.flatMap(a=>a.zones)].map(zone=>[zone.zone_id,zone])).values()];}
export function percentage(value:number|null){return value==null?'Unmeasured':`${(value*100).toFixed(1)}%`;}
