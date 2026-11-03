// Explicit isolated integration client; never imported by the running collaboration server.
import assert from 'node:assert/strict';
import { randomUUID } from 'node:crypto';
import * as Y from 'yjs';
import WebSocket from 'ws';
import pg from 'pg';
import { FormalHttpStore } from '../src/formal-http-store.mjs';
import { canonicalJson, encodeFormalDocument, sha256 } from '../src/formal-document.mjs';

const backend = process.env.FORMAL_TEST_BACKEND;
const websocket = process.env.FORMAL_TEST_WS;
const origin = process.env.FORMAL_TEST_ORIGIN;
const database = new URL(process.env.FORMAL_TEST_DATABASE_URL);
if (database.hostname !== '127.0.0.1' || database.pathname !== '/tactics_formal_selftest' || database.port === '55433') throw new Error('isolated_formal_database_required');
const sql = new pg.Client({ connectionString: database.toString() });
await sql.connect();
let cookie = ''; let csrf = ''; const sockets = [];
async function api(path, method = 'GET', body = null, extra = {}) {
  const result = await fetch(`${backend}/api/v1${path}`, { method, headers: { origin, cookie, 'x-csrf-token': csrf, 'content-type': 'application/json', ...extra }, ...(body === null ? {} : { body: JSON.stringify(body) }) });
  if (result.headers.get('set-cookie')) cookie = result.headers.get('set-cookie').split(';')[0];
  const data = await result.json(); assert.equal(result.ok, true, `${path}: ${result.status}`);
  if (data.csrf_token) csrf = data.csrf_token; return data;
}
async function connect(tacticId) {
  const { ticket } = await api(`/tactics/${tacticId}/collaboration-ticket`, 'POST', {});
  const ws = new WebSocket(websocket, { origin }); const inbox = []; const waiters = [];
  ws.on('message', (raw) => { const message = JSON.parse(raw.toString()); const index = waiters.findIndex((item) => item.predicate(message)); if (index >= 0) { const [item] = waiters.splice(index, 1); clearTimeout(item.timer); item.resolve(message); } else inbox.push(message); });
  const channel = { ws, inbox, send: (message) => ws.send(JSON.stringify(message)), wait: (predicate, timeout = 6000) => {
    const index = inbox.findIndex(predicate); if (index >= 0) return Promise.resolve(inbox.splice(index, 1)[0]);
    return new Promise((resolve, reject) => { const item = { predicate, resolve, timer: setTimeout(() => { const index = waiters.indexOf(item); if (index >= 0) waiters.splice(index, 1); reject(new Error('formal_message_timeout')); }, timeout) }; waiters.push(item); });
  } };
  sockets.push(ws);
  await new Promise((resolve, reject) => { ws.once('open', resolve); ws.once('error', reject); });
  channel.send({ type: 'auth', ticket });
  channel.auth = await channel.wait((message) => message.type === 'auth.ok');
  channel.sync = await channel.wait((message) => message.type === 'sync');
  return channel;
}
const operation = (sync, type, payload) => ({ type, operation_id: randomUUID(), epoch: sync.epoch, payload });
const sendOperation = (channel, op) => channel.send({ type: 'operation', operation: op });
const applied = (channel, op) => channel.wait((message) => message.type === 'operation.applied' && message.operation_id === op.operation_id);
function textOperation(sync, key, insertion) {
  const doc = new Y.Doc(); Y.applyUpdate(doc, Buffer.from(sync.state, 'base64')); const before = Y.encodeStateVector(doc);
  const text = doc.getMap('fields').get(key); text.insert(text.length, insertion);
  const op = operation(sync, 'text.update', { update: Buffer.from(Y.encodeStateAsUpdate(doc, before)).toString('base64') }); doc.destroy(); return op;
}
async function command(channel, sync, value) { const op = operation(sync, 'stage.command', { expected_revision: sync.revision, command: value }); sendOperation(channel, op); return applied(channel, op); }
async function lease(channel, objectId, version) {
  const requestId = randomUUID(); channel.send({ type: 'lease.acquire', request_id: requestId, payload: { object_id: objectId, version } });
  return channel.wait((message) => message.request_id === requestId && ['lease.granted', 'protocol.error'].includes(message.type));
}
let tacticId;
try {
  await api('/auth/register', 'POST', { username: `ws_${randomUUID().replaceAll('-', '').slice(0, 20)}`, display_name: '真实协作测试', password: 'formal-protocol-test-password-2026' });
  const { notebook } = await api('/notebooks/personal');
  const created = await api(`/notebooks/${notebook.id}/tactics`, 'POST', { title: '正式WS验证', side: 'T' }, { 'idempotency-key': randomUUID() });
  tacticId = created.tactic.id;
  const a = await connect(tacticId); const b = await connect(tacticId); let state = a.sync;
  assert.notEqual(a.auth.connection_id, b.auth.connection_id); assert.equal(a.auth.actor_id, b.auth.actor_id);
  const op = textOperation(state, 'tactic.title', '已确认'); sendOperation(a, op); state = await applied(a, op);
  assert.equal((await applied(b, op)).revision, state.revision);
  assert.equal((await api(`/tactics/${tacticId}`)).tactic.title, '正式WS验证已确认');
  sendOperation(a, op);
  assert.equal((await a.wait((message) => message.type === 'operation.ack' && message.operation_id === op.operation_id)).duplicate, true);
  const firstStage = state.projection.structure.stages[0].id;
  state = await command(a, state, { type: 'stage.add', after_stage_id: firstStage });
  const stale = operation(state, 'stage.command', { expected_revision: 0, command: { type: 'stage.add', after_stage_id: firstStage } });
  sendOperation(b, stale); assert.equal((await b.wait((message) => message.type === 'operation.rejected' && message.operation_id === stale.operation_id)).code, 'TACTIC_CONFLICT');
  const rogue = new Y.Doc(); Y.applyUpdate(rogue, Buffer.from(state.state, 'base64')); const before = Y.encodeStateVector(rogue);
  rogue.getMap('document').set('structure', { ...rogue.getMap('document').get('structure'), side: 'CT' });
  const forged = operation(state, 'text.update', { update: Buffer.from(Y.encodeStateAsUpdate(rogue, before)).toString('base64') }); rogue.destroy();
  sendOperation(b, forged); assert.equal((await b.wait((message) => message.type === 'operation.rejected' && message.operation_id === forged.operation_id)).code, 'COLLAB_STALE_FIELD');
  // Only this isolated test document is affected by the fault injection.
  await sql.query(`CREATE OR REPLACE FUNCTION formal_test_reject_write() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN IF NEW.tactic_id = '${tacticId}'::uuid THEN RAISE EXCEPTION 'isolated test failure'; END IF; RETURN NEW; END $$`);
  await sql.query('CREATE TRIGGER formal_test_write_failure BEFORE UPDATE ON tactic_documents FOR EACH ROW EXECUTE FUNCTION formal_test_reject_write()');
  const failed = textOperation(state, 'tactic.purpose', '失败后精确重试'); sendOperation(a, failed);
  await a.wait((message) => message.type === 'operation.rejected' && message.operation_id === failed.operation_id);
  assert.equal((await api(`/tactics/${tacticId}`)).tactic.revision, state.revision);
  assert.equal(b.inbox.some((message) => message.type === 'operation.applied' && message.operation_id === failed.operation_id), false);
  await sql.query('DROP TRIGGER formal_test_write_failure ON tactic_documents'); await sql.query('DROP FUNCTION formal_test_reject_write()');
  sendOperation(a, failed); state = await applied(a, failed);
  const roleId = state.projection.structure.roles[0].id;
  state = await command(a, state, { type: 'action.add', stage_id: firstStage, kind: 'wait', role_id: roleId, group_mode: 'after', description: '删除恢复保留说明' });
  const oldAction = state.projection.structure.stages[0].actions[0];
  const late = textOperation(state, `action.${oldAction.id}.description`, '迟到不可复活');
  const grant = await lease(a, `action:${oldAction.id}`, oldAction.version);
  state = await command(a, state, { type: 'action.delete', stage_id: firstStage, action_id: oldAction.id, lease: { token: grant.token, version: oldAction.version } });
  sendOperation(b, late); assert.equal((await b.wait((message) => message.type === 'operation.rejected' && message.operation_id === late.operation_id)).code, 'COLLAB_STALE_FIELD');
  const recovery = state.recoveries.find((item) => item.entity_id === oldAction.id);
  assert.ok(recovery);
  state = await command(a, state, { type: 'recovery.restore', recovery_id: recovery.id, stage_id: firstStage });
  const restored = state.projection.structure.stages[0].actions[0];
  assert.notEqual(restored.id, oldAction.id);
  assert.equal(state.projection.fields[`action.${restored.id}.description`], '删除恢复保留说明');
  const positionId = `position:${firstStage}:${roleId}`;
  const position = state.projection.structure.stages[0].initial_positions[roleId];
  const held = await lease(a, positionId, position.version);
  assert.equal((await lease(b, positionId, position.version)).code, 'COLLAB_LOCKED');
  state = await command(a, state, { type: 'position.set', stage_id: firstStage, role_id: roleId, position: { x: 75, y: 50 }, lease: { token: held.token, version: position.version } });
  assert.equal(state.projection.structure.stages[0].initial_positions[roleId].x, 75);
  a.inbox.splice(0, a.inbox.length, ...a.inbox.filter((message) => message.type !== 'locks'));
  const expiredLocks = await a.wait((message) => message.type === 'locks' && !message.locks.some((item) => item.object_id === positionId), 12_000);
  assert.ok(!expiredLocks.locks.some((item) => item.object_id === positionId));
  const renewRequest = randomUUID();
  a.send({ type: 'lease.renew', request_id: renewRequest, payload: { object_id: positionId, token: held.token } });
  assert.equal((await a.wait((message) => message.request_id === renewRequest)).code, 'LEASE_EXPIRED');
  // A second trusted HTTP writer forces the active WS room to encounter a real PG CAS conflict.
  const writer = new FormalHttpStore({ baseUrl: backend, internalKey: process.env.FORMAL_TEST_INTERNAL_KEY });
  const writerTicket = (await api(`/tactics/${tacticId}/collaboration-ticket`, 'POST', {})).ticket;
  const writerGrant = await writer.consume(writerTicket, randomUUID());
  const external = textOperation(state, 'tactic.purpose', '外部权威提交');
  const externalDoc = new Y.Doc(); Y.applyUpdate(externalDoc, Buffer.from(state.state, 'base64')); Y.applyUpdate(externalDoc, Buffer.from(external.payload.update, 'base64'));
  const payloadJson = canonicalJson(external.payload);
  const externalResult = await writer.persist(tacticId, writerGrant, { epoch: state.epoch, expected_revision: state.revision, operation_id: external.operation_id, type: external.type,
    original_payload: external.payload, payload_json: payloadJson, fingerprint: sha256(`${external.type}\n${state.epoch}\n${payloadJson}`), ...encodeFormalDocument(externalDoc) });
  externalDoc.destroy(); await writer.release(writerGrant);
  const cas = operation(state, 'stage.command', { expected_revision: state.revision, command: { type: 'stage.add', after_stage_id: firstStage } });
  sendOperation(a, cas);
  assert.equal((await a.wait((message) => message.type === 'operation.rejected' && message.operation_id === cas.operation_id)).code, 'TACTIC_CONFLICT');
  state = await a.wait((message) => message.type === 'sync' && message.revision === externalResult.revision);
  assert.ok(state.projection.fields['tactic.purpose'].includes('外部权威提交'));
  const secondStage = state.projection.structure.stages[1].id;
  b.send({ type: 'presence', payload: { stage_id: secondStage, field: `stage.${secondStage}.condition` } });
  const presence = await a.wait((message) => message.type === 'participants' && message.participants.some((item) => item.connection_id === b.auth.connection_id && item.stage_id === secondStage));
  assert.equal(presence.participants.find((item) => item.connection_id === b.auth.connection_id).field, `stage.${secondStage}.condition`);
  a.inbox.splice(0, a.inbox.length, ...a.inbox.filter((message) => message.type !== 'participants'));
  for (let index = 0; index < 20; index += 1) b.send({ type: 'presence', payload: { stage_id: secondStage, pointer: { x: index, y: 50 } } });
  await new Promise((resolve) => setTimeout(resolve, 150));
  assert.ok(a.inbox.filter((message) => message.type === 'participants').length <= 2, 'presence is throttled independently of persistent operations');
  assert.equal(b.ws.readyState, WebSocket.OPEN, 'a batch of throttled presence must not consume async operation queue slots');
  for (let index = 0; index < 4; index += 1) { const extra = await connect(tacticId); if (index === 3) assert.equal(extra.auth.read_only, true, `editor seats: ${JSON.stringify(extra.sync.participants.map((item) => ({ connection_id: item.connection_id, read_only: item.read_only })))}`); }
  const repeatId = failed.operation_id;
  a.ws.terminate();
  const reconnected = await connect(tacticId); sendOperation(reconnected, failed);
  const ack = await reconnected.wait((message) => message.type === 'operation.ack' && message.operation_id === repeatId);
  assert.equal(ack.revision, 3); assert.equal(ack.current_revision, state.revision);
  const rows = await sql.query('SELECT count(*)::int AS n FROM tactic_operations WHERE tactic_id=$1 AND operation_id=$2', [tacticId, repeatId]);
  assert.equal(rows.rows[0].n, 1);
  const close = new Promise((resolve) => b.ws.once('close', resolve));
  const revokedAt = Date.now(); await api('/auth/logout', 'POST', {});
  const invalid = textOperation(state, 'tactic.purpose', '撤销后禁止'); sendOperation(b, invalid);
  assert.equal((await b.wait((message) => message.type === 'operation.rejected' && message.operation_id === invalid.operation_id)).code, 'USER_AUTH_REQUIRED');
  await close; assert.ok(Date.now() - revokedAt < 5000);
  process.stdout.write(`FORMAL_PROTOCOL_REAL_HTTP_WS_PG_PASS ${tacticId}\n`);
} finally {
  for (const socket of sockets) socket.terminate();
  await sql.query('DROP TRIGGER IF EXISTS formal_test_write_failure ON tactic_documents').catch(() => {});
  await sql.query('DROP FUNCTION IF EXISTS formal_test_reject_write()').catch(() => {});
  await sql.end();
}
