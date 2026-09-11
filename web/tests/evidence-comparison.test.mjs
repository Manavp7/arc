import test from 'node:test';
import assert from 'node:assert/strict';
import {comparisonApi,comparisonDraft,comparisonItems,comparisonProblem,sameComparison,sharedInterval,linkedTimes,swapComparison,observedOffset,loadComparisonEvidence,positionComparedPlayer,prepareComparedPlayback,startComparedPlayback} from '../src/lib/evidence-comparison.ts';
const left={attachment_id:'original',video_id:'vid_left',analysis_id:'ana_old',evidence:{source:'recorded_file'},at_s:3};
const right={attachment_id:'evidence_human',video_id:'vid_right',analysis_id:'ana_human',evidence:{source:'reviewer_annotation'},at_s:8};
const draft={left_attachment_id:'original',right_attachment_id:'evidence_human',offset_s:5,note:'Same light turns on.'};
test('shared interval respects right equals left plus positive or negative offset',()=>{
  assert.deepEqual(sharedInterval(10,12,3),{start:0,end:9,duration:9});assert.deepEqual(sharedInterval(10,12,-3),{start:3,end:10,duration:7});assert.deepEqual(sharedInterval(10,12,0),{start:0,end:10,duration:10});
  for(const values of [[10,12,12],[10,12,-10],[0,12,0],[10,0,0],[10,12,NaN],[Infinity,12,0]])assert.equal(sharedInterval(...values),null);
});
test('linked seeks from either recording stay inside the actual overlap',()=>{
  const interval=sharedInterval(10,12,-3);assert.deepEqual(linkedTimes(0,'left',interval,-3),{left:3,right:0});assert.deepEqual(linkedTimes(5,'right',interval,-3),{left:8,right:5});assert.deepEqual(linkedTimes(100,'right',interval,-3),{left:10,right:7});
});
test('swapping changes source sides and offset sign without losing reviewer notes',()=>{
  const swapped=swapComparison(draft);assert.equal(swapped.left_attachment_id,'evidence_human');assert.equal(swapped.offset_s,-5);assert.equal(swapped.note,draft.note);assert.deepEqual(swapComparison(swapped),draft);assert.equal(Object.is(swapComparison({...draft,offset_s:0}).offset_s,-0),false);assert.equal(observedOffset(12.123,17.123),5);
});
test('human annotations can be compared while unrelated evidence cannot',()=>{
  assert.deepEqual(comparisonItems([left,right,{attachment_id:'alert',evidence:{source:'platform_alert'}}]),[left,right]);assert.equal(comparisonProblem(draft,[left,right]),null);assert.match(comparisonProblem({...draft,right_attachment_id:'original'},[left,right]),/different/);assert.match(comparisonProblem({...draft,right_attachment_id:'unavailable'},[left,right]),/available/);assert.match(comparisonProblem({...draft,offset_s:Infinity},[left,right]),/finite/);assert.match(comparisonProblem({...draft,offset_s:181},[left,right]),/180/);
});
test('unsaved server state remains an explicit empty selection and dirty comparison ignores metadata',()=>{
  const record={left_attachment_id:null,right_attachment_id:null,offset_s:0,note:'',revision:0,case_revision:7};assert.deepEqual(comparisonDraft(record),{left_attachment_id:null,right_attachment_id:null,offset_s:0,note:''});assert.equal(sameComparison(draft,{...draft,revision:8}),true);assert.equal(sameComparison(draft,{...draft,note:'Other cue'}),false);assert.equal(sameComparison(draft,swapComparison(draft)),false);
});
test('saving sends exact comparison and case revisions with selected attachment identities',async()=>{
  const {api}=await import('../src/lib/api.ts');const original=api.request;let captured;
  try{api.request=async(path,options)=>{captured={path,body:JSON.parse(options.body),method:options.method};return {...draft,revision:4};};await comparisonApi.save('case a',draft,3,9);assert.deepEqual(captured,{path:'/cases/case%20a/comparison',method:'PUT',body:{...draft,revision:3,case_revision:9}});}finally{api.request=original;}
});
test('loading uses each exact retained analysis without falling back to the latest run',async()=>{
  const {reviewApi}=await import('../src/lib/review-api.ts');const originalVideo=reviewApi.video,originalAnalysis=reviewApi.analysis;const calls=[];
  try{reviewApi.video=async(id)=>{calls.push(['video',id]);return {video_id:id,analysis_id:'newer-run',media_url:'/api/review/videos/vid_left/media'};};reviewApi.analysis=async(id,signal,version)=>{calls.push(['analysis',id,version]);return {video_id:id,analysis_id:version,status:'completed',zones:[],rules:[]};};const result=await loadComparisonEvidence(left,new AbortController().signal);assert.equal(result.analysis.analysis_id,'ana_old');assert.deepEqual(calls,[['video','vid_left'],['analysis','vid_left','ana_old']]);reviewApi.analysis=async()=>{throw new Error('Pinned version missing');};await assert.rejects(loadComparisonEvidence(left,new AbortController().signal),/Pinned version missing/);reviewApi.analysis=async()=>({video_id:'another-video',analysis_id:'ana_old',status:'completed'});await assert.rejects(loadComparisonEvidence(left,new AbortController().signal),/exact completed/);}finally{reviewApi.video=originalVideo;reviewApi.analysis=originalAnalysis;}
});
test('one browser play rejection pauses both attempted players',async()=>{
  const stopped=[];const players=[{play:async()=>{},pause:()=>stopped.push('left')},{play:async()=>{throw new Error('NotAllowedError');},pause:()=>stopped.push('right')}];assert.equal(await startComparedPlayback(players,()=>true),false);assert.ok(stopped.includes('left'));assert.ok(stopped.includes('right'));
});
test('late play resolution cannot restart footage after a source change or pause',async()=>{
  let resolve;let current=true;let stopped=0;const pending=new Promise(done=>{resolve=done;});const result=startComparedPlayback([{play:()=>pending,pause:()=>{stopped++;}},{play:async()=>{},pause:()=>{stopped++;}}],()=>current);current=false;resolve();assert.equal(await result,false);assert.equal(stopped,2);
});
test('successful independent playback never pauses an unrelated player',async()=>{
  let stopped=0,played=0;const active={play:async()=>{played++;},pause:()=>{stopped++;}};assert.equal(await startComparedPlayback([active],()=>true),true);assert.equal(played,1);assert.equal(stopped,0);
});

