import test from 'node:test';
import assert from 'node:assert/strict';
import { authorizationKey, decodeNavigation, encodeNavigation, navigationIdentity, resumeKey, rememberLocation, readResume } from '../src/lib/investigation-navigation.ts';
import { clipOffset, timelineBounds } from '../src/lib/recording-timeline.ts';

test('exact retained analysis and fractional clip time survive a link round trip', () => {
  const location={tab:'footage',videoId:'vid_abc',analysisId:'ana_old',atS:12.35};
  assert.deepEqual(decodeNavigation(encodeNavigation(location)),location);
  assert.equal(decodeNavigation('?view=cases&case=case_1').caseId,'case_1');
  assert.equal(navigationIdentity(location),navigationIdentity({...location,atS:14}));
  assert.notEqual(navigationIdentity(location),navigationIdentity({...location,analysisId:'ana_new'}));
});
test('links discard credentials, unrelated IDs, invalid times and unsupported destinations', () => {
  assert.equal(decodeNavigation('?view=unknown'),null);
  assert.equal(decodeNavigation('?view=__proto__'),null);
  for(const at of ['-1','Infinity','NaN','181','']) assert.equal(decodeNavigation(`?view=footage&video=vid_a&at=${at}`).atS,undefined);
  assert.equal(encodeNavigation({tab:'cases',caseId:'case_a',videoId:'vid_a',atS:12}),'?view=cases&case=case_a');
  assert.equal(encodeNavigation(decodeNavigation('?view=footage&video=../private&analysis=old&access_token=secret')),'?view=footage');
  assert.equal(encodeNavigation(decodeNavigation('?view=footage&video=vid_a&token=secret')),'?view=footage&video=vid_a');
});
test('resume state is separated by tenant and account and tolerates disabled browser storage', () => {
  const store=new Map();globalThis.localStorage={getItem:k=>store.get(k)??null,setItem:(k,v)=>store.set(k,v)};
  const alice=resumeKey({tenant:'a',subject:'alice'}),bob=resumeKey({tenant:'a',subject:'bob'}),other=resumeKey({tenant:'b',subject:'alice'});
  rememberLocation(alice,{tab:'cases',caseId:'case_a'});
  assert.equal(readResume(alice).caseId,'case_a');assert.equal(readResume(bob),null);assert.equal(readResume(other),null);
  assert.equal(resumeKey(null),null);assert.equal(resumeKey({tenant:'a',subject:'anonymous'}),null);
  globalThis.localStorage={getItem(){throw new Error('disabled')},setItem(){throw new Error('disabled')}};
  assert.equal(readResume(alice),null);assert.doesNotThrow(()=>rememberLocation(alice,{tab:'alerts'}));
  delete globalThis.localStorage;
});
test('shared timeline uses corrected UTC intervals and never assigns unknown clocks a time', () => {
  const start=Date.parse('2026-09-23T08:30:00Z');
  const row={duration_s:10,interval:{start:new Date(start).toISOString(),end:new Date(start+10000).toISOString()}};
  assert.equal(clipOffset(row,start+2500),2.5);assert.equal(clipOffset(row,start-1),null);assert.equal(clipOffset(row,start+10000),null);
  assert.equal(clipOffset({...row,interval:null},start),null);assert.equal(clipOffset(row,NaN),null);
  assert.deepEqual(timelineBounds([row,{...row,interval:null}]),[start,start+10000]);assert.equal(timelineBounds([{interval:null}]),null);
});

test('loaded protected views reset on access changes but survive ordinary token renewal', () => {
  const identity = {tenant:'a',subject:'alice',roles:['operator'],clearance:2};
  const first = authorizationKey(identity,{zones:['dock','yard'],exp:100});
  assert.equal(first,authorizationKey(identity,{zones:'yard,dock',exp:200}));
  for (const changed of [{...identity,tenant:'b'},{...identity,subject:'bob'},{...identity,roles:['viewer']},{...identity,clearance:1}]) assert.notEqual(first,authorizationKey(changed,{zones:['dock','yard']}));
  assert.notEqual(first,authorizationKey(identity,{zones:['dock']}));
  assert.notEqual(first,authorizationKey(identity,{zones:['dock','yard'],pii:true}));
});
