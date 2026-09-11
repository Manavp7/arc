import test from 'node:test';
import assert from 'node:assert/strict';
import { LatestOperation } from '../src/lib/replay.ts';
import { mergeEntity, entityIsActive, replayEvents } from '../src/lib/entity-state.ts';
import { readSse } from '../../sdk/ts/src/sse.ts';
import { SioClient, SioApiError } from '../../sdk/ts/src/client.ts';

const entity = (last, extra = {}) => ({ entity_id: 'e1', first_seen: '2026-01-01T00:00:00Z', last_seen: last, is_static: false, state: { geo: { lat: 1, lon: 1 } }, ...extra });
test('LIVE and a newer scrub invalidate delayed results and abort outstanding fetches', async () => {
  const operation = new LatestOperation();
  const first = operation.begin();
  const second = operation.begin();
  assert.equal(first.signal.aborted, true);
  assert.equal(first.isCurrent(), false);
  assert.equal(second.isCurrent(), true);
  operation.cancel();
  assert.equal(second.signal.aborted, true);
  assert.equal(second.isCurrent(), false);
});
test('late entity snapshots preserve the latest position and earliest observation', () => {
  const current = entity('2026-01-01T01:00:00Z', { first_seen: '2026-01-01T00:10:00Z', state: { geo: { lat: 9, lon: 9 } } });
  const merged = mergeEntity(current, entity('2026-01-01T00:50:00Z'));
  assert.equal(merged.state.geo.lat, 9);
  assert.equal(merged.first_seen, '2026-01-01T00:00:00Z');
  assert.equal(merged.last_seen, current.last_seen);
});
test('moving entities expire while static fixtures persist without repeated observations', () => {
  const now = Date.parse('2026-01-01T01:00:00Z');
  assert.equal(entityIsActive(entity('2026-01-01T00:54:59Z'), 300, now), false);
  assert.equal(entityIsActive(entity('2026-01-01T00:55:00Z'), 300, now), true);
  assert.equal(entityIsActive(entity('2025-01-01T00:00:00Z', { is_static: true }), 300, now), true);
});
test('scrubs replace event history and playback cannot carry future or duplicate events', () => {
  const event = (id, minute) => ({ event_id: id, ts: `2026-01-01T00:${minute}:00Z` });
  const a = event('a', '01'), b = event('b', '02'), future = event('future', '03');
  assert.deepEqual(replayEvents([a, future], [b, b], b.ts, false), [b]);
  assert.deepEqual(replayEvents([a, b, future], [b], b.ts, true), [b, a]);
  assert.deepEqual(replayEvents([a], [], b.ts, false), []);
});
test('SSE decoder handles split chunks, named and unnamed frames, multiline data and CRLF', async () => {
  const encoder = new TextEncoder();
  const body = new ReadableStream({ start(controller) {
    for (const chunk of [': keepalive\r\nevent: Replay', 'Frame\r\nid: 8\r\ndata: {"a":\r\n', 'data: 1}\r\n\r\ndata: {"b":2}\n\n']) controller.enqueue(encoder.encode(chunk));
    controller.close();
  } });
  const frames = [];
  for await (const frame of readSse(body)) frames.push(frame);
  assert.equal(frames.length, 2);
  assert.equal(frames[0].event, 'ReplayFrame');
  assert.deepEqual(JSON.parse(frames[0].data), { a: 1 });
  assert.equal(frames[1].event, 'message');
  assert.equal(frames[1].id, '8');
});
test('provided credentials never downgrade to the development token issuer after 401', async () => {
  const calls = [];
  const client = new SioClient({ url: 'https://sio.test', token: 'external-token', fetch: async (url) => {
    calls.push(url); return new Response('{"detail":"expired"}', { status: 401 });
  } });
  await assert.rejects(client.entities(), (error) => error instanceof SioApiError && error.status === 401);
  assert.deepEqual(calls, ['https://sio.test/api/entities?active_within_s=300&include_static=false&limit=100']);
});
test('browser token provider is renewed once and SDK preserves structured refusal details', async () => {
  const renewals = [];
  const requests = [];
  const client = new SioClient({ url: '', tokenProvider: async (force) => { renewals.push(force); return force ? 'new' : 'old'; }, fetch: async (_, init) => {
    requests.push(new Headers(init.headers).get('Authorization'));
    return requests.length === 1 ? new Response('{}', { status: 401 }) : new Response(JSON.stringify({ detail: { message: 'Missing objective', outstanding: ['objective A'], fix: 'Complete the objective.' } }), { status: 409 });
  } });
  await assert.rejects(client.request('POST', '/api/mission'), (error) => error.status === 409 && error.detail.fix === 'Complete the objective.');
  assert.deepEqual(renewals, [false, true]);
  assert.deepEqual(requests, ['Bearer old', 'Bearer new']);
});
test('SDK supports successful empty responses', async () => {
  const client = new SioClient({ token: 't', fetch: async () => new Response(null, { status: 204 }) });
  assert.equal(await client.request('DELETE', '/api/resource'), undefined);
});

test('SDK streams refresh a refused provider token once and release the reader on cancellation', async () => {
  const renewals = [];
  let attempts = 0;
  let cancelled = false;
  const client = new SioClient({ tokenProvider: async (force) => { renewals.push(force); return force ? 'fresh' : 'old'; }, fetch: async () => {
    attempts += 1;
    if (attempts === 1) return new Response('{"detail":"expired"}', { status: 401 });
    return new Response(new ReadableStream({ start(controller) {
      controller.enqueue(new TextEncoder().encode('event: Entity\ndata: {"payload":{"entity_id":"e1"}}\n\n'));
    }, cancel() { cancelled = true; } }));
  } });
  const stream = client.subscribe('entities');
  assert.equal((await stream.next()).value.kind, 'Entity');
  await stream.return();
  assert.deepEqual(renewals, [false, true]);
  assert.equal(cancelled, true);
});