test('a play rejection pauses the whole group before another pending play settles',async()=>{
  let release;const pending=new Promise(resolve=>{release=resolve;});const stopped=[];const result=startComparedPlayback([{play:async()=>{throw new Error('decode failed');},pause:()=>stopped.push('left')},{play:()=>pending,pause:()=>stopped.push('right')}],()=>true);await new Promise(resolve=>setImmediate(resolve));assert.deepEqual(stopped,['left','right']);release();assert.equal(await result,false);
});
test('comparison GET preserves cancellation and note and offset limits match the saved contract',async()=>{
  const {api}=await import('../src/lib/api.ts');const original=api.request;const signal=new AbortController().signal;let captured;try{api.request=async(path,options)=>{captured={path,signal:options.signal};return {};};await comparisonApi.get('case / one',signal);assert.deepEqual(captured,{path:'/cases/case%20%2F%20one/comparison',signal});}finally{api.request=original;}assert.equal(comparisonProblem({...draft,note:'x'.repeat(4000),offset_s:-180},[left,right]),null);assert.match(comparisonProblem({...draft,note:'x'.repeat(4001)},[left,right]),/4000/);
});

class PreparingVideo extends EventTarget {
  constructor(at=0){super();this.at=at;this.duration=10;this.readyState=4;this.seeking=false;this.error=null;this.seekWrites=0;}
  get currentTime(){return this.at;}
  set currentTime(value){this.seekWrites++;this.at=value;this.seeking=true;this.readyState=2;}
  finishSeek(){this.seeking=false;this.readyState=4;this.dispatchEvent(new Event('seeked'));this.dispatchEvent(new Event('canplay'));}
}
test('starting already aligned paused players does not trigger a redundant media seek',async()=>{
  const left=new PreparingVideo(2.014),right=new PreparingVideo(3.013);
  assert.equal(await prepareComparedPlayback([{player:left,at:2.014},{player:right,at:3.014}],new AbortController().signal),true);
  assert.equal(left.seekWrites,0);assert.equal(right.seekWrites,0);
  assert.equal(positionComparedPlayer(left,2.014),false);
});
test('group playback preparation waits for both target positions to finish seeking and decode',async()=>{
  const left=new PreparingVideo(2),right=new PreparingVideo(2);let done=false;
  const result=prepareComparedPlayback([{player:left,at:2},{player:right,at:3}],new AbortController().signal).then(value=>{done=true;return value;});
  await new Promise(resolve=>setImmediate(resolve));assert.equal(done,false);assert.equal(right.seekWrites,1);
  right.readyState=4;right.dispatchEvent(new Event('canplay'));await new Promise(resolve=>setImmediate(resolve));assert.equal(done,false,'canplay while seeking must not start the group');
  right.finishSeek();assert.equal(await result,true);assert.equal(left.seekWrites,0);
});
test('pause or source replacement cancels pending seek preparation without starting playback',async()=>{
  const player=new PreparingVideo(0),controller=new AbortController();
  const result=prepareComparedPlayback([{player,at:3}],controller.signal);controller.abort();assert.equal(await result,false);
  player.finishSeek();const writes=player.seekWrites;assert.equal(await prepareComparedPlayback([{player,at:5}],controller.signal),false);assert.equal(player.seekWrites,writes);
});
test('unready or undecodable media cannot pass playback preparation',async()=>{
  const waiting=new PreparingVideo(0);waiting.readyState=2;
  assert.equal(await prepareComparedPlayback([{player:waiting,at:0}],new AbortController().signal,5),false);
  const broken=new PreparingVideo(0);broken.error={code:3};assert.equal(await prepareComparedPlayback([{player:broken,at:0}],new AbortController().signal),false);
});
