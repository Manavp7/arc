import test from 'node:test';
import assert from 'node:assert/strict';
import {api} from '../src/lib/api.ts';
import {reviewApi} from '../src/lib/review-api.ts';
import {analysisOptionsProblem,defaultAnalysisOptions,normalizedAnalysisOptions,sameAnalysisOptions} from '../src/lib/analysis-profiles.ts';
import {bookmarkAnalysis,bookmarkProblem,bookmarksApi,bookmarkScope} from '../src/lib/review-bookmarks.ts';

const capabilities={models:[{mode:'motion',available:true},{mode:'onnx',available:false}],sample_fps:[1,2],confidence_threshold:{min:.05,max:.95,default:.35},default_mode:'auto'};
test('explicit profile choices reject unavailable models and invalid sampling/confidence',()=>{
  assert.equal(analysisOptionsProblem({mode:'motion',sample_fps:1},capabilities),null);
  assert.match(analysisOptionsProblem({mode:'onnx',sample_fps:2,confidence_threshold:.4},capabilities),/unavailable/);
  assert.match(analysisOptionsProblem({mode:'auto',sample_fps:2},capabilities),/explicitly/);
  assert.match(analysisOptionsProblem({mode:'motion',sample_fps:3},capabilities),/sampling/);
  assert.match(analysisOptionsProblem({mode:'motion',sample_fps:2}),/Refresh/);
  const ready={...capabilities,models:[{mode:'onnx',available:true}]};
  for(const confidence_threshold of [undefined,NaN,Infinity,-.1,1])assert.match(analysisOptionsProblem({mode:'onnx',sample_fps:2,confidence_threshold},ready),/confidence/);
  assert.equal(analysisOptionsProblem({mode:'onnx',sample_fps:2,confidence_threshold:.05},ready),null);
});
test('next-run settings preserve a retained profile and motion never fabricates confidence',()=>{
  const analysis={profile:{resolved_mode:'onnx',sample_fps:1,confidence_threshold:.7}};
  assert.deepEqual(defaultAnalysisOptions(capabilities,analysis),{mode:'onnx',sample_fps:1,confidence_threshold:.7});
  assert.deepEqual(defaultAnalysisOptions(capabilities),{mode:'motion',sample_fps:2});
  assert.deepEqual(normalizedAnalysisOptions({mode:'motion',sample_fps:1,confidence_threshold:.8}),{mode:'motion',sample_fps:1});
  assert.equal(sameAnalysisOptions({mode:'motion',sample_fps:1},{mode:'motion',sample_fps:1,confidence_threshold:.8}),true);
  assert.equal(sameAnalysisOptions({mode:'motion',sample_fps:1},{mode:'motion',sample_fps:2}),false);
});
test('analysis requests include selected settings while retaining the legacy empty-body call',async()=>{
  const original=api.request,calls=[];api.request=async(path,options)=>{calls.push([path,options]);return {};};
  try{await reviewApi.analyze('clip/one',{mode:'motion',sample_fps:1});await reviewApi.analyze('clip/two');
    assert.equal(calls[0][0],'/review/videos/clip%2Fone/analyze');assert.deepEqual(JSON.parse(calls[0][1].body),{mode:'motion',sample_fps:1});assert.deepEqual(JSON.parse(calls[1][1].body),{});
  }finally{api.request=original;}
});
test('bookmarks require a valid position and exact completed recording context',()=>{
  for(const at_s of [-1,NaN,Infinity,6.001])assert.match(bookmarkProblem({at_s,title:'Moment',note:''},6),/position/);
  for(const at_s of [0,2.5,6])assert.equal(bookmarkProblem({at_s,title:'Moment',note:''},6),null);
  assert.match(bookmarkProblem({at_s:2,title:'   ',note:''},6),/title/);
  assert.match(bookmarkProblem({at_s:2,title:'Moment',note:'x'.repeat(2001)},6),/note/);
  assert.equal(bookmarkAnalysis('v',{video_id:'v',analysis_id:'a',status:'completed'}),'a');
  assert.equal(bookmarkAnalysis('v',{video_id:'other',analysis_id:'a',status:'completed'}),null);
  for(const status of ['running','queued','failed','cancelled'])assert.equal(bookmarkAnalysis('v',{video_id:'v',analysis_id:'a',status}),null);
});
test('bookmark writes pin analysis and optimistic revision without changing the source snapshot',async()=>{
  const original=api.request,calls=[],record={bookmark_id:'b/1',video_id:'v',analysis_id:'a',revision:7};api.request=async(path,options)=>{calls.push([path,options]);return record;};
  const draft={at_s:2.5,title:'  Entry  ',note:'  Review later  '};
  try{assert.equal(await bookmarksApi.create('v/1','a',draft),record);await bookmarksApi.update(record,draft);await bookmarksApi.remove(record);
    assert.equal(calls[0][0],'/review/videos/v%2F1/bookmarks');assert.deepEqual(JSON.parse(calls[0][1].body),{analysis_id:'a',at_s:2.5,title:'Entry',note:'Review later'});
    assert.equal(calls[1][0],'/review/bookmarks/b%2F1');assert.equal(calls[1][1].method,'PATCH');assert.deepEqual(JSON.parse(calls[1][1].body),{expected_revision:7,at_s:2.5,title:'Entry',note:'Review later'});
    assert.equal(calls[2][1].method,'DELETE');assert.deepEqual(JSON.parse(calls[2][1].body),{expected_revision:7});assert.equal(draft.title,'  Entry  ');
  }finally{api.request=original;}
});


