import { test } from 'node:test';
import assert from 'node:assert/strict';
import { hasUnconfirmedWork, sendAuth, leaseGrantMessage, leaseRenewedMessage, parseLeaseGrant, mapMoveOperation, settleProtocolError, shouldFinishLocalMapMove } from '../src/client-protocol.mjs';
import { LeaseManager } from '../src/leases.mjs';

test('browser websocket auth sends the supplied ticket value, not ambient DOM state', () => {
  let payload;
  const socket = { send: (value) => { payload = value; } };
  const ticket = 't0-local-test-ticket-'.padEnd(48, 'x');
  sendAuth(socket, ticket);
  assert.deepEqual(JSON.parse(payload), { type: 'auth', ticket });
  assert.throws(() => sendAuth(socket, ''), /invalid_test_ticket/);
});

test('real lease acquisition round-trips the wire grant into a valid map.move payload', () => {
  const manager = new LeaseManager({ now: () => 1234 });
  const acquired = manager.acquire({
    objectId: 'role-a-start', version: 1, connectionId: 'connection-a', currentVersion: 1,
    actor: { id: 'actor-a', name: '参与者 A', color: '#cb5a42' },
  });
  const message = leaseGrantMessage('request-1', acquired);
  assert.deepEqual(Object.keys(message).sort(), ['expires_at', 'object_id', 'renew_every_ms', 'request_id', 'token', 'type'].sort());
  assert.equal('objectId' in message, false);
  const lease = parseLeaseGrant(message);
  assert.deepEqual(lease, {
    requestId: 'request-1', objectId: 'role-a-start', token: acquired.token,
    expiresAt: acquired.expiresAt, renewEveryMs: acquired.renewEveryMs,
  });
  const operation = mapMoveOperation({
    operationId: 'f590d0f2-92b4-48c0-8a5f-8e73aac4bd4a', epoch: 'epoch-1',
    drag: { objectId: lease.objectId, stepId: 'step-1', version: 1, x: 80, y: 60 }, lease,
  });
  assert.deepEqual(operation.payload, { step_id: 'step-1', object_id: 'role-a-start', token: acquired.token, version: 1, x: 80, y: 60 });
  assert.deepEqual(leaseRenewedMessage('renew-1', { objectId: lease.objectId, expiresAt: 2345, renewEveryMs: lease.renewEveryMs }), {
    type: 'lease.renewed', request_id: 'renew-1', object_id: 'role-a-start', expires_at: 2345, renew_every_ms: lease.renewEveryMs,
  });
});

test('remote map.move broadcasts cannot finish a different local drag', () => {
  const message = { operation_type: 'map.move', actor_id: 'actor-b' };
  assert.equal(shouldFinishLocalMapMove(message, false), false);
  assert.equal(shouldFinishLocalMapMove({ operation_type: 'text.update' }, true), false);
  assert.equal(shouldFinishLocalMapMove(message, true), true);
});

test('locked-object notice is shown after move cleanup so it is not overwritten', () => {
  const events = [];
  settleProtocolError('COLLAB_LOCKED', true, () => events.push('finish'), (message) => events.push(message));
  assert.deepEqual(events, ['finish', '该对象正由另一位参与者拖动。']);
});

test('before-unload guard covers pending, rejected, recovery, failed, and composing text without clearing it', () => {
  const clean = { pending: new Map(), localUnconfirmed: false, rejectedLocal: false, recoveryDraft: null, failedOperations: [], composing: new Map() };
  assert.equal(hasUnconfirmedWork(clean), false);
  for (const change of [
    (state) => state.pending.set('op-1', {}),
    (state) => { state.localUnconfirmed = true; },
    (state) => { state.rejectedLocal = true; },
    (state) => { state.recoveryDraft = { fields: { note: 'recovery' } }; },
    (state) => { state.failedOperations.push({ type: 'text.update' }); },
    (state) => state.composing.set('note', { base: 'composition' }),
  ]) {
    const state = structuredClone(clean);
    change(state);
    assert.equal(hasUnconfirmedWork(state), true);
    assert.equal(state.pending.size > 0 || state.recoveryDraft !== null || state.failedOperations.length > 0 || state.composing.size > 0 || state.localUnconfirmed || state.rejectedLocal, true);
  }
});
