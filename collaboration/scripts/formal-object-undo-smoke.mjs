// Explicit integration runner; only a newly arranged undo selftest database is allowed.
import assert from 'node:assert/strict';
import { randomUUID } from 'node:crypto';
import WebSocket from 'ws';
import pg from 'pg';

const database = new URL(process.env.UNDO_TEST_DATABASE_URL);
if (database.hostname !== '127.0.0.1' || database.port !== '65402' || database.username !== 'tactics_test' || database.pathname !== '/tactics_undo_selftest') throw new Error('isolated_undo_database_required');
const backend = new URL(process.env.FORMAL_TEST_BACKEND); const websocket = new URL(process.env.FORMAL_TEST_WS);
if (backend.hostname !== '127.0.0.1' || websocket.hostname !== '127.0.0.1') throw new Error('loopback_test_servers_required');
const origin = process.env.FORMAL_TEST_ORIGIN; const sql = new pg.Client({ connectionString: database.toString() });
const sockets = []; let cookie = ''; let csrf = ''; let trigger = null;
async function api(path, method = 'GET', body = null, extra = {}) {
  const response = await fetch(`${backend.origin}/api/v1${path}`, { method, headers: { origin, cookie, 'x-csrf-token': csrf, 'content-type': 'application/json', ...extra }, ...(body === null ? {} : { body: JSON.stringify(body) }) });
  if (response.headers.get('set-cookie')) cookie = response.headers.get('set-cookie').split(';')[0];
  assert.equal(response.ok, true, `HTTP_${response.status}`); const result = await response.json(); if (result.csrf_token) csrf = result.csrf_token; return result;
}
async function connect(id) {
  const { ticket } = await api(`/tactics/${id}/collaboration-ticket`, 'POST', {});
  const ws = new WebSocket(websocket, { origin }); const inbox = []; const waiters = [];
  const channel = { ws, send: (value) => ws.send(JSON.stringify(value)), wait(predicate) {
    const index = inbox.findIndex(predicate); if (index >= 0) return Promise.resolve(inbox.splice(index, 1)[0]);
    return new Promise((resolve, reject) => { const waiter = { predicate, resolve, timer: setTimeout(() => { waiters.splice(waiters.indexOf(waiter), 1); reject(new Error('undo_message_timeout')); }, 7000) }; waiters.push(waiter); });
  } };
  ws.on('message', raw => { const value = JSON.parse(raw); const index = waiters.findIndex(item => item.predicate(value)); if (index < 0) inbox.push(value); else { const [waiter] = waiters.splice(index, 1); clearTimeout(waiter.timer); waiter.resolve(value); } });
  sockets.push(ws); await new Promise((resolve, reject) => { ws.once('open', resolve); ws.once('error', reject); });
  channel.send({ type: 'auth', ticket }); await channel.wait(value => value.type === 'auth.ok');
  channel.state = await channel.wait(value => value.type === 'sync');
  assert.equal((await channel.wait(value => value.type === 'object.undo.state')).undo, null); return channel;
}
async function command(channel, state, value) {
  const operation = { type: 'stage.command', operation_id: randomUUID(), epoch: state.epoch, payload: { expected_revision: state.revision, command: value } };
  channel.send({ type: 'operation', operation }); const result = await channel.wait(value => value.operation_id === operation.operation_id && ['operation.applied', 'operation.rejected'].includes(value.type));
  assert.equal(result.type, 'operation.applied', result.code); return result;
}
const receipt = (channel, sourceId) => channel.wait(value => value.type === 'object.undo.state' && value.undo?.source_operation_id === sourceId).then(value => value.undo);
function undoOperation(state, item) { return { type: 'stage.command', operation_id: randomUUID(), epoch: state.epoch, payload: { expected_revision: state.revision, command: { type: 'object.undo', source_operation_id: item.source_operation_id, recovery_id: item.recovery_id } } }; }
try {
  await sql.connect();
  await api('/auth/register', 'POST', { username: `undo_${randomUUID().replaceAll('-', '').slice(0, 18)}`, display_name: '对象撤销隔离测试', password: 'undo-selftest-public-password-2026' });
  const { notebook } = await api('/notebooks/personal'); const created = await api(`/notebooks/${notebook.id}/tactics`, 'POST', { title: '撤销真实PG', side: 'T' }, { 'idempotency-key': randomUUID() });
  const id = created.tactic.id; const a = await connect(id); const b = await connect(id); let state = a.state;
  const stageId = state.projection.structure.stages[0].id; const roleId = state.projection.structure.roles[0].id;
  state = await command(a, state, { type: 'action.add', stage_id: stageId, role_id: roleId, kind: 'wait', group_mode: 'after', description: '保留原说明' });
  const action = state.projection.structure.stages[0].actions[0];
  const request = randomUUID(); a.send({ type: 'lease.acquire', request_id: request, payload: { object_id: `action:${action.id}`, version: action.version } });
  const lease = await a.wait(value => value.type === 'lease.granted' && value.request_id === request);
  state = await command(a, state, { type: 'action.delete', stage_id: stageId, action_id: action.id, lease: { token: lease.token, version: action.version } });
  const deletion = await receipt(a, state.operation_id); assert.equal(deletion.is_delete, true); assert.ok(deletion.recovery_id);
  const undo = undoOperation(state, deletion); a.send({ type: 'operation', operation: undo });
  state = await a.wait(value => value.operation_id === undo.operation_id && ['operation.applied', 'operation.rejected'].includes(value.type));
  assert.equal(state.type, 'operation.applied', state.code); const fresh = state.projection.structure.stages[0].actions[0];
  assert.notEqual(fresh.id, action.id); assert.equal(state.projection.fields[`action.${fresh.id}.description`], '保留原说明');
  const revision = state.revision; a.send({ type: 'operation', operation: undo });
  const ack = await a.wait(value => value.type === 'operation.ack' && value.operation_id === undo.operation_id); assert.equal(ack.current_revision, revision);
  const saved = await api(`/tactics/${id}`); assert.equal(saved.tactic.revision, revision); assert.equal(saved.projection.structure.stages[0].actions[0].id, fresh.id);
  state = await command(a, ack, { type: 'role.add' }); const last = await receipt(a, state.operation_id); const pending = undoOperation(state, last);
  trigger = `undo_fault_${randomUUID().replaceAll('-', '')}`;
  await sql.query(`CREATE FUNCTION ${trigger}() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'isolated undo fault'; END; $$`);
  await sql.query(`CREATE TRIGGER ${trigger} BEFORE UPDATE ON tactic_documents FOR EACH ROW WHEN (OLD.tactic_id='${id}'::uuid) EXECUTE FUNCTION ${trigger}()`);
  a.send({ type: 'operation', operation: pending }); const rejected = await a.wait(value => value.type === 'operation.rejected' && value.operation_id === pending.operation_id); assert.notEqual(rejected.code, undefined);
  const unchanged = await api(`/tactics/${id}`); assert.equal(unchanged.tactic.revision, state.revision);
  assert.deepEqual((await receipt(a, last.source_operation_id)), last);
  await sql.query(`DROP TRIGGER ${trigger} ON tactic_documents`); await sql.query(`DROP FUNCTION ${trigger}()`); trigger = null;
  a.send({ type: 'operation', operation: pending }); state = await a.wait(value => value.type === 'operation.applied' && value.operation_id === pending.operation_id);
  assert.equal(state.projection.structure.roles.length, 1);
  // A separate connection cannot undo a's latest confirmed operation.
  state = await command(a, state, { type: 'tactic.side.set', side: 'CT' }); const privateReceipt = await receipt(a, state.operation_id);
  const stolen = undoOperation(state, privateReceipt); b.send({ type: 'operation', operation: stolen });
  assert.equal((await b.wait(value => value.type === 'operation.rejected' && value.operation_id === stolen.operation_id)).code, 'TACTIC_CONFLICT');
  process.stdout.write('OBJECT_UNDO_REAL_HTTP_WS_PG_PASS\n');
} finally {
  if (trigger) { await sql.query(`DROP TRIGGER IF EXISTS ${trigger} ON tactic_documents`); await sql.query(`DROP FUNCTION IF EXISTS ${trigger}()`); }
  for (const ws of sockets) ws.close(); await sql.end();
}