function bookmarkSession(claims={},updates={}) {
  const payload={sub:'server-subject-a',preferred_username:'same-display-name',zones:['gate','yard'],iat:100,exp:200,...claims};
  const token=`eyJhbGciOiJub25lIn0.${Buffer.from(JSON.stringify(payload)).toString('base64url')}.test-only`;
  return {token,subject:'same-display-name',tenant:'tenant-a',roles:['operator'],clearance:1,expiresAt:200,mode:'keycloak',...updates};
}
test('bookmark scope distinguishes authenticated subjects even with an identical displayed username',()=>{
  const first=bookmarkSession(),second=bookmarkSession({sub:'server-subject-b'});
  assert.notEqual(bookmarkScope(first),bookmarkScope(second));
  assert.equal(bookmarkScope(first),bookmarkScope(bookmarkSession({preferred_username:'renamed-display-name'},{subject:'renamed-display-name'})));
  assert.notEqual(bookmarkScope(first),bookmarkScope(null));
});
test('bookmark scope resets private state when zone access narrows or unrestricted access changes',()=>{
  const full=bookmarkScope(bookmarkSession());
  assert.notEqual(full,bookmarkScope(bookmarkSession({zones:['gate']})));
  assert.notEqual(bookmarkScope(bookmarkSession({zones:[]})),bookmarkScope(bookmarkSession({zones:['gate']})));
  assert.equal(full,bookmarkScope(bookmarkSession({zones:'yard, gate'})));
});
test('bookmark scope isolates tenants and changes in roles or clearance',()=>{
  const initial=bookmarkScope(bookmarkSession());
  assert.notEqual(initial,bookmarkScope(bookmarkSession({}, {tenant:'tenant-b'})));
  assert.notEqual(initial,bookmarkScope(bookmarkSession({}, {roles:['viewer']})));
  assert.notEqual(initial,bookmarkScope(bookmarkSession({}, {clearance:2})));
});
test('unchanged scope survives token renewal and reordered equivalent zone and role claims',()=>{
  const first=bookmarkSession({}, {roles:['operator','commander']});
  const renewed=bookmarkSession({iat:300,exp:400,jti:'rotated-token',zones:['yard','gate','gate']},{expiresAt:400,roles:['commander','operator']});
  assert.notEqual(first.token,renewed.token);
  assert.equal(bookmarkScope(first),bookmarkScope(renewed));
  assert.deepEqual(first.roles,['operator','commander']);
});
