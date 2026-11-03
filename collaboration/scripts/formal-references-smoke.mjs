// Isolated reference-catalog integration; this client is not part of the Node runtime server.
import assert from 'node:assert/strict';
import { randomUUID } from 'node:crypto';
import WebSocket from 'ws';
import * as Y from 'yjs';
import pg from 'pg';

const dbUrl = new URL(process.env.REFERENCES_TEST_DATABASE_URL);
if (dbUrl.hostname !== '127.0.0.1' || dbUrl.pathname !== '/tactics_references_selftest' || dbUrl.port === '55433') throw new Error('reference_test_database_required');
const catalog = JSON.parse(process.env.REFERENCES_TEST_CATALOG);
const sql = new pg.Client({ connectionString: dbUrl.toString() }); await sql.connect();
const backend = process.env.FORMAL_TEST_BACKEND; const origin = process.env.FORMAL_TEST_ORIGIN;
let cookie = ''; let csrf = ''; const sockets = [];
async function api(path, method = 'GET', body = null, extra = {}, status = 200) {
  const result = await fetch(`${backend}/api/v1${path}`, { method, headers: { origin, cookie, 'x-csrf-token': csrf, 'content-type': 'application/json', ...extra }, ...(body === null ? {} : { body: JSON.stringify(body) }) });
  if (result.headers.get('set-cookie')) cookie = result.headers.get('set-cookie').split(';')[0];
  const data = await result.json(); assert.equal(result.status, status, `${path}: ${result.status}`);
  assert.equal(result.headers.get('cache-control'), 'no-store');
  if (data.csrf_token) csrf = data.csrf_token; return data;
}
async function connect(tacticId) {
  const { ticket } = await api(`/tactics/${tacticId}/collaboration-ticket`, 'POST', {}, {}, 201);
  const ws = new WebSocket(process.env.FORMAL_TEST_WS, { origin }); const inbox = []; const waiting = [];
  const channel = { ws, send: (message) => ws.send(JSON.stringify(message)), wait: (predicate) => {
    const index = inbox.findIndex(predicate); if (index >= 0) return Promise.resolve(inbox.splice(index, 1)[0]);
    return new Promise((resolve, reject) => { const item = { predicate, resolve, timer: setTimeout(() => reject(new Error('reference_message_timeout')), 6000) }; waiting.push(item); });
  } };
  ws.on('message', (raw) => { const message = JSON.parse(raw.toString()); const index = waiting.findIndex((item) => item.predicate(message)); if (index < 0) inbox.push(message); else { const [item] = waiting.splice(index, 1); clearTimeout(item.timer); item.resolve(message); } });
  sockets.push(ws); await new Promise((resolve, reject) => { ws.once('open', resolve); ws.once('error', reject); });
  channel.send({ type: 'auth', ticket }); await channel.wait((message) => message.type === 'auth.ok');
  channel.sync = await channel.wait((message) => message.type === 'sync'); return channel;
}
async function command(channel, state, value, expectedCode = null, revision = state.revision) {
  const operationId = randomUUID();
  channel.send({ type: 'operation', operation: { type: 'stage.command', operation_id: operationId, epoch: state.epoch, payload: { expected_revision: revision, command: value } } });
  const result = await channel.wait((message) => message.operation_id === operationId && ['operation.applied', 'operation.rejected'].includes(message.type));
  if (expectedCode) { assert.equal(result.code, expectedCode); assert.ok(!JSON.stringify(result).includes('隐藏教学秘密')); return state; }
  assert.equal(result.type, 'operation.applied'); return result;
}
try {
  await api('/auth/register', 'POST', { username: `refs_${randomUUID().replaceAll('-', '').slice(0, 18)}`, display_name: '引用测试', password: 'references-test-password-2026' }, {}, 201);
  const { notebook } = await api('/notebooks/personal');
  const created = await api(`/notebooks/${notebook.id}/tactics`, 'POST', { title: '教学引用演示', side: 'T' }, { 'idempotency-key': randomUUID() }, 201);
  const identity = created.tactic.id; const path = `/tactics/${identity}`;
  const a = await connect(identity); const b = await connect(identity); let state = a.sync;
  const stageId = state.projection.structure.stages[0].id; const roleId = state.projection.structure.roles[0].id;
  const requestId = randomUUID(); b.send({ type: 'lease.acquire', request_id: requestId, payload: { object_id: `position:${stageId}:${roleId}`, version: 1 } });
  await b.wait((message) => message.type === 'lease.granted' && message.request_id === requestId);
  await sql.query('BEGIN');
  await sql.query('SELECT pg_advisory_xact_lock($1)', [1397575499]);
  let settled = false;
  const waitingForCatalog = command(a, state, { type: 'stage.lineups.set', stage_id: stageId, lineup_ids: [catalog.visible, catalog.variant] }, 'TACTIC_LINEUP_UNAVAILABLE').finally(() => { settled = true; });
  await new Promise((resolve) => setTimeout(resolve, 100));
  assert.equal(settled, false, 'reference commit must wait for the existing exclusive catalog writer');
  await sql.query("UPDATE tutorial_videos SET status='archived' WHERE lineup_id=$1", [catalog.visible]);
  await sql.query('COMMIT');
  state = await waitingForCatalog;
  await sql.query("UPDATE tutorial_videos SET status='ready' WHERE lineup_id=$1", [catalog.visible]);
  state = await command(a, state, { type: 'stage.lineups.set', stage_id: stageId, lineup_ids: [catalog.visible, catalog.variant] });
  assert.equal(state.projection.structure.stages[0].version, 2);
  assert.equal(state.projection.structure.stages[0].initial_positions[roleId].version, 1);
  assert.deepEqual(state.projection.structure.stages[0].lineup_ids, [catalog.visible, catalog.variant]);
  const detail = await api(`${path}/lineups/${catalog.visible}`);
  assert.equal(detail.available, true); assert.equal(detail.lineup.source, 'local_curated');
  assert.equal(detail.lineup.id, catalog.visible);
  assert.deepEqual(detail.lineup.other_lineups.map((item) => item.id), [catalog.variant]);
  assert.equal('rights_note' in detail.lineup.primary_video, false);
  await sql.query('ALTER TABLE tutorial_videos RENAME TO reference_test_video_off');
  try {
    const fault = await api(`${path}/lineups/${catalog.visible}`, 'GET', null, {}, 500);
    assert.equal('available' in fault, false, 'a database error must not masquerade as unavailable teaching');
  } finally { await sql.query('ALTER TABLE reference_test_video_off RENAME TO tutorial_videos'); }
  state = await command(a, state, { type: 'stage.lineups.set', stage_id: stageId, lineup_ids: [catalog.visible, catalog.hidden] }, 'TACTIC_LINEUP_UNAVAILABLE');
  state = await command(a, state, { type: 'stage.lineups.set', stage_id: stageId, lineup_ids: [catalog.visible, catalog.visible] }, 'VALIDATION_ERROR');
  state = await command(a, state, { type: 'stage.lineups.set', stage_id: stageId, lineup_ids: Array.from({ length: 11 }, () => randomUUID()) }, 'VALIDATION_ERROR');
  state = await command(a, state, { type: 'stage.lineups.set', stage_id: stageId, lineup_ids: [] }, 'TACTIC_CONFLICT', 0);
  await api(`${path}/lineups/${catalog.hidden}`, 'GET', null, {}, 404);
  assert.equal((await api(path)).tactic.revision, state.revision);
  await sql.query("UPDATE tutorial_videos SET status='archived' WHERE lineup_id=$1", [catalog.visible]);
  assert.deepEqual(await api(`${path}/lineups/${catalog.visible}`), { id: catalog.visible, available: false, lineup: null });
  const doc = new Y.Doc(); Y.applyUpdate(doc, Buffer.from(state.state, 'base64')); const vector = Y.encodeStateVector(doc);
  doc.getMap('fields').get('tactic.purpose').insert(0, '下架不阻止正常保存');
  const textId = randomUUID(); a.send({ type: 'operation', operation: { type: 'text.update', operation_id: textId, epoch: state.epoch, payload: { update: Buffer.from(Y.encodeStateAsUpdate(doc, vector)).toString('base64') } } }); doc.destroy();
  state = await a.wait((message) => message.type === 'operation.applied' && message.operation_id === textId);
  state = await command(a, state, { type: 'stage.lineups.set', stage_id: stageId, lineup_ids: [catalog.variant, catalog.visible] });
  state = await command(a, state, { type: 'stage.copy', stage_id: stageId });
  const copied = state.projection.structure.stages[1];
  assert.deepEqual(copied.lineup_ids, [catalog.variant, catalog.visible]);
  state = await command(a, state, { type: 'stage.lineups.set', stage_id: stageId, lineup_ids: [] });
  state = await command(a, state, { type: 'stage.delete', stage_id: copied.id });
  await api(`${path}/lineups/${catalog.visible}`, 'GET', null, {}, 404);
  const recovery = state.recoveries.find((item) => item.entity_id === copied.id);
  state = await command(a, state, { type: 'recovery.restore', recovery_id: recovery.id, after_stage_id: stageId });
  const restored = state.projection.structure.stages[1];
  assert.deepEqual(restored.lineup_ids, copied.lineup_ids); assert.notEqual(restored.id, copied.id);
  assert.equal((await api(`${path}/lineups/${catalog.visible}`)).available, false);
  state = await command(a, state, { type: 'stage.lineups.set', stage_id: restored.id, lineup_ids: [catalog.variant] });
  state = await command(a, state, { type: 'stage.lineups.set', stage_id: stageId, lineup_ids: [catalog.visible] }, 'TACTIC_LINEUP_UNAVAILABLE');
  await sql.query('UPDATE targets SET is_active=false WHERE id=$1', [catalog.target]);
  assert.deepEqual(await api(`${path}/lineups/${catalog.variant}`), { id: catalog.variant, available: false, lineup: null });
  const presentation = await api(`${path}/present`);
  assert.equal(presentation.revision, state.revision); assert.deepEqual(presentation.projection, state.projection);
  const noLogin = await fetch(`${backend}/api/v1${path}/lineups/${catalog.variant}`);
  assert.equal(noLogin.status, 401);
  await api('/auth/register', 'POST', { username: `other_${randomUUID().replaceAll('-', '').slice(0, 18)}`, display_name: '另一用户', password: 'references-test-password-2026' }, {}, 201);
  await api(`${path}/lineups/${catalog.variant}`, 'GET', null, {}, 404);
  await api(`${path}/present`, 'GET', null, {}, 404);
  process.stdout.write(`FORMAL_REFERENCES_HTTP_WS_PG_PASS ${identity}\n`);
} finally { for (const socket of sockets) socket.terminate(); await sql.end(); }
