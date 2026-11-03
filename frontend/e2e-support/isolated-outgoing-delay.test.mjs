import test from 'node:test';
import assert from 'node:assert/strict';
import { EventEmitter } from 'node:events';
import { Buffer } from 'node:buffer';
import { installIsolatedOutgoingDelay } from './isolated-outgoing-delay.mjs';

const doc = '10000000-0000-4000-8000-000000000001', other = '20000000-0000-4000-8000-000000000002';
const connection = '30000000-0000-4000-8000-000000000003', actor = '40000000-0000-4000-8000-000000000004';
const epoch = '50000000-0000-4000-8000-000000000005', id = '60000000-0000-4000-8000-000000000006';
class Endpoint {
  sent = []; closes = []; server = null;
  connectToServer(...args) { assert.deepEqual(args, []); return this.server; }
  onMessage(handler) { this.message = handler; }
  onClose(handler) { this.closed = handler; }
  send(raw) { this.sent.push(raw); }
  async close(options) { this.closes.push(options); }
}
class Page extends EventEmitter {
  async routeWebSocket(url, handler) { this.url = url; this.handler = handler; }
  open() { const route = new Endpoint(), server = new Endpoint(); route.server = server; this.handler(route); return { route, server }; }
}
const json = value => JSON.stringify(value);
const text = (change = {}) => json({ type: 'operation', operation: { type: 'text.update', operation_id: id, epoch, payload: { update: 'PRIVATE_TEXT' }, ...change } });
function ready(pair, documentId = doc) {
  pair.server.message(json({ type: 'auth.ok', actor_id: actor, connection_id: connection, ticket: 'PRIVATE_TICKET' }));
  pair.server.message(json({ type: 'sync', document_id: documentId, epoch, state: 'PRIVATE_STATE' }));
}
async function setup() { const page = new Page(), fault = await installIsolatedOutgoingDelay(page, { targetDocumentId: doc }); const pair = page.open(); ready(pair); return { page, fault, ...pair }; }

