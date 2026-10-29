import { test } from 'node:test';
import assert from 'node:assert/strict';
import { authenticateSocket, closeSocketAuth, createSocketAuthState } from '../src/socket-auth.mjs';

test('socket closed during async connect is disconnected instead of left as a ghost participant', async () => {
  let finishConnect;
  const disconnects = [];
  const connection = { id: 'pending-connection' };
  const core = {
    connect: () => new Promise((resolve) => { finishConnect = resolve; }),
    disconnect: (value) => disconnects.push(value),
  };
  const socket = { OPEN: 1, readyState: 1 };
  const state = createSocketAuthState();
  const pending = authenticateSocket({ core, socket, state, connectionId: 'pending-connection', actor: { id: 'actor-a' }, send() {} });

  assert.equal(state.authenticating, true);
  closeSocketAuth(core, state);
  socket.readyState = 3;
  finishConnect(connection);

  assert.equal(await pending, null);
  assert.deepEqual(disconnects, [connection]);
  assert.equal(state.connection, null);
  assert.equal(state.authenticating, false);
});

test('parallel auth attempts cannot overwrite one socket connection', async () => {
  let finishConnect;
  const disconnects = [];
  const connection = { id: 'single-connection' };
  const core = {
    connect: () => new Promise((resolve) => { finishConnect = resolve; }),
    disconnect: (value) => disconnects.push(value),
  };
  const socket = { OPEN: 1, readyState: 1 };
  const state = createSocketAuthState();
  const first = authenticateSocket({ core, socket, state, connectionId: 'first', actor: { id: 'actor-a' }, send() {} });
  const second = await authenticateSocket({ core, socket, state, connectionId: 'second', actor: { id: 'actor-b' }, send() {} });
  assert.equal(second, null);
  finishConnect(connection);
  assert.equal(await first, connection);
  assert.equal(state.connection, connection);
  assert.deepEqual(disconnects, []);
});
