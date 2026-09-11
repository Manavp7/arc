import test from 'node:test';
import assert from 'node:assert/strict';
import { clipProblem, recordedCaseSummaries, setupBody, setupDraft } from '../src/lib/commissioning.ts';

const saved = {setup_id:'setup-a',title:'Surveyed gate',source_id:'rtsp-a',site_id:'site-a',camera_id:'camera-a',pose:{lat:0,lon:0,bearing_deg:0,height_m:6,tilt_deg:35,fov_deg:70,vfov_deg:45,frame_width:1920,frame_height:1080,range_m:60},checkpoints:[{checkpoint_id:'p1',label:'Survey marker',image_x:.2,image_y:.4,measured_east_m:0,measured_north_m:8}],tolerance_m:.5,measurement_note:'Surveyed with tape against site datum',revision:8,status:'validated',validation:{passed:true},updated_by:'surveyor',activation:'not_applied'};

test('missing camera measurements remain blank and cannot become false zero coordinates', () => {
  const empty = setupDraft();
  assert.deepEqual(empty.pose, {});
  assert.deepEqual(empty.checkpoints, []);
  assert.throws(() => setupBody(empty,0), /existing camera/);
  const draft = setupDraft(saved);
  draft.pose.lat='';
  assert.throws(() => setupBody(draft,8), /Latitude/);
  draft.pose.lat='0';draft.checkpoints[0].measured_east_m=' ';
  assert.throws(() => setupBody(draft,8), /surveyed east distance/);
});
test('saved readiness drafts retain measured zeroes, image dimensions and current revision without trusting validation metadata', () => {
  const draft = setupDraft(saved);
  const body = setupBody(draft,8);
  assert.equal(body.expected_revision,8);
  assert.equal(body.pose.lat,0);
  assert.equal(body.pose.frame_width,1920);
  assert.equal(body.checkpoints[0].measured_east_m,0);
  assert.equal(body.measurement_note,saved.measurement_note);
  for (const field of ['status','validation','updated_by','activation']) assert.equal(field in body,false);
  draft.pose.lat='NaN';assert.throws(()=>setupBody(draft,8), /Latitude/);
});
test('evidence interval validation handles endpoints and caps output independently of recording length', () => {
  assert.equal(clipProblem(0,60,180,[]),null);
  assert.equal(clipProblem(179,180,180,[]),null);
  for (const [start,end,duration] of [[0,61,180],[-1,2,180],[4,4,180],[0,4,3],[NaN,2,3],[0,2,Infinity]]) assert.ok(clipProblem(start,end,duration,[]));
});
test('evidence annotations cannot silently leave their selected clip when the interval changes', () => {
  const notes=[{at_s:4,text:'Vehicle crossed the painted line'}];
  assert.equal(clipProblem(3,5,12,notes),null);
  assert.match(clipProblem(5,8,12,notes),/inside/);
  assert.match(clipProblem(3,5,12,[{at_s:NaN,text:'Missing timestamp'}]),/inside/);
  assert.match(clipProblem(3,5,12,[{at_s:4,text:' '}]),/needs text/);
});

test('evidence package selector uses the case-list summary contract without requiring omitted evidence payloads', () => {
  // GET /api/cases excludes evidence, timeline and notes.
  const summaries = [{case_id:'recorded',title:'Authored recording event',video_id:'vid_a',analysis_id:'ana_original',event_id:'event_a',status:'open',revision:1}, {case_id:'live',title:'Platform alert',video_id:null,analysis_id:null,alert_id:'alert_a',status:'open',revision:2}, {case_id:'incomplete',title:'Unavailable analysis',video_id:'vid_b',analysis_id:null,status:'open',revision:1}];
  assert.deepEqual(recordedCaseSummaries(summaries), [summaries[0]]);
  assert.equal('evidence' in summaries[0], false);
});
