import test from 'node:test';
import assert from 'node:assert/strict';
import { api } from '../src/lib/api.ts';
import { annotationCaseKey, annotationCasesApi, frozenAnnotationCase, reportMissCase } from '../src/lib/annotation-cases.ts';
import { caseEvidenceItems, recordedEvidenceItems, evidencePosition, evidenceOriginLabel } from '../src/lib/case-attachments.ts';

const observation = {annotation_id:'human-1',zone_id:'gate',event_type:'entry',start_s:4,end_s:5,note:'Observed crossing'};
const frozen = {video_id:'video-1',annotation_set_id:'frozen-1',annotation_hash:'sha256',frozen_at:'2026-09-11T08:00:00Z',annotations:[observation]};
const analysis = {video_id:'video-1',analysis_id:'retained-1',status:'completed'};
const expected = {video_id:'video-1',analysis_id:'retained-1',annotation_set_id:'frozen-1',annotation_id:'human-1'};
const clip = {video_id:'video-1',analysis_id:'retained-1',annotation_set_id:'frozen-1',misses:[observation]};
const report = {report_id:'report-1',candidates:[{candidate:1,clips:[clip]}]};

test('human case source uses frozen IDs and a completed exact recording analysis without fabricated detector data', () => {
  const result = frozenAnnotationCase('video-1',frozen,'human-1',analysis);
  assert.deepEqual(result,expected);
  assert.equal('event_id' in result,false); assert.equal('confidence' in result,false); assert.equal('note' in result,false);
  assert.equal('evaluation_report_id' in result,false);
  assert.deepEqual(frozen.annotations,[observation]);
});
test('mutable drafts, mismatched runs, unfinished analyses and labels outside the frozen set cannot create cases', () => {
  assert.throws(()=>frozenAnnotationCase('video-1',{annotations:[observation]},'human-1',analysis),/freeze/);
  assert.throws(()=>frozenAnnotationCase('video-1',{...frozen,annotation_hash:''},'human-1',analysis),/freeze/);
  assert.throws(()=>frozenAnnotationCase('other',frozen,'human-1',analysis),/different recording/);
  assert.throws(()=>frozenAnnotationCase('video-1',frozen,'human-1',{...analysis,video_id:'other'}),/completed analysis/);
  for (const status of ['queued','running','failed','cancelled']) assert.throws(()=>frozenAnnotationCase('video-1',frozen,'human-1',{...analysis,status}),/completed analysis/);
  assert.throws(()=>frozenAnnotationCase('video-1',frozen,'draft-only',analysis),/selected frozen/);
});
test('report misses pin the saved report and actual matching candidate source', () => {
  assert.deepEqual(reportMissCase(report,{...clip,misses:[]},'human-1'),{...expected,evaluation_report_id:'report-1'});
  assert.throws(()=>reportMissCase(report,{...clip,analysis_id:'new-run'},'human-1'),/not a missed incident/);
  assert.throws(()=>reportMissCase(report,{...clip,annotation_set_id:'other-set'},'human-1'),/not a missed incident/);
  assert.throws(()=>reportMissCase(report,clip,'invented-label'),/not a missed incident/);
  assert.throws(()=>reportMissCase({...report,report_id:''},clip,'human-1'),/saved report/);
  assert.throws(()=>reportMissCase({...report,candidates:[{clips:[{...clip,misses:[]}]}]},clip,'human-1'),/not a missed incident/);
});
test('UI duplicate identity matches backend across report contexts while respecting retained analysis and frozen set', () => {
  assert.equal(annotationCaseKey(expected),annotationCaseKey({...expected,evaluation_report_id:'report-1'}));
  assert.notEqual(annotationCaseKey(expected),annotationCaseKey({...expected,analysis_id:'another-run'}));
  assert.notEqual(annotationCaseKey(expected),annotationCaseKey({...expected,annotation_set_id:'another-set'}));
});
test('create and attach use the configured API boundary and retain server duplicate response unchanged', async () => {
  const original=api.request,calls=[],reused={case_id:'existing',evidence:{source:'reviewer_annotation'}};
  api.request=async(path,options)=>{calls.push([path,options]);return reused;};
  try {
    assert.equal(await annotationCasesApi.create({...expected,evaluation_report_id:'report-1'}),reused);
    assert.equal(await annotationCasesApi.attach('case/one',expected,9,'  second camera  '),reused);
    assert.equal(calls[0][0],'/cases'); assert.equal(calls[0][1].method,'POST');
    assert.deepEqual(JSON.parse(calls[0][1].body),{...expected,evaluation_report_id:'report-1'});
    assert.equal(calls[1][0],'/cases/case%2Fone/evidence');
    assert.deepEqual(JSON.parse(calls[1][1].body),{...expected,expected_revision:9,note:'second camera'});
    assert.equal(evidenceOriginLabel({evidence:reused.evidence}),'Human observation');
  } finally {api.request=original;}
});
test('human observations remain recording evidence for comparisons and packages, without event IDs', () => {
  const record={...expected,case_id:'case-1',created_by:'reviewer',created_at:'2026-09-11T09:00:00Z',at_s:99,evidence:{source:'reviewer_annotation',annotation:observation},evidence_attachments:[{attachment_id:'detector',video_id:'video-2',analysis_id:'retained-2',event_id:'actual-event',evidence:{source:'recorded_file',event:{at_s:8}}},{attachment_id:'alert',evidence:{source:'platform_alert'}}]};
  const items=recordedEvidenceItems(record);
  assert.equal(items.length,2);assert.equal(caseEvidenceItems(record).length,3);
  assert.equal(items[0].annotation_id,'human-1');assert.equal(items[0].annotation_set_id,'frozen-1');
  assert.equal(items[0].event_id,undefined);assert.equal(evidencePosition(items[0]),4);assert.equal(evidencePosition(items[1]),8);
  assert.equal(evidenceOriginLabel(items[0]),'Human observation');
  assert.equal(evidenceOriginLabel({...items[0],evaluation_report_id:'not-authoritative'}),'Human observation');
  assert.equal(evidenceOriginLabel({...items[0],evidence:{...items[0].evidence,evaluation_report:{report_id:'original-report'}}}),'Human observation · evaluated miss');
});
