import test from 'node:test';
import assert from 'node:assert/strict';
import { unreadSnapshot, appendNotificationPage, notificationCount, notificationTime, notificationApi } from '../src/lib/notifications.ts';
import { api } from '../src/lib/api.ts';

const notice = (id, revision, created_at='2026-09-11T12:00:00Z', read_at=null) => ({notification_id:id,revision,created_at,read_at});

test('mark-visible snapshot contains only displayed unread ids and observed revisions', () => {
  const rows=[notice('one',1),notice('read',2,undefined,'2026-09-11T12:01:00Z'),notice('one',3),notice('two',4)];
  assert.deepEqual(unreadSnapshot(rows),[{notification_id:'one',revision:3},{notification_id:'two',revision:4}]);
  assert.equal(unreadSnapshot(Array.from({length:101},(_,i)=>notice(String(i),1))).length,100);
  assert.equal(rows[0].read_at,null);
});
test('notification pages deduplicate overlapping ids and preserve creation-time order after read edits', () => {
  const first=[notice('new',1,'2026-09-11T14:00:00Z'),notice('old',1,'2026-09-11T12:00:00Z')];
  const merged=appendNotificationPage(first,[notice('old',2,'2026-09-11T12:00:00Z','2026-09-11T15:00:00Z'),notice('older',1,'2026-09-11T11:00:00Z')]);
  assert.deepEqual(merged.map(row=>row.notification_id),['new','old','older']);
  assert.equal(merged[1].revision,2);assert.equal(first[1].revision,1);
});
test('notification counts disclose bounded totals and time labels handle invalid input', () => {
  assert.equal(notificationCount(0,true),'0');assert.equal(notificationCount(5,false),'5+');assert.equal(notificationCount(200,true),'99+');
  assert.equal(notificationCount(NaN,true),'0');assert.equal(notificationCount(-1,true),'0');
  const now=Date.parse('2026-09-11T12:00:00Z');
  assert.equal(notificationTime('2026-09-11T11:59:40Z',now),'Just now');
  assert.equal(notificationTime('2026-09-11T11:58:00Z',now),'2m ago');
  assert.equal(notificationTime('2026-09-11T10:00:00Z',now),'2h ago');
  assert.equal(notificationTime('bad',now),'Time unavailable');
});
test('notification API scopes mark snapshot and revalidates a target instead of navigating cached data', async () => {
  const original=api.request, calls=[];
  api.request=async (path,options)=>{calls.push([path,options]);return {target:{kind:'case',id:'case-a'}};};
  try{
    const signal=new AbortController().signal;
    await notificationApi.list(true,'opaque+cursor',signal);
    await notificationApi.mark([notice('one',7),notice('read',8,undefined,'read')],signal);
    await notificationApi.target('notice/with space',signal);
    assert.match(calls[0][0],/unread_only=true/);assert.match(calls[0][0],/before=opaque%2Bcursor/);
    assert.deepEqual(JSON.parse(calls[1][1].body),{notifications:[{notification_id:'one',revision:7}]});
    assert.equal(calls[2][0],'/notifications/notice%2Fwith%20space/target');assert.equal(calls[2][1].signal,signal);
  }finally{api.request=original;}
});
