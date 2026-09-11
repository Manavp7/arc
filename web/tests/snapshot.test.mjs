import test from 'node:test';
import assert from 'node:assert/strict';
import { mergeAlertSnapshot, settleConsoleSnapshot } from '../src/lib/snapshot.ts';

const alert = (id, state = 'open') => ({ alert_id: id, state, last_ts: '2026-01-01T00:00:00Z', score: 80 });

test('an unavailable alerts service still produces the successful entity, event and zone snapshots', async () => {
  const entities = [{ entity_id: 'truck-1' }], events = [{ event_id: 'event-1' }], zones = [{ zone_id: 'gate' }];
  const result = await settleConsoleSnapshot({ entities: Promise.resolve(entities), events: Promise.resolve(events), zones: Promise.resolve(zones), alerts: Promise.reject(new Error('service unavailable')) });
  assert.deepEqual(result.entities, entities);
  assert.deepEqual(result.events, events);
  assert.deepEqual(result.zones, zones);
  assert.equal(result.alerts, undefined);
  assert.deepEqual(result.failures, ['Alerts: service unavailable']);
});

test('snapshot failures name each failed section while an empty successful section stays distinguishable', async () => {
  const result = await settleConsoleSnapshot({ entities: Promise.reject(new Error('database offline')), events: Promise.resolve([]), zones: Promise.reject('no geometry'), alerts: Promise.resolve({ alerts: [] }) });
  assert.deepEqual(result.events, []);
  assert.deepEqual(result.alerts, []);
  assert.equal(result.entities, undefined);
  assert.deepEqual(result.failures, ['Entities: database offline', 'Zones: no geometry']);
});

test('late snapshot cannot undo acknowledgement or resolution with an unchanged last_ts', () => {
  const original = alert('a'), started = new Map([['a', original]]);
  for (const state of ['acknowledged', 'resolved']) {
    const changed = { ...original, state };
    const [merged] = mergeAlertSnapshot([alert('a')], [changed], started);
    assert.equal(merged, changed);
    assert.equal(merged.last_ts, original.last_ts);
  }
});

test('snapshot can update an unchanged alert and preserves newly arrived or omitted alerts', () => {
  const original = alert('a'), fresh = alert('a', 'resolved'), streamed = alert('new', 'escalated'), omitted = alert('omitted');
  const started = new Map([['a', original], ['omitted', omitted]]);
  const result = mergeAlertSnapshot([fresh, alert('new')], [original, streamed, omitted], started);
  assert.deepEqual(result, [fresh, streamed, omitted]);
  assert.equal(result[0], fresh);
  assert.equal(result[1], streamed);
});
