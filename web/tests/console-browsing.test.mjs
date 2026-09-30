import test from 'node:test';
import assert from 'node:assert/strict';
import {timelineQuery,timelineFilterValues} from '../src/lib/recording-timeline.ts';
import {decodeNavigation,encodeNavigation} from '../src/lib/investigation-navigation.ts';
import {alertDeliveryApi,mergeDeliveryHistory} from '../src/lib/alert-delivery.ts';
import {calibrationApi,calibrationPreviewUsable} from '../src/lib/calibration-publication.ts';
import {uploadAccessZones} from '../src/lib/review-access.ts';
import {reviewApi} from '../src/lib/review-api.ts';
import {api} from '../src/lib/api.ts';

test('timeline date filters are explicitly UTC and reject reversed or invalid ranges',()=>{
  assert.deepEqual(timelineFilterValues(' cam-a ','2026-09-30T08:00','2026-09-30T09:00'),{camera_id:'cam-a',from:'2026-09-30T08:00:00.000Z',to:'2026-09-30T09:00:00.000Z'});
  assert.throws(()=>timelineFilterValues('','bad',''));assert.throws(()=>timelineFilterValues('','2026-09-30T09:00','2026-09-30T08:00'));
  const q=new URLSearchParams(timelineQuery({camera_id:'camera/a',cursor:'opaque+/=',from:'2026-09-30T08:00:00Z'}));assert.equal(q.get('camera_id'),'camera/a');assert.equal(q.get('cursor'),'opaque+/=');assert.equal(q.get('limit'),'20');
});
test('camera setup links retain exact setup and site but strip identifiers from unrelated views',()=>{
  const selected={tab:'camera',siteId:'site-a',setupId:'setup-b'};assert.deepEqual(decodeNavigation(encodeNavigation(selected)),selected);assert.equal(encodeNavigation({...selected,tab:'site'}),'?view=site&site=site-a');
});
test('history pages retain distinct attempts and deduplicate overlap',()=>{
  const item=(id,kind='attempt')=>({history_id:id,at:'2026-09-30T08:00:00Z',kind,attempt:1});
  assert.deepEqual(mergeDeliveryHistory([item(4),item(3)],[item(3),item(2),item(1)]).map(row=>row.history_id),[4,3,2,1]);
});
test('pagination and calibration clients send encoded scope-bound references, never pose or actor replacements',async()=>{
  const original=api.request,calls=[];api.request=async(...args)=>{calls.push(args);return {};};
  try{
    await alertDeliveryApi.list('failed',undefined,{cursor:'next+/=',alert_id:'alert/a'});await alertDeliveryApi.history('delivery/a','history+/=');
    await calibrationApi.preview('setup/a',7,true);await calibrationApi.apply({setup_id:'setup/a',preview_id:'ticket',setup_revision:7,operation:'rollback',proposed_pose:{lat:99},actor:'spoof'});
    await reviewApi.upload(new File(['x'],'recording.mp4'),undefined,'permitted/zone');
    assert.equal(new URL(calls[0][0],'http://localhost').searchParams.get('cursor'),'next+/=');assert.match(calls[1][0],/delivery%2Fa\/history/);
    assert.equal(calls[2][0],'/camera-setups/setup%2Fa/calibration/rollback-preview');assert.deepEqual(JSON.parse(calls[3][1].body),{preview_id:'ticket',expected_revision:7});
    assert.equal(calls[4][0],'/review/videos?access_zone_id=permitted%2Fzone');assert.equal(calls[4][1].body.name,'recording.mp4');
  }finally{api.request=original;}
});
test('calibration tickets expire and bind exact saved setup revision',()=>{
  const p={setup_id:'a',setup_revision:2,expires_at:'2026-09-30T09:00:00Z'};const now=Date.parse('2026-09-30T08:00:00Z');assert.equal(calibrationPreviewUsable(p,'a',2,now),true);assert.equal(calibrationPreviewUsable(p,'b',2,now),false);assert.equal(calibrationPreviewUsable(p,'a',3,now),false);assert.equal(calibrationPreviewUsable(p,'a',2,now+3600000),false);
});
test('upload permissions distinguish scoped access labels from unrestricted and administrator accounts',()=>{
  const identity=(zones,roles=['operator'])=>({roles,token:`x.${Buffer.from(JSON.stringify({zones})).toString('base64url')}.x`});
  assert.deepEqual(uploadAccessZones(identity(['yard','gate','yard'])),['yard','gate']);assert.deepEqual(uploadAccessZones(identity(' yard,gate ')),['yard','gate']);assert.deepEqual(uploadAccessZones(identity(['*'])),[]);assert.deepEqual(uploadAccessZones(identity(['yard'],['admin'])),[]);assert.deepEqual(uploadAccessZones(identity(undefined)),[]);
});
