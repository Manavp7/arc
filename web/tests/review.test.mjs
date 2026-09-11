import test from 'node:test';
import assert from 'node:assert/strict';
import { normalizedPoint, polygonProblem, detectionAt, trackTrails, formatVideoTime } from '../src/lib/review-geometry.ts';
import { caseDraft, casePatch, localDateInput, metricDuration } from '../src/lib/review-state.ts';
import { protectedUrl, readBoundedBlob } from '../src/lib/review-api.ts';

test('normalized image coordinates account for element position, aspect and boundaries', () => {
  const bounds = { left: 140, top: 50, width: 800, height: 450 };
  assert.deepEqual(normalizedPoint(540, 275, bounds), [.5, .5]);
  assert.deepEqual(normalizedPoint(-1, 900, bounds), [0, 1]);
});
test('polygon editor accepts concave shapes with separated collinear edges', () => {
  assert.equal(polygonProblem([[0,0],[1,0],[1,1],[.7,1],[.7,.7],[.3,.7],[.3,1],[0,1]]), null);
  assert.equal(polygonProblem([[.1,.1],[.9,.1],[.9,.9],[.1,.9]]), null);
});
test('polygon editor rejects crossings, repeated vertices and invalid image coordinates', () => {
  assert.match(polygonProblem([[0,0],[1,1],[1,0],[0,1]]), /cross/);
  assert.match(polygonProblem([[0,0],[1,0],[1,1],[0,0]]), /repeated/);
  assert.match(polygonProblem([[0,0],[1.1,0],[1,1]]), /inside/);
  assert.match(polygonProblem([[0,0],[NaN,0],[1,1]]), /inside/);
  assert.match(polygonProblem([[0,0],[.1,.1],[.2,.2]]), /line/);
});
const object = (x, id = 'track-1') => ({ track_id: id, class_name: 'motion', confidence: null, bbox: [x,0,x+.2,.2] });
const frame = (at_s, x=0) => ({ at_s, frame_index: at_s*2, frame_url: '/api/review/example.jpg', objects: [object(x)] });
test('scrubbing uses the latest nearby detection and clears it across gaps', () => {
  const frames = [frame(4),frame(0),frame(2)];
  assert.equal(detectionAt(frames, 2.3)?.at_s, 2);
  assert.equal(detectionAt(frames, 3.9), null);
  assert.equal(detectionAt(frames, -1), null);
  assert.equal(detectionAt(frames, 30), null);
});
test('trails exclude future and aged samples and never join different tracks', () => {
  const frames = [frame(0,0),frame(4,.2),frame(5,.4),frame(6,.6)];
  assert.deepEqual(trackTrails(frames,5,2), [{ id:'track-1', points:'300.00000000000006,100 500,100' }]);
  assert.deepEqual(trackTrails([frame(4), {...frame(5),objects:[object(.2,'other')]}],5), []);
});
test('case resolution requires an authored reason and retains optimistic revision', () => {
  const draft = { title:'  Boundary event  ',owner:' Ops ',due:'',status:'resolved',summary:'Reviewed clip',verdict:'false_positive',resolution:' ' };
  assert.throws(() => casePatch(draft,7), /resolution reason/);
  const patch = casePatch({...draft,resolution:'Reflection outside the doorway'},7);
  assert.equal(patch.expected_revision,7);
  assert.equal(patch.title,'Boundary event');
  assert.equal(patch.owner,'Ops');
  assert.equal(patch.verdict,'false_positive');
  assert.equal(patch.resolution_note,'Reflection outside the doorway');
  assert.equal(patch.due_at,null);
  assert.throws(() => casePatch({...draft,resolution:'Reviewed',due:'invalid'},7), /valid due date/);
});
test('due dates round trip local input without silently changing the saved instant', () => {
  const record = { title:'A',owner:null,due_at:'2026-09-11T12:30:00.000Z',status:'open',summary:'',verdict:'unreviewed' };
  const draft = caseDraft(record);
  assert.equal(casePatch(draft,1).due_at, record.due_at);
  assert.equal(localDateInput('not a date'),'');
});
test('unavailable timing metrics stay unavailable rather than becoming zero', () => {
  assert.equal(metricDuration(null),'Not available');
  assert.equal(metricDuration(0),'0s');
  assert.equal(formatVideoTime(125.8),'2:05');
});
test('protected media never attaches credentials to other origins or unrelated routes', () => {
  const origin='https://console.example';
  assert.equal(protectedUrl('/api/sites/site/floorplan',origin),origin+'/api/sites/site/floorplan');
  assert.equal(protectedUrl('/media/frame.jpg',origin),origin+'/media/frame.jpg');
  for (const path of ['https://other.example/api/review/file','//other.example/media/file','/auth/login','data:text/plain,secret','/api/review/../../auth/login']) assert.throws(() => protectedUrl(path,origin),/protected/);
});
test('protected download bounds unannounced streamed bytes and cancels excess data', async () => {
  let cancelled=false;
  const response = new Response(new ReadableStream({start(controller){controller.enqueue(new Uint8Array([1,2,3]));controller.enqueue(new Uint8Array([4,5,6]));},cancel(){cancelled=true;}}),{headers:{'Content-Type':'image/png'}});
  await assert.rejects(readBoundedBlob(response,5),/size limit/);
  assert.equal(cancelled,true);
});
test('protected downloads reject oversized declarations and preserve valid media MIME', async () => {
  await assert.rejects(readBoundedBlob(new Response(new Uint8Array([1]),{headers:{'Content-Length':'100'}}),5),/size limit/);
  const blob=await readBoundedBlob(new Response(new Uint8Array([1,2]),{headers:{'Content-Type':'image/png; charset=binary'}}),5);
  assert.equal(blob.type,'image/png');assert.equal(blob.size,2);
});

