import test from 'node:test';
import assert from 'node:assert/strict';
import { attachmentRequest, caseEvidenceItems, recordedEvidenceCases, recordedEvidenceItems } from '../src/lib/case-attachments.ts';

test('package case selection includes an alert-origin summary with attached recording counts', () => {
  const alertWithVideo={case_id:'alert-case',video_id:null,analysis_id:null,recorded_evidence_count:1};
  const originalVideo={case_id:'video-case',video_id:'v',analysis_id:'a'};
  const platformOnly={case_id:'plain',video_id:null,recorded_evidence_count:0};
  assert.deepEqual(recordedEvidenceCases([alertWithVideo,originalVideo,platformOnly]),[alertWithVideo,originalVideo]);
});
test('legacy case detail gains one original item and retains exact analysis references', () => {
  const record={case_id:'one',title:'Origin',created_by:'alice',created_at:'2026-09-11T10:00:00Z',video_id:'v',analysis_id:'retained-old',event_id:'e',at_s:3,evidence:{source:'recorded_file'},evidence_attachments:[{attachment_id:'added',video_id:'other',analysis_id:'other-old',event_id:'other-event',evidence:{source:'recorded_file'}}]};
  const sources=caseEvidenceItems(record);
  assert.equal(sources.length,2);assert.equal(sources[0].attachment_id,'original');assert.equal(sources[0].analysis_id,'retained-old');
  assert.equal(recordedEvidenceItems(record)[1].analysis_id,'other-old');
  assert.equal('evidence_items' in record,false);
});
test('server-provided chronological items are preserved and platform evidence is excluded from clip choices', () => {
  const timeline=[{attachment_id:'earlier-alert',evidence:{source:'platform_alert'}},{attachment_id:'original',video_id:'v',analysis_id:'a',evidence:{source:'recorded_file'}}];
  const record={evidence_items:timeline};
  assert.equal(caseEvidenceItems(record),timeline);
  assert.deepEqual(recordedEvidenceItems(record),[timeline[1]]);
});
test('attachment requests contain only authoritative IDs, revision and authored text', () => {
  const source={kind:'video_event',id:'e',video_id:'v',analysis_id:'old',title:'Client display label',evidence:{forged:true},at_s:999};
  assert.deepEqual(attachmentRequest(source,7,'  Related view  '),{expected_revision:7,video_id:'v',analysis_id:'old',event_id:'e',note:'Related view'});
  assert.deepEqual(attachmentRequest({kind:'alert',id:'alert'},3,''),{expected_revision:3,alert_id:'alert',note:''});
  assert.throws(()=>attachmentRequest({kind:'video_event',id:'e',video_id:'v'},1,''),/persisted/);
  assert.throws(()=>attachmentRequest({kind:'entity',id:'someone'},1,''),/persisted/);
});
