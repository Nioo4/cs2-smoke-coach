import assert from 'node:assert/strict';
import { randomUUID } from 'node:crypto';
import { isDeepStrictEqual } from 'node:util';
import * as Y from 'yjs';
import WebSocket from 'ws';

const timeoutMs = 5_000;
const targetInput = process.argv[2] ?? 'http://127.0.0.1:4173/';
const sockets = new Set();

class AuditError extends Error {
  constructor(code) { super(code); this.code = code; }
}

function ensure(condition, code) {
  if (!condition) throw new AuditError(code);
}

function targetUrl(input) {
  ensure(process.argv.length <= 3, 'usage: node scripts/live-boundary-audit.mjs [http://127.0.0.1:4173/]');
  let url;
  try { url = new URL(input); } catch { throw new AuditError('invalid_target_url'); }
  ensure(url.protocol === 'http:' && url.hostname === '127.0.0.1' && !url.username && !url.password && url.pathname === '/' && !url.search && !url.hash, 'target_must_be_loopback_prototype');
  return url;
}

async function fetchLocal(path, options = {}) {
  try {
    return await fetch(new URL(path, target), { ...options, signal: AbortSignal.timeout(timeoutMs) });
  } catch {
    throw new AuditError('local_http_request_failed');
  }
}

async function expectRejectedTicketRequest(origin, code) {
  const headers = origin ? { origin } : {};
  const response = await fetchLocal('/__t0/test-tickets', { method: 'POST', headers });
  await response.body?.cancel();
  assert.equal(response.status, 403, code);
}

function createPeer(socket) {
  const peer = { socket, messages: [], waiters: [], closeWaiters: [], closed: null, failed: false };
  socket.on('message', (raw) => {
    let message;
    try { message = JSON.parse(raw.toString('utf8')); } catch { peer.fail(); return; }
    const index = peer.messages.push(message) - 1;
    for (let i = peer.waiters.length - 1; i >= 0; i -= 1) {
      const waiter = peer.waiters[i];
      if (index < waiter.fromIndex || !waiter.predicate(message)) continue;
      peer.waiters.splice(i, 1);
      clearTimeout(waiter.timer);
      waiter.resolve(message);
    }
  });
  socket.on('close', (code, reason) => {
    peer.closed = { code, reason: reason.toString() };
    for (const waiter of peer.closeWaiters.splice(0)) {
      clearTimeout(waiter.timer);
      waiter.resolve(peer.closed);
    }
    peer.failWaiters();
  });
  socket.on('error', () => { peer.fail(); });
  peer.failWaiters = () => {
    for (const waiter of peer.waiters.splice(0)) {
      clearTimeout(waiter.timer);
      waiter.reject(new AuditError('websocket_closed_before_expected_message'));
    }
  };
  peer.fail = () => {
    peer.failed = true;
    peer.failWaiters();
    for (const waiter of peer.closeWaiters.splice(0)) {
      clearTimeout(waiter.timer);
      waiter.reject(new AuditError('websocket_transport_failed'));
    }
  };
  peer.waitForMessage = (predicate, fromIndex = 0) => {
    const existing = peer.messages.find((message, index) => index >= fromIndex && predicate(message));
    if (existing) return Promise.resolve(existing);
    if (peer.closed || peer.failed) return Promise.reject(new AuditError('websocket_closed_before_expected_message'));
    return new Promise((resolve, reject) => {
      const waiter = { predicate, fromIndex, resolve, reject, timer: null };
      waiter.timer = setTimeout(() => {
        peer.waiters.splice(peer.waiters.indexOf(waiter), 1);
        reject(new AuditError('websocket_message_timeout'));
      }, timeoutMs);
      peer.waiters.push(waiter);
    });
  };
  peer.waitForClose = () => {
    if (peer.closed) return Promise.resolve(peer.closed);
    if (peer.failed) return Promise.reject(new AuditError('websocket_transport_failed'));
    return new Promise((resolve, reject) => {
      const waiter = { resolve, reject, timer: null };
      waiter.timer = setTimeout(() => {
        peer.closeWaiters.splice(peer.closeWaiters.indexOf(waiter), 1);
        reject(new AuditError('websocket_close_timeout'));
      }, timeoutMs);
      peer.closeWaiters.push(waiter);
    });
  };
  peer.send = (message) => {
    ensure(socket.readyState === WebSocket.OPEN, 'websocket_not_open');
    socket.send(JSON.stringify(message));
  };
  return peer;
}

