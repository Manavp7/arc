import test from 'node:test';
import assert from 'node:assert/strict';
import {api} from '../src/lib/api.ts';
import {recordedFramePath,recordedSearchApi,sampleReference,similarityLabel} from '../src/lib/recorded-search.ts';
const sample={video_id:`vid_${'a'.repeat(32)}`,analysis_id:`ana_${'b'.repeat(32)}`,frame_index:4};
test('thumbnail paths use only validated retained-source IDs and never a result URL',()=>{
  assert.equal(recordedFramePath({...sample,frame_url:'https://untrusted.invalid/original'}),`/api/review/videos/${sample.video_id}/frames/${sample.analysis_id}/4`);
  for(const bad of [{video_id:'../secret'},{analysis_id:'original.mp4'},{frame_index:360},{frame_index:-1},{frame_index:NaN}])assert.equal(recordedFramePath({...sample,...bad}),null);
});
test('image searches strip server fields and preserve exact selected sample',()=>{
  assert.deepEqual(sampleReference({...sample,similarity:.9,frame_url:'bad'}),sample);
  assert.equal(similarityLabel(.71342),'0.713');assert.equal(similarityLabel(NaN),'Unavailable');
});
test('indexing requires consent and image query suppresses a stale text query',async()=>{
  const calls=[];const original=api.request;api.request=async(...args)=>{calls.push(args);return{};};
  try{
    await assert.rejects(()=>recordedSearchApi.index(sample.video_id,sample.analysis_id,false),/Confirm/);
    assert.equal(calls.length,0);
    await recordedSearchApi.index(sample.video_id,sample.analysis_id,true);
    assert.deepEqual(JSON.parse(calls[0][1].body),{video_id:sample.video_id,analysis_id:sample.analysis_id,consent_private_original:true});
    await recordedSearchApi.query('ignored stale query',sample,'');
    assert.deepEqual(JSON.parse(calls[1][1].body),{text:'',sample,video_id:'',limit:24});
    await assert.rejects(()=>recordedSearchApi.query(' ',null,''),/Describe/);
    await recordedSearchApi.query(' red truck ',null,sample.video_id);
    assert.equal(JSON.parse(calls[2][1].body).text,'red truck');
  }finally{api.request=original;}
});
