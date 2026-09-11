import {api} from './api';
import {claimsOf,type Session} from './session';
import type {VideoAnalysis} from './review-types';
export interface ReviewBookmark {bookmark_id:string;video_id:string;analysis_id:string;at_s:number;title:string;note:string;author:string;revision:number;created_at:string;updated_at:string}
export interface BookmarkDraft {at_s:number;title:string;note:string}
export interface BookmarkPage {bookmarks:ReviewBookmark[];limit:number;possibly_truncated:boolean}
const collection=(videoId:string)=>`/review/videos/${encodeURIComponent(videoId)}/bookmarks`;
const item=(id:string)=>`/review/bookmarks/${encodeURIComponent(id)}`;
export const bookmarksApi={
  list:(videoId:string,signal?:AbortSignal)=>api.request<BookmarkPage>(collection(videoId),{signal}),
  create:(videoId:string,analysisId:string,draft:BookmarkDraft)=>api.request<ReviewBookmark>(collection(videoId),{method:'POST',body:JSON.stringify({analysis_id:analysisId,...draft,title:draft.title.trim(),note:draft.note.trim()})}),
  update:(bookmark:ReviewBookmark,draft:BookmarkDraft)=>api.request<ReviewBookmark>(item(bookmark.bookmark_id),{method:'PATCH',body:JSON.stringify({expected_revision:bookmark.revision,...draft,title:draft.title.trim(),note:draft.note.trim()})}),
  remove:(bookmark:ReviewBookmark)=>api.request<{deleted:boolean;bookmark_id:string}>(item(bookmark.bookmark_id),{method:'DELETE',body:JSON.stringify({expected_revision:bookmark.revision})}),
};
export function bookmarkProblem(draft:BookmarkDraft,duration:number):string|null {
  if(!draft.title.trim()||draft.title.length>120)return 'Give the bookmark a title of up to 120 characters.';
  if(draft.note.length>2000)return 'Keep the bookmark note within 2000 characters.';
  if(!Number.isFinite(draft.at_s)||draft.at_s<0||draft.at_s>duration)return 'Choose a finite position inside this recording.';
  return null;
}
export function bookmarkAnalysis(videoId:string,analysis:VideoAnalysis|null):string|null {return analysis?.video_id===videoId&&analysis.status==='completed'?analysis.analysis_id:null;}

/** Private-note state follows the server subject and zone scope, not token expiry or display names. */
export function bookmarkScope(identity:Session|null):string {
  if(!identity)return 'signed-out';
  const claims=claimsOf(identity.token);
  const zones=Array.isArray(claims.zones)?claims.zones.map(String):typeof claims.zones==='string'?claims.zones.split(',').map(zone=>zone.trim()).filter(Boolean):[];
  return JSON.stringify([identity.tenant,claims.sub||claims.preferred_username||identity.subject,[...new Set(identity.roles)].sort(),identity.clearance,[...new Set(zones)].sort()]);
}
