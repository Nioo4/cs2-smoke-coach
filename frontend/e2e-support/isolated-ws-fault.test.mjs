import test from 'node:test';
import assert from 'node:assert/strict';
import { EventEmitter } from 'node:events';
import { Buffer } from 'node:buffer';
import { installIsolatedWsFault } from './isolated-ws-fault.mjs';

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
const operation = () => json({ type: 'operation', operation: { type: 'text.update', epoch, operation_id: id, payload: { update: 'PRIVATE_UPDATE' } } });
const applied = (extra = {}) => json({ type: 'operation.applied', document_id: doc, epoch, revision: 2, connection_id: connection, actor_id: actor, operation_id: id, operation_type: 'text.update', state: 'PRIVATE_STATE', ...extra });
function ready(pair, documentId = doc) {
  pair.server.message(json({ type: 'auth.ok', connection_id: connection, actor_id: actor, ticket: 'PRIVATE_TICKET' }));
  pair.server.message(json({ type: 'sync', document_id: documentId, epoch, state: 'PRIVATE_STATE' }));
  pair.route.sent.length = 0;
}
async function setup() { const page = new Page(), fault = await installIsolatedWsFault(page, { targetDocumentId: doc }); const pair = page.open(); ready(pair); return { page, fault, ...pair }; }

test('holds one own real applied frame, preserves all other bytes and exposes metadata only', async () => {
  const { page, fault, route, server } = await setup();
  assert.equal(page.url, 'ws://127.0.0.1:18220/ws/tactics'); fault.arm();
  const outgoing = operation(); route.message(outgoing); assert.equal(server.sent[0], outgoing);
  for (const raw of [applied({ document_id: other }), applied({ connection_id: other }), applied({ actor_id: other }), applied({ epoch: other }), applied({ operation_id: other }), '{broken', Buffer.from('binary')]) {
    server.message(raw); assert.equal(route.sent.at(-1), raw);
  }
  const original = Buffer.from(applied()); const count = route.sent.length; server.message(original);
  assert.equal(route.sent.length, count); assert.equal(fault.status().phase, 'held');
  const metadata = fault.status(); assert.equal(metadata.held.bytes, original.byteLength);
  assert.ok(!/PRIVATE|payload|ticket|state/i.test(JSON.stringify(metadata)));
  const second = applied(); server.message(second); assert.equal(route.sent.at(-1), second);
  assert.equal(fault.releaseHeld(), true); assert.equal(route.sent.at(-1), original);
  assert.equal(fault.releaseHeld(), false); assert.equal(fault.status().heldCount, 1);
  assert.throws(() => fault.arm(), /FAULT_ARM_UNAVAILABLE/); fault.dispose();
});
test('ACK requires an operation observed on that route after arming; other documents cannot arm', async () => {
  const page = new Page(), fault = await installIsolatedWsFault(page, { targetDocumentId: doc });
  const unrelated = page.open(); ready(unrelated, other);
  assert.throws(() => fault.arm(), /FAULT_ARM_UNAVAILABLE/);
  const pair = page.open(); ready(pair); fault.arm();
  const ack = json({ type: 'operation.ack', document_id: doc, epoch, revision: 2, current_revision: 4, duplicate: true, operation_id: id });
  pair.server.message(ack); assert.equal(pair.route.sent.at(-1), ack);
  pair.route.message(operation()); pair.server.message(ack);
  assert.equal(fault.status().phase, 'held');
  await fault.dropHeldAndRestore({ closeConnection: true });
  assert.equal(pair.route.closes.length, 1); assert.equal(pair.server.closes.length, 1);
  assert.equal(unrelated.route.closes.length, 0); assert.equal(unrelated.server.closes.length, 0);
  assert.equal(fault.status().held, null); fault.dispose();
});
test('drop restores passthrough without closing; natural closure clears held bytes and forwards close', async () => {
  const first = await setup(); first.fault.arm(); first.route.message(operation()); first.server.message(applied());
  await first.fault.dropHeldAndRestore();
  first.server.message(applied()); assert.equal(first.route.sent.at(-1), applied()); assert.equal(first.route.closes.length, 0); first.fault.dispose();
  const second = await setup(); second.fault.arm(); second.route.message(operation()); second.server.message(applied());
  second.server.closed(1008, 'authorization_expired');
  assert.equal(second.fault.status().held, null); assert.equal(second.fault.status().phase, 'closed');
  assert.deepEqual(second.route.closes, [{ code: 1008, reason: 'authorization_expired' }]); second.fault.dispose();
});
test('deadline drops held memory, disarms and allows later real messages; page close disposes', async context => {
  context.mock.timers.enable({ apis: ['setTimeout'] });
  const value = await setup(); value.fault.arm(); value.route.message(operation()); value.server.message(applied());
  context.mock.timers.tick(30_001);
  assert.equal(value.fault.status().phase, 'expired'); assert.equal(value.fault.status().held, null);
  value.server.message(applied()); assert.equal(value.route.sent.at(-1), applied());
  value.page.emit('close'); assert.equal(value.fault.status().phase, 'disposed'); assert.equal(value.fault.status().connections, 0);
});
test('rejects ambiguous install/scope and fails transparent on oversize frames', async () => {
  const page = new Page();
  await assert.rejects(installIsolatedWsFault(page, { targetDocumentId: 'not-a-uuid' }), /FAULT_INVALID_SCOPE/);
  const fault = await installIsolatedWsFault(page, { targetDocumentId: doc });
  await assert.rejects(installIsolatedWsFault(page, { targetDocumentId: doc }), /FAULT_ALREADY_INSTALLED_ON_PAGE/);
  const pair = page.open(); ready(pair); fault.arm();
  const large = Buffer.alloc(8 * 1024 * 1024 + 1); pair.server.message(large);
  assert.equal(pair.route.sent.at(-1), large); assert.equal(fault.status().phase, 'frame_limit'); assert.equal(fault.status().held, null); fault.dispose();
});
test('operation bookkeeping has a fixed cap and cannot interfere with future forwarding', async () => {
  const value = await setup(); value.fault.arm();
  for (let index = 1; index <= 33; index++) value.route.message(json({ type: 'operation', operation: { type: 'text.update', epoch, operation_id: `60000000-0000-4000-8000-${String(index).padStart(12, '0')}` } }));
  assert.equal(value.server.sent.length, 33); assert.equal(value.fault.status().phase, 'operation_limit');
  value.server.message(applied()); assert.equal(value.route.sent.at(-1), applied()); value.fault.dispose();
});
test('disposal drops held bytes without closing any socket and leaves both directions transparent', async () => {
  const value = await setup(); value.fault.arm(); value.route.message(operation()); value.server.message(applied());
  value.fault.dispose();
  assert.equal(value.fault.status().held, null); assert.equal(value.fault.status().connections, 0);
  value.server.message(applied()); assert.equal(value.route.sent.at(-1), applied());
  value.route.message('unchanged'); assert.equal(value.server.sent.at(-1), 'unchanged');
  assert.equal(value.route.closes.length + value.server.closes.length, 0);
  assert.throws(() => value.fault.arm(), /FAULT_ARM_UNAVAILABLE/);
});
