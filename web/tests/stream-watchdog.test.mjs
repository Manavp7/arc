import test from 'node:test';
import assert from 'node:assert/strict';
import * as session from '../src/lib/session.ts';
import { connectStream, readSse } from '../src/lib/stream.ts';
import { latestTimestamp } from '../src/lib/freshness.ts';
import { useSioStore } from '../src/store.ts';
const flush = () => new Promise(resolve => setImmediate(resolve));
function fakeTimers() {
  let now = 0, id = 0;
  const tasks = new Map();
  return {
    setTimeout(fn, delay) { tasks.set(++id, { fn, at: now + delay }); return id; },
    clearTimeout(key) { tasks.delete(key); },
    async advance(ms) {
      const until = now + ms;
      for (;;) {
        const next = [...tasks].filter(([, task]) => task.at <= until).sort((a, b) => a[1].at - b[1].at)[0];
        if (!next) break;
        now = next[1].at; tasks.delete(next[0]); next[1].fn(); await flush();
      }
      now = until; await flush();
    },
    count() { return tasks.size; },
  };
}
function storage() { const values = new Map(); return { getItem: key => values.get(key) ?? null, setItem: (key, value) => values.set(key, value), removeItem: key => values.delete(key) }; }
async function environment(respond) {
  const previous = { window: globalThis.window, document: globalThis.document, fetch: globalThis.fetch };
  const timers = fakeTimers();
  globalThis.window = { sessionStorage: storage(), localStorage: storage(), setTimeout: timers.setTimeout, clearTimeout: timers.clearTimeout };
  globalThis.document = { cookie: '' };
  const token = `e30.${Buffer.from(JSON.stringify({ sub: 'operator', tenant: 'site', roles: ['operator'], exp: Date.now() / 1000 + 3600 })).toString('base64url')}.test`;
  globalThis.fetch = async (url, init) => {
    if (url === '/auth/config') return new Response(JSON.stringify({ mode: 'dev', required: true, oidc: null }));
    if (url.includes('/dev/token')) return new Response(JSON.stringify({ access_token: token }));
    return respond(url, init);
  };
  await session.signIn();
  return { timers, async cleanup(close) { close?.(); await flush(); session.clear(); Object.assign(globalThis, previous); } };
}
const bytes = text => new TextEncoder().encode(text);

test('silent half-open reader is cancelled after 45 seconds and reconnects without reloading', async () => {
  let attempts = 0, cancelled = 0;
  const statuses = [], connections = [];
  const env = await environment(() => {
    attempts++;
    return new Response(new ReadableStream({ start(c) { c.enqueue(bytes(': connected\n\n')); }, cancel() { cancelled++; } }), { headers: { 'Content-Type': 'text/event-stream' } });
  });
  let close;
  try {
    close = connectStream({ onMessage() {}, onStatus: value => statuses.push(value), onConnected: value => connections.push(value) });
    await flush();
    assert.equal(statuses.at(-1), 'live');
    await env.timers.advance(44_999);
    assert.equal(cancelled, 0);
    await env.timers.advance(1);
    assert.equal(cancelled, 1);
    assert.equal(statuses.at(-1), 'reconnecting');
    await env.timers.advance(1000);
    assert.equal(attempts, 2);
    assert.deepEqual(connections, [false, true]);
    assert.equal(statuses.at(-1), 'live');
    close(); await flush();
    assert.equal(env.timers.count(), 0);
    await env.timers.advance(100_000);
    assert.equal(attempts, 2);
    assert.equal(statuses.at(-1), 'closed');
  } finally { await env.cleanup(close); }
});