async function openPeer(wsUrl, origin) {
  const socket = new WebSocket(wsUrl, { origin, handshakeTimeout: timeoutMs });
  sockets.add(socket);
  const peer = createPeer(socket);
  await new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new AuditError('websocket_open_timeout')), timeoutMs);
    socket.once('open', () => { clearTimeout(timer); resolve(); });
    socket.once('error', () => { clearTimeout(timer); reject(new AuditError('websocket_connect_failed')); });
  });
  return peer;
}

async function checkRejectedUpgrade(wsUrl, origin) {
  const socket = new WebSocket(wsUrl, { origin, handshakeTimeout: timeoutMs });
  sockets.add(socket);
  return new Promise((resolve, reject) => {
    let settled = false;
    const timer = setTimeout(() => finish(reject, new AuditError('foreign_origin_ws_timeout')), timeoutMs);
    const finish = (complete, value) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      complete(value);
    };
    socket.once('unexpected-response', (_request, response) => {
      const status = response.statusCode;
      response.resume();
      finish(resolve, status);
    });
    socket.once('open', () => {
      socket.terminate();
      finish(reject, new AuditError('foreign_origin_ws_accepted'));
    });
    socket.on('error', () => finish(reject, new AuditError('foreign_origin_ws_failed')));
  });
}

async function closeSocket(socket) {
  if (socket.readyState === WebSocket.CLOSED) return;
  await new Promise((resolve) => {
    const timer = setTimeout(() => { socket.terminate(); resolve(); }, 750);
    socket.once('close', () => { clearTimeout(timer); resolve(); });
    if (socket.readyState === WebSocket.OPEN) socket.close(1000, 'audit_complete');
    else socket.terminate();
  });
}