test('holds exactly one original UI text frame; all nonmatching frames and all inbound bytes pass unchanged', async () => {
  const value = await setup(); assert.equal(value.page.url, 'ws://127.0.0.1:18220/ws/tactics'); value.fault.arm();
  for (const raw of [json({ type: 'auth', ticket: 'PRIVATE_TICKET' }), json({ type: 'presence', payload: {} }), text({ type: 'stage.command' }), text({ epoch: other }), '{invalid', Buffer.from('binary'), text({ operation_id: 'invalid' })]) {
    value.route.message(raw); assert.equal(value.server.sent.at(-1), raw);
  }
  const original = Buffer.from(text()); const count = value.server.sent.length; value.route.message(original);
  assert.equal(value.server.sent.length, count); assert.equal(value.fault.status().phase, 'held');
  const incoming = json({ type: 'sync', document_id: doc, epoch, state: 'PRIVATE_DELETE_STATE' }); value.server.message(incoming);
  assert.equal(value.route.sent.at(-1), incoming);
  value.route.message('presence-after-held'); assert.equal(value.server.sent.at(-1), 'presence-after-held');
  assert.ok(!/PRIVATE|payload|ticket|state/i.test(JSON.stringify(value.fault.status())));
  assert.equal(await value.fault.releaseHeld(), true); assert.equal(value.server.sent.at(-1), original);
  assert.equal(await value.fault.releaseHeld(), false); assert.equal(value.fault.status().heldCount, 1);
  assert.throws(() => value.fault.arm(), /DELAY_ARM_UNAVAILABLE/); await value.fault.dispose();
});
test('requires a uniquely authenticated target document and leaves another document untouched', async () => {
  const page = new Page(), fault = await installIsolatedOutgoingDelay(page, { targetDocumentId: doc });
  const unrelated = page.open(); ready(unrelated, other); assert.throws(() => fault.arm(), /DELAY_ARM_UNAVAILABLE/);
  const target = page.open(); ready(target); fault.arm();
  unrelated.route.message(text()); assert.equal(unrelated.server.sent.at(-1), text());
  target.route.message(text()); assert.equal(target.server.sent.length, 0);
  await fault.dropHeldAndClose();
  assert.equal(target.route.closes.length, 1); assert.equal(target.server.closes.length, 1);
  assert.equal(unrelated.route.closes.length + unrelated.server.closes.length, 0); await fault.dispose();
});
test('timeout discards held bytes and closes the test connection, never forwarding a late write', async context => {
  context.mock.timers.enable({ apis: ['setTimeout'] });
  const value = await setup(); value.fault.arm(); value.route.message(text());
  context.mock.timers.tick(30_001);
  assert.equal(value.fault.status().held, null); assert.equal(value.fault.status().phase, 'expired');
  assert.equal(await value.fault.releaseHeld(), false);
  assert.equal(value.route.closes.length, 1); assert.equal(value.server.closes.length, 1);
  assert.equal(value.server.sent.length, 0); value.route.message(text()); assert.equal(value.server.sent.length, 0); await value.fault.dispose();
});
test('release checks wall deadline even before a delayed timeout callback is scheduled', async context => {
  context.mock.timers.enable({ apis: ['Date'], now: 1000 });
  const value = await setup(); value.fault.arm(); value.route.message(text());
  context.mock.timers.setTime(31_001);
  assert.equal(await value.fault.releaseHeld(), false); assert.equal(value.server.sent.length, 0);
  assert.equal(value.route.closes.length + value.server.closes.length, 2); await value.fault.dispose();
  const retry = await setup(); retry.fault.arm(); retry.route.message(text());
  context.mock.timers.setTime(61_002);
  retry.route.message(text()); // A new UI retry must not slip through an overdue timer either.
  assert.equal(retry.server.sent.length, 0); assert.equal(retry.fault.status().phase, 'expired');
  assert.equal(retry.route.closes.length + retry.server.closes.length, 2); await retry.fault.dispose();
});
test('dispose and page close discard and close held connections, without automatic flush', async () => {
  const first = await setup(); first.fault.arm(); first.route.message(text());
  assert.equal(await first.fault.dispose(), true); assert.equal(first.fault.status().phase, 'disposed');
  assert.equal(first.fault.status().held, null); assert.equal(first.server.sent.length, 0);
  assert.equal(first.route.closes.length + first.server.closes.length, 2);
  const second = await setup(); second.fault.arm(); second.route.message(text()); second.page.emit('close');
  await Promise.resolve(); assert.equal(second.fault.status().held, null); assert.equal(second.server.sent.length, 0);
  assert.equal(second.route.closes.length + second.server.closes.length, 2);
});
test('natural close or epoch change permanently prevents release under a new scope', async () => {
  const first = await setup(); first.fault.arm(); first.route.message(text()); first.server.closed(1008, 'authorization_expired');
  assert.equal(await first.fault.releaseHeld(), false); assert.equal(first.server.sent.length, 0);
  assert.deepEqual(first.route.closes, [{ code: 1008, reason: 'authorization_expired' }]); await first.fault.dispose();
  const second = await setup(); second.fault.arm(); second.route.message(text());
  second.server.message(json({ type: 'sync', document_id: doc, epoch: other }));
  assert.equal(await second.fault.releaseHeld(), false); assert.equal(second.server.sent.length, 0);
  assert.equal(second.route.closes.length + second.server.closes.length, 2); await second.fault.dispose();
});
test('scope/installation are strict; oversize frames are not retained and passthrough remains intact', async () => {
  const page = new Page();
  await assert.rejects(installIsolatedOutgoingDelay(page, { targetDocumentId: 'invalid' }), /DELAY_INVALID_SCOPE/);
  await assert.rejects(installIsolatedOutgoingDelay(page, { targetDocumentId: doc, timeoutMs: 999999 }), /DELAY_INVALID_SCOPE/);
  const fault = await installIsolatedOutgoingDelay(page, { targetDocumentId: doc });
  await assert.rejects(installIsolatedOutgoingDelay(page, { targetDocumentId: doc }), /DELAY_ALREADY_INSTALLED_ON_PAGE/);
  const pair = page.open(); ready(pair); fault.arm();
  const large = Buffer.alloc(2 * 1024 * 1024 + 1); pair.route.message(large);
  assert.equal(pair.server.sent.at(-1), large); assert.equal(fault.status().held, null);
  await fault.dispose();
});
