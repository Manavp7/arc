import test from 'node:test';
import assert from 'node:assert/strict';
import {SioClient} from '../../sdk/ts/src/client.ts';

function clientWith(payload={}) {const requests=[];const client=new SioClient({url:'https://local.invalid',token:'signed',fetch:async(url,init)=>{requests.push({url:new URL(url),init,body:typeof init.body==='string'?JSON.parse(init.body):init.body});return new Response(JSON.stringify(payload),{headers:{'content-type':'application/json'}});}});return{client,requests};}

test('typed namespace encodes IDs/cursors and preserves query scopes',async()=>{
  const {client,requests}=clientWith({analyses:[],next_cursor:'opaque'});
  assert.equal((await client.investigations.analyses('video/a',{cursor:'next+/='})).next_cursor,'opaque');
  await client.investigations.timeline({camera_id:'camera-a',from:'2026-09-30T00:00:00Z',cursor:'next+/='});
  await client.investigations.deliveries({status:'failed',alert_id:'alert-a',cursor:'next+/='});
  await client.investigations.deliveryHistory('delivery/a',{cursor:'older+/='});
  assert.equal(requests[0].url.pathname,'/api/review/videos/video%2Fa/analyses');assert.equal(requests[0].url.searchParams.get('cursor'),'next+/=');
  assert.equal(requests[1].url.searchParams.get('from'),'2026-09-30T00:00:00Z');assert.equal(requests[2].url.searchParams.get('alert_id'),'alert-a');
  assert.equal(requests[3].url.pathname,'/api/alert-deliveries/delivery%2Fa/history');
  for(const {init}of requests)assert.equal(new Headers(init.headers).get('authorization'),'Bearer signed');
});
test('reviewed activation and calibration apply only server tickets',async()=>{
  const {client,requests}=clientWith();
  await client.investigations.applyActivation({source_id:'source/a',action:'rollback',preview_id:'source-ticket',after:{credential:'not forwarded'}});
  await client.investigations.applyCalibration({setup_id:'setup/a',operation:'rollback',preview_id:'cal-ticket',setup_revision:4,proposed_pose:{lat:99},actor:'not forwarded'});
  assert.equal(requests[0].url.pathname,'/api/sources/source%2Fa/activation/rollback');assert.deepEqual(requests[0].body,{preview_id:'source-ticket',timeout_s:20});
  assert.equal(requests[1].url.pathname,'/api/camera-setups/setup%2Fa/calibration/rollback');assert.deepEqual(requests[1].body,{preview_id:'cal-ticket',expected_revision:4});
});
test('raw upload carries scoped MP4 bytes without JSON conversion',async()=>{
  const {client,requests}=clientWith();const blob=new Blob([new Uint8Array([0,1,2,3])]);
  await client.investigations.upload(blob,'camera one.mp4','yard/a');
  assert.equal(requests[0].body,blob);assert.equal(new Headers(requests[0].init.headers).get('content-type'),'video/mp4');assert.equal(requests[0].url.searchParams.get('access_zone_id'),'yard/a');
});
test('search rejects ambiguous query and private-original indexing without exact consent',async()=>{
  const {client,requests}=clientWith();const sample={video_id:'v',analysis_id:'a',frame_index:4};
  for(const value of [false,1,'yes'])await assert.rejects(client.investigations.indexRecording('v','a',value),/Explicit consent/);
  await assert.rejects(client.investigations.searchRecordings({text:' '}),/exactly one/);await assert.rejects(client.investigations.searchRecordings({text:'person',sample}),/exactly one/);assert.equal(requests.length,0);
  await client.investigations.searchRecordings({sample});assert.deepEqual(requests[0].body.sample,sample);
});
test('case changes preserve explicit null and clock saves omit provenance actors',async()=>{
  const {client,requests}=clientWith();await client.investigations.updateCase('case/a',{expected_revision:2,owner:null});
  await client.investigations.saveClock('video/a',{revision:1,camera_id:'camera',capture_started_at:null,clock_offset_s:0,uncertainty_s:null,note:'Unknown',declared_by:'spoof',updated_at:'old'});
  await client.investigations.retryDelivery('delivery/a','  Receiver restored  ');
  assert.deepEqual(requests[0].body,{expected_revision:2,owner:null});assert.equal(Object.hasOwn(requests[1].body,'declared_by'),false);assert.equal(Object.hasOwn(requests[1].body,'updated_at'),false);assert.deepEqual(requests[2].body,{reason:'Receiver restored'});
});
test('approval derives actor from authentication and explicit provider token does not invoke dev issuer',async()=>{
  for(const token of ['', ' '])assert.throws(()=>new SioClient({token}),/must not be empty/);
  const {client,requests}=clientWith();await client.approve('decision-a','option-a');assert.deepEqual(requests[0].body,{option_id:'option-a'});
  const urls=[];const unauthorized=new SioClient({token:'expired-provider-token',fetch:async url=>{urls.push(String(url));return new Response(JSON.stringify({detail:'expired'}),{status:401});}});
  await assert.rejects(unauthorized.investigations.videos(),error=>error.status===401);assert.equal(urls.length,1);assert.ok(urls.every(url=>url.endsWith('/api/review/videos')));
});


test('job pages retain server-side view and opaque cursor',async()=>{
  const {client,requests}=clientWith({jobs:[],next_cursor:'older+/=',capabilities:{max_attempts:2,max_pending:4,worker_concurrency:1}});
  const first=await client.investigations.jobs({view:'finished',limit:1});
  await client.investigations.jobs({view:'finished',limit:1,cursor:first.next_cursor});
  assert.equal(requests[1].url.pathname,'/api/review/jobs');assert.equal(requests[1].url.searchParams.get('view'),'finished');assert.equal(requests[1].url.searchParams.get('limit'),'1');assert.equal(requests[1].url.searchParams.get('cursor'),'older+/=');
});