async function run() {
  target = targetUrl(targetInput);
  const healthResponse = await fetchLocal('/__t0/health');
  ensure(healthResponse.status === 200, 'target_health_failed');
  let health;
  try { health = await healthResponse.json(); } catch { throw new AuditError('target_health_invalid'); }
  ensure(health.status === 'local-only' && health.prototype === true, 'target_is_not_t0_prototype');
  process.stdout.write('T0_LIVE_AUDIT_LOCAL_HEALTH_PASS\n');

  await expectRejectedTicketRequest(undefined, 'ticket_without_origin_accepted');
  await expectRejectedTicketRequest('http://foreign-origin.invalid', 'ticket_with_foreign_origin_accepted');
  process.stdout.write('T0_LIVE_AUDIT_TICKET_ORIGIN_PASS\n');

  const wsUrl = new URL('/collab', target);
  wsUrl.protocol = 'ws:';
  const foreignWsStatus = await checkRejectedUpgrade(wsUrl, 'http://foreign-origin.invalid');
  assert.equal(foreignWsStatus, 403, 'websocket_foreign_origin_accepted');
  process.stdout.write('T0_LIVE_AUDIT_WS_ORIGIN_PASS\n');

  const ticketResponse = await fetchLocal('/__t0/test-tickets', { method: 'POST', headers: { origin: target.origin } });
  ensure(ticketResponse.status === 201, 'local_ticket_issue_failed');
  let ticketSet;
  try { ticketSet = await ticketResponse.json(); } catch { throw new AuditError('local_ticket_response_invalid'); }
  const actorA = ticketSet.participants?.find((participant) => participant.actor_id === 'test-a')?.token;
  const actorB = ticketSet.participants?.find((participant) => participant.actor_id === 'test-b')?.token;
  ensure(typeof actorA === 'string' && typeof actorB === 'string', 'local_test_tickets_missing');

  const peerA = await openPeer(wsUrl, target.origin);
  const firstSyncPromise = peerA.waitForMessage((message) => message.type === 'sync');
  const firstAuthPromise = peerA.waitForMessage((message) => message.type === 'auth.ok');
  peerA.send({ type: 'auth', ticket: actorA });
  await Promise.all([firstSyncPromise, firstAuthPromise]);

  const reusedPeer = await openPeer(wsUrl, target.origin);
  const reusedClosePromise = reusedPeer.waitForClose();
  reusedPeer.send({ type: 'auth', ticket: actorA });
  const reusedClose = await reusedClosePromise;
  assert.equal(reusedClose.code, 4401, 'single_use_ticket_reuse_not_rejected');
  assert.equal(reusedClose.reason, 'invalid_ticket', 'single_use_ticket_reuse_reason_mismatch');
  process.stdout.write('T0_LIVE_AUDIT_SINGLE_USE_TICKET_PASS\n');

  const peerB = await openPeer(wsUrl, target.origin);
  const syncPromise = peerB.waitForMessage((message) => message.type === 'sync');
  const authPromise = peerB.waitForMessage((message) => message.type === 'auth.ok');
  peerB.send({ type: 'auth', ticket: actorB });
  const [before] = await Promise.all([syncPromise, authPromise]);

  const candidate = new Y.Doc();
  try {
    Y.applyUpdate(candidate, Buffer.from(before.state, 'base64'), 'audit-sync');
    const rootName = `audit-extra-${randomUUID()}`;
    let maliciousUpdate;
    candidate.once('update', (update, origin) => { if (origin === 'audit-extra-root') maliciousUpdate = update; });
    candidate.transact(() => candidate.getMap(rootName).set('probe', 'extra-root'), 'audit-extra-root');
    ensure(maliciousUpdate instanceof Uint8Array, 'malicious_update_not_created');

    const operationId = randomUUID();
    const start = peerB.messages.length;
    peerB.send({
      type: 'operation',
      operation: {
        type: 'text.update', operation_id: operationId, epoch: before.epoch,
        payload: { update: Buffer.from(maliciousUpdate).toString('base64') },
      },
    });
    const outcome = await peerB.waitForMessage((message) => ['operation.rejected', 'operation.applied'].includes(message.type) && message.operation_id === operationId, start);
    ensure(outcome.type === 'operation.rejected', 'malicious_update_was_applied');
    const rejected = outcome;
    ensure(typeof rejected.code === 'string' && rejected.code.length > 0, 'malicious_update_rejection_missing_code');
    const after = await peerB.waitForMessage((message) => message.type === 'sync', start);
    const applied = peerB.messages.slice(start).filter((message) => message.type === 'operation.applied');
    ensure(!applied.some((message) => message.operation_id === operationId), 'malicious_update_was_applied');
    if (applied.length || after.revision !== before.revision) throw new AuditError('document_changed_during_audit');
    ensure(after.epoch === before.epoch, 'epoch_changed_after_rejection');
    ensure(isDeepStrictEqual(after.fields, before.fields), 'authoritative_text_changed_after_rejection');
    ensure(isDeepStrictEqual(after.structure, before.structure), 'authoritative_structure_changed_after_rejection');
    ensure(after.state === before.state, 'authoritative_document_changed_after_rejection');
    process.stdout.write('T0_LIVE_AUDIT_ILLEGAL_ROOT_REJECTED_NO_CHANGE_PASS\n');
  } finally {
    candidate.destroy();
  }

  process.stdout.write('T0_LIVE_BOUNDARY_AUDIT_PASS\n');
}

let target;
try {
  await run();
} catch (error) {
  process.stderr.write(`T0_LIVE_BOUNDARY_AUDIT_FAIL ${error instanceof AuditError ? error.code : 'unexpected_error'}\n`);
  process.exitCode = 1;
} finally {
  await Promise.all([...sockets].map(closeSocket));
}
