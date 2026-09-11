import test from 'node:test';
import assert from 'node:assert/strict';
import {boundedFootageTime,footageSeekTarget,footageShortcut,footageThumbnails,footageTimestamp,playbackFrameStep} from '../src/lib/footage-navigation.ts';

const video={video_id:'video_a',duration_s:120,playback_fps:15};
const frame=(at,index=0,extra={})=>({at_s:at,frame_index:index,frame_url:`/api/review/videos/video_a/frames/analysis_old/${index}`,objects:[],...extra});
const analysis=frames=>({video_id:'video_a',analysis_id:'analysis_old',status:'completed',detections:frames,events:[],zones:[],rules:[],model:{mode:'motion',name:'Motion regions'}});

test('review keyboard protocol maps transport, frame and bookmark commands',()=>{
  for(const [key,command]of [[' ','toggle'],['j','backward'],['L','forward'],['k','pause'],['ArrowLeft','previous_frame'],['ArrowRight','next_frame'],['b','bookmark']])assert.equal(footageShortcut({key},{canBookmark:true}),command);
  assert.equal(footageShortcut({key:'b'}),null);assert.equal(footageShortcut({key:'x'}),null);
  assert.equal(footageShortcut({key:' ',repeat:true}),null);assert.equal(footageShortcut({key:'b',repeat:true},{canBookmark:true}),null);assert.equal(footageShortcut({key:'j',repeat:true}),'backward');
});
test('typing, native control focus and dialogs never trigger footage transport',()=>{
  for(const tagName of ['INPUT','TEXTAREA','SELECT','OPTION','DIALOG'])for(const key of [' ','j','ArrowLeft','b'])assert.equal(footageShortcut({key,target:{tagName}},{canBookmark:true}),null);
  for(const tagName of ['VIDEO','AUDIO','BUTTON','A','SUMMARY'])for(const key of [' ','ArrowLeft','ArrowRight'])assert.equal(footageShortcut({key,target:{tagName}}),null);
  assert.equal(footageShortcut({key:'j',target:{tagName:'SPAN',isContentEditable:true}}),null);
  assert.equal(footageShortcut({key:'j',target:{tagName:'SPAN',closest:()=>({tagName:'DIV'})}}),null);
  assert.equal(footageShortcut({key:'j'},{dialogOpen:true}),null);
});
test('shortcuts honor inactive views, drawing, explicit guards, modifiers and composition',()=>{
  for(const context of [{active:false},{disabled:true},{drawing:true},{guarded:true}])assert.equal(footageShortcut({key:'j'},context),null);
  for(const property of ['ctrlKey','metaKey','altKey','shiftKey','defaultPrevented','isComposing'])assert.equal(footageShortcut({key:'j',[property]:true}),null);
});
test('frame stepping uses the protected playback rate and stays within footage bounds',()=>{
  assert.equal(playbackFrameStep(15),1/15);assert.equal(footageSeekTarget('next_frame',2,video),2+1/15);assert.equal(footageSeekTarget('previous_frame',0,video),0);assert.equal(footageSeekTarget('next_frame',120,video),120);
  assert.equal(footageSeekTarget('forward',118,video),120);assert.equal(footageSeekTarget('backward',3,video),0);assert.equal(footageSeekTarget('forward',15,video),20);
  assert.equal(footageSeekTarget('next_frame',2,{video_id:'legacy',duration_s:20,fps:60}),null,'original fps must not substitute for playback fps');
  for(const value of [null,undefined,0,-1,NaN,Infinity])assert.equal(playbackFrameStep(value),null);
  assert.equal(footageSeekTarget('bookmark',2,video),null);assert.equal(boundedFootageTime(NaN,120),0);
});
test('navigation timestamps round across minute boundaries without displaying 60 seconds',()=>{
  assert.equal(footageTimestamp(59.999),'1:00.00');assert.equal(footageTimestamp(61.234),'1:01.23');assert.equal(footageTimestamp(-2),'0:00.00');assert.equal(footageTimestamp(NaN),'0:00.00');
});
test('thumbnail selection is bounded, ordered and spread across time without fabricated samples',()=>{
  const saved=[0,20,40,60,80,100,120].map((at,index)=>frame(at,index));
  const result=footageThumbnails(video,analysis([...saved].reverse()));assert.equal(result.status,'ready');assert.equal(result.analysis_id,'analysis_old');assert.equal(result.slots.length,12);
  assert.deepEqual(result.slots.filter(slot=>slot.frame).map(slot=>slot.frame.at_s),saved.map(item=>item.at_s));
  assert.equal(result.slots[1].frame,null);assert.equal(result.slots[1].image_path,null);assert.equal(result.slots[11].frame.at_s,120);
  assert.equal(footageThumbnails(video,analysis(saved),500).slots.length,12);
});
test('sparse runs leave gaps instead of repeating the same frame across the recording',()=>{
  const result=footageThumbnails(video,analysis([frame(1,0),frame(2,1),frame(3,2)]));
  assert.equal(result.slots.filter(slot=>slot.frame).length,1);assert.equal(result.slots[0].frame.at_s,3);assert.equal(result.slots.slice(1).every(slot=>slot.frame===null),true);
});
test('sample strip uses exact retained analysis frame references and preserves missing-image slots',()=>{
  const result=footageThumbnails(video,analysis([frame(1,0),frame(21,1,{frame_url:''}),frame(41,2,{frame_url:'/api/review/videos/video_a/frames/newer_analysis/2'}),frame(61,3,{frame_url:'/api/review/videos/another_video/frames/analysis_old/3'})]));
  assert.equal(result.slots[0].image_path,'/api/review/videos/video_a/frames/analysis_old/0');
  for(const index of [2,4,6]){assert.ok(result.slots[index].frame);assert.equal(result.slots[index].image_path,null);}
});
test('another clip or unfinished analysis cannot supply images; no-analysis and no-frame states are explicit',()=>{
  assert.equal(footageThumbnails(video,null).status,'no_analysis');
  assert.equal(footageThumbnails(video,{...analysis([frame(1)]),video_id:'video_b'}).status,'wrong_video');
  assert.equal(footageThumbnails(video,{...analysis([frame(1)]),status:'running'}).status,'not_completed');
  assert.equal(footageThumbnails(video,analysis([])).status,'no_frames');
  assert.equal(footageThumbnails(video,analysis([frame(-1),frame(NaN),frame(121)])).status,'no_frames');
  assert.equal(footageThumbnails({...video,duration_s:0},analysis([frame(0)])).slots.length,0);
});
