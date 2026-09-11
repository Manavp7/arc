import {api} from './api';
import type {ReviewRule,ReviewVideo} from './review-types';
export interface PresetSlot {slot_id:string;name:string}
export interface PresetSourceSlot extends PresetSlot {source_zone_id:string}
export interface PresetRule extends Omit<ReviewRule,'zone_id'> {slot_id:string}
export interface PresetVersion {version_id:string;number:number;slots:PresetSlot[];rules:PresetRule[];created_at:string;created_by:string}
export interface RulePreset {preset_id:string;name:string;description:string;revision:number;current_version_id:string;versions:PresetVersion[]}
export interface PresetTarget {video_id:string;revision:number;zone_map:Record<string,string>}
export interface PresetResult {video_id:string;title?:string;status:'ready'|'rejected'|'applied'|'already_applied';message?:string;revision?:number;before_rules?:ReviewRule[];added_rules?:ReviewRule[];final_rules?:ReviewRule[];removed_count?:number}
export interface PresetPreview {preview_id:string;preset_name:string;version_id:string;version_number:number;mode:'append'|'replace';expires_at:string;results:PresetResult[];note:string}
export interface PresetSave {name:string;description:string;source_video_id:string;source_revision:number;expected_revision:number;slots:PresetSourceSlot[]}
const base='/review/rule-presets';
export const rulePresetsApi={
  list:(signal?:AbortSignal)=>api.request<{presets:RulePreset[]}>(base,{signal}),
  save:(body:PresetSave,id?:string)=>api.request<RulePreset>(`${base}${id?`/${encodeURIComponent(id)}`:''}`,{method:id?'PUT':'POST',body:JSON.stringify(body)}),
  preview:(body:{preset_id:string;version_id:string;mode:'append'|'replace';targets:PresetTarget[]})=>api.request<PresetPreview>(`${base}/preview`,{method:'POST',body:JSON.stringify(body)}),
  apply:(preview_id:string)=>api.request<{results:PresetResult[];analysis_started:false}>(`${base}/apply`,{method:'POST',body:JSON.stringify({preview_id})}),
};
export function sourceSlots(video:ReviewVideo):PresetSourceSlot[]{const used=new Set(video.rules.map(rule=>rule.zone_id));return video.zones.filter(zone=>used.has(zone.zone_id)).map((zone,index)=>({source_zone_id:zone.zone_id,slot_id:`scope_${index+1}`,name:zone.name}));}
export function mappedTargets(videos:ReviewVideo[],selected:string[],mappings:Record<string,Record<string,string>>,version:PresetVersion):PresetTarget[]{
  if(!selected.length||selected.length>20)throw new Error('Choose between one and 20 target recordings.');
  return selected.map(id=>{const video=videos.find(item=>item.video_id===id);if(!video)throw new Error('A target recording is no longer available. Refresh targets.');const zone_map=mappings[id]??{};const required=version.slots.map(slot=>slot.slot_id);if(required.some(slot=>!zone_map[slot])||Object.keys(zone_map).some(slot=>!required.includes(slot)))throw new Error(`Map every preset scope for ${video.title}.`);if(new Set(Object.values(zone_map)).size!==required.length)throw new Error('Map each logical scope to a different target zone.');if(Object.values(zone_map).some(zone=>!video.zones.some(item=>item.zone_id===zone)))throw new Error('Choose zones that already belong to each target recording.');return {video_id:id,revision:video.revision,zone_map:{...zone_map}};});
}
export function presetPreviewSummary(result:PresetResult,mode:'append'|'replace'):string {return result.status==='ready'?`${result.before_rules?.length??0} current → ${result.final_rules?.length??0} rules · ${result.added_rules?.length??0} added${mode==='replace'?` · ${result.removed_count??0} removed`:''}`:result.message??result.status.replaceAll('_',' ');}
