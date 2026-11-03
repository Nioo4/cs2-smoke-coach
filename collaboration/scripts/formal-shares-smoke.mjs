// Actual HTTP/WS/PG share visibility and publication/revocation ordering.
import assert from 'node:assert/strict';
import { randomUUID } from 'node:crypto';
import * as Y from 'yjs';
import pg from 'pg';
import WebSocket from 'ws';

const database = new URL(process.env.SHARES_TEST_DATABASE_URL);
if (database.hostname !== '127.0.0.1' || database.port !== '65402' || database.pathname !== '/tactics_shares_selftest') throw new Error('shares_test_database_required');
const sql = new pg.Client({ connectionString: database.toString() }); await sql.connect();
const backend = process.env.FORMAL_TEST_BACKEND; const origin = process.env.FORMAL_TEST_ORIGIN;
let cookie = ''; let csrf = ''; let socket;
async function api(path, method = 'GET', body = null, expected = 200, anonymous = false) {
  const response = await fetch(`${backend}/api/v1${path}`, { method, headers: { origin, ...(anonymous ? {} : { cookie, 'x-csrf-token': csrf, 'idempotency-key': randomUUID() }), 'content-type': 'application/json' }, ...(body == null ? {} : { body: JSON.stringify(body) }) });
  if (!anonymous && response.headers.get('set-cookie')) cookie = response.headers.get('set-cookie').split(';')[0];
  const data = await response.json(); assert.equal(response.status, expected, JSON.stringify(data)); assert.equal(response.headers.get('cache-control'), 'no-store');
  if (data.csrf_token) csrf = data.csrf_token; return data;
}
const publicApi = (path, body, status = 200) => api(`/shares/${path}`, 'POST', body, status, true);
async function waitBlocked(count) {
  for (let index = 0; index < 150; index += 1) {
    await sql.query('SELECT pg_stat_clear_snapshot()');
    const result = await sql.query("SELECT count(*)::int n FROM pg_stat_activity WHERE datname=current_database() AND pid<>pg_backend_pid() AND wait_event_type='Lock' AND query LIKE '%FROM notebooks%'");
    if (result.rows[0].n >= count) return;
    await new Promise((resolve) => setTimeout(resolve, 20));
  }
  throw new Error('share_lock_wait_not_observed');
}
try {
  await api('/auth/register', 'POST', { username: `share_${randomUUID().replaceAll('-', '').slice(0, 17)}`, display_name: '分享作者', password: 'shares-smoke-password-2026' }, 201);
  const { notebook } = await api('/notebooks/personal');
  const made = await api(`/notebooks/${notebook.id}/tactics`, 'POST', { title: '发布原文', side: 'T' }, 201); const id = made.tactic.id;
  const { ticket } = await api(`/tactics/${id}/collaboration-ticket`, 'POST', {}, 201);
  socket = new WebSocket(process.env.FORMAL_TEST_WS, { origin }); const inbox = []; const waiters = [];
  socket.on('message', (raw) => { const message = JSON.parse(raw.toString()); const i = waiters.findIndex((item) => item.predicate(message)); if (i < 0) inbox.push(message); else { const [item] = waiters.splice(i, 1); clearTimeout(item.timer); item.resolve(message); } });
  const wait = (predicate) => { const i = inbox.findIndex(predicate); if (i >= 0) return Promise.resolve(inbox.splice(i, 1)[0]); return new Promise((resolve, reject) => { const item = { predicate, resolve, timer: setTimeout(() => reject(new Error('share_ws_timeout')), 6000) }; waiters.push(item); }); };
  await new Promise((resolve, reject) => { socket.once('open', resolve); socket.once('error', reject); });
  socket.send(JSON.stringify({ type: 'auth', ticket })); await wait((item) => item.type === 'auth.ok'); let state = await wait((item) => item.type === 'sync');
  const publish = (revision) => api('/shares', 'POST', { notebook_id: notebook.id, tactic_id: id, allow_copy: true, expected_revisions: [{ id, revision }] }, 201);
  const first = await publish(0); const g1 = await publicApi('exchange', { token: first.token });
  const doc = new Y.Doc(); Y.applyUpdate(doc, Buffer.from(state.state, 'base64')); const vector = Y.encodeStateVector(doc); doc.getMap('fields').get('tactic.title').insert(4, '后续草稿');
  const operationId = randomUUID(); socket.send(JSON.stringify({ type: 'operation', operation: { type: 'text.update', operation_id: operationId, epoch: state.epoch, payload: { update: Buffer.from(Y.encodeStateAsUpdate(doc, vector)).toString('base64') } } })); doc.destroy();
  state = await wait((item) => item.type === 'operation.applied' && item.operation_id === operationId);
  const oldPage = await publicApi('items', { read_grant: g1.read_grant, version_id: first.version_id, cursor: null });
  assert.equal(oldPage.items[0].projection.fields['tactic.title'], '发布原文');
  const updated = await api(`/shares/${first.id}/publish`, 'POST', { allow_copy: true, expected_revisions: [{ id, revision: state.revision }] });
  assert.notEqual(updated.version_id, first.version_id);
  await publicApi('items', { read_grant: g1.read_grant, version_id: first.version_id, cursor: null }, 409);
  assert.equal((await publicApi('read', { read_grant: g1.read_grant })).items[0].title, '发布原文后续草稿');
  // Read locks source notebook first; later revoke waits and then closes future reads.
  await sql.query('BEGIN'); await sql.query('SELECT id FROM notebooks WHERE id=$1 FOR UPDATE', [notebook.id]);
  const beforeRevoke = publicApi('read', { read_grant: g1.read_grant }); await waitBlocked(1);
  const revoke = api(`/shares/${first.id}/revoke`, 'POST', {}); await waitBlocked(2);
  await sql.query('COMMIT'); await beforeRevoke; await revoke;
  await publicApi('read', { read_grant: g1.read_grant }, 404);
  const second = await publish(state.revision); const g2 = await publicApi('exchange', { token: second.token });
  await sql.query('BEGIN'); await sql.query('SELECT id FROM notebooks WHERE id=$1 FOR UPDATE', [notebook.id]);
  const revokeFirst = api(`/shares/${second.id}/revoke`, 'POST', {}); await waitBlocked(1);
  const afterRevoke = publicApi('read', { read_grant: g2.read_grant }, 404); await waitBlocked(2);
  await sql.query('COMMIT'); await revokeFirst; await afterRevoke;
  const third = await publish(state.revision); const g3 = await publicApi('exchange', { token: third.token });
  await api(`/tactics/${id}/archive`, 'POST', { expected_revision: state.revision });
  await publicApi('exchange', { token: third.token }, 404); await publicApi('read', { read_grant: g3.read_grant }, 404);
  await api(`/tactics/${id}/restore`, 'POST', {});
  await publicApi('read', { read_grant: g3.read_grant }, 404);
  process.stdout.write('FORMAL_SHARES_HTTP_WS_PG_PASS\n');
} finally { await sql.query('ROLLBACK').catch(() => {}); socket?.terminate(); await sql.end(); }
