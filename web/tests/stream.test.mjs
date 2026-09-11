import test from 'node:test';
import assert from 'node:assert/strict';
import * as session from '../src/lib/session.ts';
import { connectStream } from '../src/lib/stream.ts';

function storage() { const values = new Map(); return { getItem: (key) => values.get(key) ?? null, setItem: (key, value) => values.set(key, value), removeItem: (key) => values.delete(key) }; }

test('live stream recovers from an initial API outage, authenticates with headers, and refreshes snapshot', async () => {
  const old = { window: globalThis.window, document: globalThis.document, fetch: globalThis.fetch };
  globalThis.window = { sessionStorage: storage(), localStorage: storage(), setTimeout, clearTimeout };
  globalThis.document = { cookie: '' };
  const token = `e30.${Buffer.from(JSON.stringify({ sub: 'operator', tenant: 'site', roles: ['operator'], exp: Date.now() / 1000 + 3600 })).toString('base64url')}.test`;
  let streamAttempts = 0;
  const connections = [];
  const statuses = [];
  let close;
  let resolveMessage;
  const messageReceived = new Promise((resolve) => { resolveMessage = resolve; });
  globalThis.fetch = async (url, init) => {
    if (url === '/auth/config') return new Response(JSON.stringify({ mode: 'dev', required: true, oidc: null }));
    if (url.includes('/dev/token')) return new Response(JSON.stringify({ access_token: token }));
    streamAttempts += 1;
    if (streamAttempts === 1) throw new TypeError('API restarting');
    assert.equal(new Headers(init.headers).get('Authorization'), `Bearer ${token}`);
    return new Response(new ReadableStream({ start(controller) {
      controller.enqueue(new TextEncoder().encode('event: Entity\ndata: {"kind":"Entity","payload":{"entity_id":"e1"}}\n\n'));
      init.signal.addEventListener('abort', () => controller.error(new DOMException('Aborted', 'AbortError')));
    } }), { headers: { 'Content-Type': 'text/event-stream' } });
  };
  try {
    await session.signIn();
    close = connectStream({ onMessage: (message) => { close(); resolveMessage(message); }, onStatus: (value) => statuses.push(value), onConnected: (reconnected) => connections.push(reconnected) });
    const received = await messageReceived;
    assert.equal(received.payload.entity_id, 'e1');
    assert.equal(streamAttempts, 2);
    assert.deepEqual(connections, [false]);
    assert.ok(statuses.includes('reconnecting'));
    assert.ok(statuses.includes('live'));
    assert.equal(statuses.at(-1), 'closed');
  } finally { close?.(); await new Promise((resolve) => setImmediate(resolve)); session.clear(); Object.assign(globalThis, old); }
});
