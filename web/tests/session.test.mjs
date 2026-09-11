import test from 'node:test';
import assert from 'node:assert/strict';

const token = (claims = {}) => `e30.${Buffer.from(JSON.stringify({ sub: 'console', tenant: 'test-site', roles: ['operator'], exp: Math.floor(Date.now() / 1000) + 3600, ...claims })).toString('base64url')}.signature`;
const json = (body, status = 200) => new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });
function storage() { const map = new Map(); return { getItem: (key) => map.get(key) ?? null, setItem: (key, value) => map.set(key, value), removeItem: (key) => map.delete(key) }; }
let count = 0;
async function environment(mode = 'dev') {
  const original = { window: globalThis.window, document: globalThis.document, fetch: globalThis.fetch };
  globalThis.window = { sessionStorage: storage(), localStorage: storage(), location: { origin: 'https://console.test', pathname: '/', href: 'https://console.test/', assign(url) { this.redirect = url; } }, history: { replaceState(_state, _unused, url) { window.location.href = url; } } };
  globalThis.document = { cookie: '' };
  const auth = { mode, required: true, oidc: mode === 'dev' ? null : {
    issuer: 'https://id.test/realms/site', client_id: 'sio-console', authorization_endpoint: 'https://id.test/authorize', token_endpoint: 'https://id.test/token', logout_endpoint: 'https://id.test/logout',
  } };
  const requests = [];
  let responder = async () => json({ access_token: token() });
  globalThis.fetch = async (url, init) => { requests.push({ url, init }); return url === '/auth/config' ? json(auth) : responder(url, init); };
  const session = await import(`../src/lib/session.ts?test=${++count}`);
  return { session, requests, respond: (fn) => { responder = fn; }, cleanup: () => Object.assign(globalThis, original) };
}

test('console requires explicit development sign-in and sends no bearer cookie', async () => {
  const env = await environment();
  try {
    await env.session.initialize();
    assert.equal(env.session.peek(), null);
    await assert.rejects(env.session.ensure(), /Sign in/);
    assert.equal(env.requests.filter((item) => item.url.includes('/dev/token')).length, 0);
    env.respond(async () => json({ access_token: token({ roles: ['commander'], clearance: 2 }) }));
    await env.session.signIn('commander');
    assert.equal(env.session.can('decision.approve'), true);
    assert.equal(env.session.can('integration.write'), false);
    assert.equal(window.localStorage.getItem('sio.token'), null);
    assert.ok(!document.cookie.includes('signature'));
    assert.match(env.session.headers().Authorization, /^Bearer /);
    await env.session.signOut();
    assert.equal(env.session.peek(), null);
    assert.deepEqual(env.session.headers(), {});
  } finally { env.cleanup(); }
});

test('sign-out during renewal cannot resurrect a session', async () => {
  const env = await environment();
  try {
    await env.session.signIn();
    let resolve;
    env.respond(() => new Promise((done) => { resolve = done; }));
    const pending = env.session.ensure(true);
    await new Promise((done) => setTimeout(done, 0));
    env.session.clear();
    resolve(json({ access_token: token() }));
    await assert.rejects(pending, /session has ended/);
    assert.equal(env.session.peek(), null);
    assert.equal(window.sessionStorage.getItem('sio.session.v2'), null);
  } finally { env.cleanup(); }
});