test('opening old case evidence fetches its exact run and keeps its original configuration', async () => {
  const { api, ApiError } = await import('../src/lib/api.ts');
  const { loadVideoReview } = await import('../src/lib/review-api.ts');
  const { reviewConfiguration } = await import('../src/lib/review-state.ts');
  const original = api.request;
  const requests=[];
  const oldZone={zone_id:'door',name:'Original boundary',points:[[0,0],[1,0],[1,1]]};
  const video={video_id:'clip',analysis_id:'new-run',zones:[{...oldZone,name:'Moved boundary'}],rules:[]};
  const saved={analysis_id:'old-run',zones:[oldZone],rules:[{rule_id:'old-rule'}]};
  try {
    api.request=async (path) => { requests.push(path); return path.includes('/analysis?analysis_id=old-run') ? saved : video; };
    const [record,analysis]=await loadVideoReview('clip',new AbortController().signal,'old-run');
    assert.equal(record.analysis_id,'new-run');
    assert.equal(analysis.analysis_id,'old-run');
    assert.deepEqual(reviewConfiguration(record,analysis,true),{zones:[oldZone],rules:saved.rules});
    assert.equal(reviewConfiguration(record,analysis,false).zones[0].name,'Moved boundary');
    assert.ok(requests.includes('/review/videos/clip/analysis?analysis_id=old-run'));
    api.request=async path => { if(path.includes('/analysis')) throw new ApiError('Missing',404,path);return video; };
    await assert.rejects(loadVideoReview('clip',new AbortController().signal,'missing-run'),/Missing/);
    assert.equal((await loadVideoReview('clip',new AbortController().signal))[1],null);
  } finally { api.request=original; }
});

test('local upload probe defers unsupported codecs and timeout to the server but rejects readable overlong footage', async () => {
  const { validateUpload } = await import('../src/lib/review-api.ts');
  const original={document:globalThis.document,window:globalThis.window,create:URL.createObjectURL,revoke:URL.revokeObjectURL};
  let outcome='error',created=0,revoked=0,cleared=0,timer;
  const video={duration:12,onloadedmetadata:null,onerror:null,removeAttribute(){cleared++;},load(){},set src(_value){queueMicrotask(()=>{if(outcome==='timeout')timer();else if(outcome==='error')this.onerror?.();else this.onloadedmetadata?.();});}};
  globalThis.document={createElement(){created++;return video;}};
  globalThis.window={setTimeout(fn){timer=fn;return 1;},clearTimeout(){}};
  URL.createObjectURL=()=> 'blob:local-metadata';URL.revokeObjectURL=()=>{revoked++;};
  const file={name:'supported-by-server.mp4',type:'video/mp4',size:32};
  try {
    await validateUpload(file,100,180); // Browser cannot decode mp4v; server can transcode.
    outcome='timeout';await validateUpload(file,100,180);
    outcome='metadata';video.duration=12;await validateUpload(file,100,180);
    video.duration=181;await assert.rejects(validateUpload(file,100,180),/no longer than 180/);
    assert.equal(created,4);assert.equal(revoked,4);assert.equal(cleared,4);
    assert.equal(video.onerror,null);assert.equal(video.onloadedmetadata,null);
    await assert.rejects(validateUpload({...file,size:101},100,180),/smaller/);
    await assert.rejects(validateUpload({...file,name:'clip.mov'},100,180),/MP4/);
    assert.equal(created,4,'file type and byte limits run before creating a local player');
  } finally {globalThis.document=original.document;globalThis.window=original.window;URL.createObjectURL=original.create;URL.revokeObjectURL=original.revoke;}
});