test('raw heartbeat comments reset silence timer without pretending a data message arrived', async () => {
  let source, attempts = 0;
  const activity = [], messages = [], statuses = [];
  const env = await environment(() => { attempts++; return new Response(new ReadableStream({ start(c) { source = c; } }), { headers: { 'Content-Type': 'text/event-stream' } }); });
  let close;
  try {
    close = connectStream({ onMessage: message => messages.push(message), onStatus: value => statuses.push(value), onActivity: at => activity.push(at) });
    await flush();
    assert.equal(statuses.at(-1), 'connecting');
    source.enqueue(bytes(': connected\n\n')); await flush();
    for (let i = 0; i < 3; i++) {
      await env.timers.advance(30_000);
      source.enqueue(bytes(': keepalive\n\n')); await flush();
    }
    assert.equal(attempts, 1);
    assert.equal(statuses.at(-1), 'live');
    assert.equal(activity.length, 4);
    assert.deepEqual(messages, []);
  } finally { await env.cleanup(close); }
});

test('watchdog also aborts fetch waiting for response headers', async () => {
  let attempts = 0, aborted = false;
  const statuses = [];
  const env = await environment((_url, init) => {
    attempts++;
    if (attempts === 1) return new Promise((_resolve, reject) => init.signal.addEventListener('abort', () => { aborted = true; reject(new DOMException('Aborted', 'AbortError')); }, { once: true }));
    return new Response(new ReadableStream({ start(c) { c.enqueue(bytes(': connected\n\n')); } }), { headers: { 'Content-Type': 'text/event-stream' } });
  });
  let close;
  try {
    close = connectStream({ onMessage() {}, onStatus: value => statuses.push(value) });
    await flush(); await env.timers.advance(45_000);
    assert.equal(aborted, true);
    assert.equal(statuses.includes('live'), false);
    await env.timers.advance(1000);
    assert.equal(attempts, 2);
    assert.equal(statuses.at(-1), 'live');
  } finally { await env.cleanup(close); }
});

test('JSON responses and successful REST snapshots cannot mark the stream live', async () => {
  const statuses = [], activities = [];
  const env = await environment(() => new Response('{"ok":true}', { headers: { 'Content-Type': 'application/json' } }));
  let close;
  try {
    useSioStore.getState().reset();
    close = connectStream({ onMessage() {}, onStatus: value => { statuses.push(value); useSioStore.getState().setConnection(value); }, onActivity: at => activities.push(at) });
    await flush();
    useSioStore.getState().replaceEntities([]);
    assert.equal(useSioStore.getState().connection, 'reconnecting');
    assert.equal(useSioStore.getState().lastStreamActivityAt, null);
    assert.equal(statuses.includes('live'), false);
    assert.deepEqual(activities, []);
  } finally { await env.cleanup(close); useSioStore.getState().reset(); }
});

test('SSE abort releases a pending reader and heartbeat activity stays separate from messages', async () => {
  const abort = new AbortController();
  let cancelled = false, activity = 0;
  const body = new ReadableStream({ start(c) { c.enqueue(bytes(': keepalive\n\n')); }, cancel() { cancelled = true; } });
  const pending = readSse(body, { signal: abort.signal, onActivity: () => activity++ }).next();
  await flush();
  assert.equal(activity, 1);
  abort.abort();
  assert.equal((await pending).done, true);
  assert.equal(cancelled, true);
  assert.equal(body.locked, false);
});

test('map freshness chooses latest snapshot or push; heartbeat does not advance data freshness', () => {
  const older = '2026-09-11T06:58:15.468Z', current = '2026-09-11T07:03:00.000Z';
  assert.equal(latestTimestamp(older, current), current);
  assert.equal(latestTimestamp(current, older), current);
  assert.equal(latestTimestamp(null, current), current);
  assert.equal(latestTimestamp('invalid', null), null);
  useSioStore.getState().reset();
  useSioStore.getState().markStreamActivity(current);
  assert.equal(useSioStore.getState().lastStreamActivityAt, current);
  assert.equal(useSioStore.getState().lastMessageAt, null);
  useSioStore.getState().reset();
  assert.equal(useSioStore.getState().lastStreamActivityAt, null);
});