test('OIDC uses PKCE, validates callback state, renews with refresh token, and signs out', async () => {
  const env = await environment('keycloak');
  try {
    await env.session.signIn();
    const authorization = new URL(window.location.redirect);
    const transaction = JSON.parse(window.sessionStorage.getItem('sio.oidc.transaction'));
    const challenge = Buffer.from(await crypto.subtle.digest('SHA-256', new TextEncoder().encode(transaction.verifier))).toString('base64url');
    assert.equal(authorization.searchParams.get('code_challenge'), challenge);
    assert.equal(authorization.searchParams.get('code_challenge_method'), 'S256');
    window.location.href = `https://console.test/?code=authorization-code&state=${transaction.state}`;
    const grants = [];
    env.respond(async (url, init) => {
      assert.equal(url, 'https://id.test/token');
      grants.push(new URLSearchParams(init.body));
      return json({ access_token: token({ roles: [], realm_access: { roles: ['commander'] }, clearance: 2 }), refresh_token: 'refresh-one', id_token: 'id-one' });
    });
    await env.session.initialize();
    assert.equal(grants[0].get('code_verifier'), transaction.verifier);
    assert.equal(env.session.peek().tenant, 'test-site');
    assert.equal(env.session.can('decision.approve'), true);
    assert.equal(new URL(window.location.href).searchParams.has('code'), false);
    await env.session.ensure(true);
    assert.equal(grants[1].get('grant_type'), 'refresh_token');
    assert.equal(grants[1].get('refresh_token'), 'refresh-one');
    assert.equal(env.requests.some((item) => item.url.includes('/dev/token')), false);
    await env.session.signOut();
    assert.equal(env.session.peek(), null);
    assert.equal(new URL(window.location.redirect).pathname, '/logout');
  } finally { env.cleanup(); }
});

test('OIDC refuses mismatched state before exchanging the authorization code', async () => {
  const env = await environment('keycloak');
  try {
    await env.session.signIn();
    window.location.href = 'https://console.test/?code=stolen-code&state=wrong';
    await assert.rejects(env.session.initialize(), /could not be verified/);
    assert.equal(env.requests.filter((item) => item.url === 'https://id.test/token').length, 0);
    assert.equal(env.session.peek(), null);
  } finally { env.cleanup(); }
});

test('an expired OIDC refresh grant returns to sign-in without minting a dev identity', async () => {
  const env = await environment('keycloak');
  try {
    await env.session.signIn();
    const transaction = JSON.parse(window.sessionStorage.getItem('sio.oidc.transaction'));
    window.location.href = `https://console.test/?code=ok&state=${transaction.state}`;
    env.respond(async () => json({ access_token: token(), refresh_token: 'refresh' }));
    await env.session.initialize();
    env.respond(async () => json({ error: 'invalid_grant' }, 400));
    await assert.rejects(env.session.ensure(true), /session has ended/);
    assert.equal(env.session.peek(), null);
    assert.equal(env.requests.some((item) => item.url.includes('/dev/token')), false);
  } finally { env.cleanup(); }
});

test('renewal notifies the same subscriber with new expiry, claims and permissions, then sign-out', async () => {
  const env = await environment();
  const observed = [];
  const unsubscribe = env.session.subscribeSession(() => observed.push({ identity: env.session.peek(), mayApprove: env.session.can('decision.approve') }));
  try {
    const initialExpiry = Math.floor(Date.now() / 1000) + 120;
    const renewedExpiry = initialExpiry + 3600;
    env.respond(async () => json({ access_token: token({ exp: initialExpiry, roles: ['operator'], clearance: 1 }) }));
    await env.session.signIn('operator');
    const initial = env.session.peek();
    assert.equal(initial.expiresAt, initialExpiry);
    assert.equal(env.session.can('decision.approve'), false);
    env.respond(async () => json({ access_token: token({ exp: renewedExpiry, roles: ['commander'], clearance: 2, preferred_username: 'renewed-operator' }) }));
    const renewed = await env.session.ensure(true);
    assert.notEqual(renewed, initial);
    assert.equal(env.session.peek(), renewed);
    assert.equal(renewed.expiresAt, renewedExpiry);
    assert.equal(renewed.subject, 'renewed-operator');
    assert.equal(env.session.can('decision.approve'), true);
    assert.deepEqual(observed, [{ identity: initial, mayApprove: false }, { identity: renewed, mayApprove: true }]);
    await env.session.signOut();
    assert.deepEqual(observed.at(-1), { identity: null, mayApprove: false });
    unsubscribe();
    env.session.clear();
    assert.equal(observed.length, 3);
  } finally { unsubscribe(); env.cleanup(); }
});
