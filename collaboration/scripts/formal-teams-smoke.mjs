// Isolated T3 real HTTP/WS/PG regression; never imported by the application runtime.
import assert from 'node:assert/strict';
import { randomUUID } from 'node:crypto';
import * as Y from 'yjs';
import WebSocket from 'ws';
import pg from 'pg';
import { canonicalJson, encodeFormalDocument, sha256 } from '../src/formal-document.mjs';

const url = new URL(process.env.TEAMS_TEST_DATABASE_URL);
if (url.hostname !== '127.0.0.1' || url.port !== '65402' || url.pathname !== '/tactics_teams_selftest') throw new Error('teams_test_database_required');
const sql = new pg.Client({ connectionString: url.toString() }); await sql.connect();
const backend = process.env.FORMAL_TEST_BACKEND; const origin = process.env.FORMAL_TEST_ORIGIN;
const sockets = [];
function client() {
  let cookie = ''; let csrf = '';
  return async (path, method = 'GET', body = null, status = 200, key = randomUUID()) => {
    const response = await fetch(`${backend}/api/v1${path}`, { method, headers: { origin, cookie, 'x-csrf-token': csrf, 'idempotency-key': key, 'content-type': 'application/json' }, ...(body == null ? {} : { body: JSON.stringify(body) }) });
    if (response.headers.get('set-cookie')) cookie = response.headers.get('set-cookie').split(';')[0];
    const data = await response.json(); assert.equal(response.status, status, `${path}: ${JSON.stringify(data)}`);
    assert.equal(response.headers.get('cache-control'), 'no-store'); if (data.csrf_token) csrf = data.csrf_token; return data;
  };
}
async function connect(api, id) {
  const { ticket } = await api(`/tactics/${id}/collaboration-ticket`, 'POST', {}, 201);
  const ws = new WebSocket(process.env.FORMAL_TEST_WS, { origin }); const inbox = []; const waiters = [];
  ws.on('message', (raw) => { const message = JSON.parse(raw.toString()); const index = waiters.findIndex((item) => item.predicate(message)); if (index < 0) inbox.push(message); else { const [item] = waiters.splice(index, 1); clearTimeout(item.timer); item.resolve(message); } });
  const channel = { ws, wait: (predicate) => {
    const index = inbox.findIndex(predicate); if (index >= 0) return Promise.resolve(inbox.splice(index, 1)[0]);
    return new Promise((resolve, reject) => { const item = { predicate, resolve, timer: setTimeout(() => reject(new Error('teams_ws_timeout')), 6000) }; waiters.push(item); });
  } };
  sockets.push(ws); await new Promise((resolve, reject) => { ws.once('open', resolve); ws.once('error', reject); });
  ws.send(JSON.stringify({ type: 'auth', ticket })); channel.auth = await channel.wait((message) => message.type === 'auth.ok'); channel.sync = await channel.wait((message) => message.type === 'sync'); return channel;
}
async function internal(path, body, expected = 200) {
  const result = await fetch(`${backend}/api/v1/internal/tactics${path}`, { method: 'POST', headers: { 'content-type': 'application/json', 'internal-key': process.env.FORMAL_TEST_INTERNAL_KEY }, body: JSON.stringify(body) });
  const data = await result.json(); assert.equal(result.status, expected, JSON.stringify(data)); return data;
}
async function grant(api, id) {
  const { ticket } = await api(`/tactics/${id}/collaboration-ticket`, 'POST', {}, 201);
  return internal('/tickets/consume', { ticket, connection_id: randomUUID(), node_instance_id: randomUUID() }, 201);
}
function packet(saved, context, text) {
  const doc = new Y.Doc(); Y.applyUpdate(doc, Buffer.from(saved.state_base64, 'base64'));
  const vector = Y.encodeStateVector(doc); const field = doc.getMap('fields').get('tactic.purpose'); field.insert(field.length, text);
  const original = { update: Buffer.from(Y.encodeStateAsUpdate(doc, vector)).toString('base64') };
  const payload = canonicalJson(original); const encoded = encodeFormalDocument(doc); doc.destroy();
  return { grant_id: context.grant_id, epoch: saved.epoch, operation_id: randomUUID(), type: 'text.update', expected_revision: saved.revision, original_payload: original, payload_json: payload, fingerprint: sha256(`text.update\n${saved.epoch}\n${payload}`), ...encoded };
}
async function waiting(count, pattern = 'FROM teams') {
  for (let attempt = 0; attempt < 150; attempt += 1) {
    await sql.query('SELECT pg_stat_clear_snapshot()');
    const { rows } = await sql.query("SELECT count(*)::int AS n FROM pg_stat_activity WHERE datname=current_database() AND pid<>pg_backend_pid() AND wait_event_type='Lock' AND query LIKE $1", [`%${pattern}%`]);
    if (rows[0].n >= count) return;
    await new Promise((resolve) => setTimeout(resolve, 20));
  }
  throw new Error('team_lock_wait_not_observed');
}
async function block(id) { await sql.query('BEGIN'); await sql.query('SELECT id FROM teams WHERE id=$1 FOR UPDATE', [id]); }
const closed = (channel) => new Promise((resolve, reject) => { const timer = setTimeout(() => reject(new Error('revoked_socket_still_open_after_5s')), 5000); channel.ws.once('close', (code) => { clearTimeout(timer); resolve(code); }); });
try {
  const a = client(); const b = client();
  await a('/auth/register', 'POST', { username: `owner_${randomUUID().replaceAll('-', '').slice(0, 16)}`, display_name: 'owner', password: 'teams-smoke-password-2026' }, 201);
  const member = await b('/auth/register', 'POST', { username: `member_${randomUUID().replaceAll('-', '').slice(0, 16)}`, display_name: 'member', password: 'teams-smoke-password-2026' }, 201);
  const { team, notebook } = await a('/teams', 'POST', { name: 'TCP团队' }, 201);
  const invite = await a(`/teams/${team.id}/invites`, 'POST', {}, 201);
  await b('/invites/accept', 'POST', { token: invite.token });
  const created = await a(`/notebooks/${notebook.id}/tactics`, 'POST', { title: '协作权限', side: 'T' }, 201); const id = created.tactic.id;
  const viewer = await connect(b, id); assert.equal(viewer.auth.read_only, true);
  const operationId = randomUUID(); viewer.ws.send(JSON.stringify({ type: 'operation', operation: { type: 'stage.command', operation_id: operationId, epoch: viewer.sync.epoch, payload: { expected_revision: 0, command: { type: 'stage.add', name: '无权' } } } }));
  assert.equal((await viewer.wait((message) => message.type === 'operation.rejected' && message.operation_id === operationId)).code, 'USER_FORBIDDEN');
  const viewerClosed = closed(viewer);
  await a(`/teams/${team.id}/members/${member.user.id}`, 'PATCH', { role: 'editor' }); await viewerClosed;
  const editor = await connect(b, id); assert.equal(editor.auth.read_only, false);
  const seats = [];
  for (let index = 0; index < 5; index += 1) seats.push(await connect(a, id));
  assert.deepEqual(seats.map((item) => item.auth.read_only), [false, false, false, false, true]);
  // Commit acquires the team lock first; the subsequent downgrade waits and then revokes grants.
  const g1 = await grant(b, id); const saved1 = await internal(`/${id}/load`, { grant_id: g1.grant_id });
  await block(team.id);
  const firstCommit = internal(`/${id}/commit`, packet(saved1, g1, '撤权前'));
  await waiting(1);
  const firstRevoke = a(`/teams/${team.id}/members/${member.user.id}`, 'PATCH', { role: 'viewer' });
  await waiting(2); const editorClosed = closed(editor); await sql.query('COMMIT');
  assert.equal((await firstCommit).revision, 1); await firstRevoke; await editorClosed;
  const afterFirst = await a(`/tactics/${id}`); assert.equal(afterFirst.projection.fields['tactic.purpose'], '撤权前');
  await internal(`/${id}/commit`, packet(saved1, g1, '不得后写'), 401);
  // Downgrade acquires the team lock first; the queued commit must fail without revision advance.
  await a(`/teams/${team.id}/members/${member.user.id}`, 'PATCH', { role: 'editor' });
  const g2 = await grant(b, id); const saved2 = await internal(`/${id}/load`, { grant_id: g2.grant_id });
  await block(team.id);
  const secondRevoke = a(`/teams/${team.id}/members/${member.user.id}`, 'PATCH', { role: 'viewer' }); await waiting(1);
  const secondCommit = internal(`/${id}/commit`, packet(saved2, g2, '拒绝'), 401); await waiting(2);
  await sql.query('COMMIT'); await secondRevoke; await secondCommit;
  assert.equal((await a(`/tactics/${id}`)).tactic.revision, 1);
  // Restore edit and transfer ownership while the target's commit waits on the same team.
  await a(`/teams/${team.id}/members/${member.user.id}`, 'PATCH', { role: 'editor' });
  const g3 = await grant(b, id); const saved3 = await internal(`/${id}/load`, { grant_id: g3.grant_id });
  await block(team.id);
  const targetCommit = internal(`/${id}/commit`, packet(saved3, g3, '转让前')); await waiting(1);
  const transfer = a(`/teams/${team.id}/ownership`, 'POST', { user_id: member.user.id }); await waiting(2);
  await sql.query('COMMIT'); await targetCommit; await transfer;
  assert.equal((await b(`/teams/${team.id}`)).my_role, 'owner');
  // Opposite direction: ownership FK checks coexist with the waiting target user's identity lock.
  const g4 = await grant(a, id); const saved4 = await internal(`/${id}/load`, { grant_id: g4.grant_id });
  const originalId = (await a('/auth/session')).user.id;
  await block(team.id);
  const transferBack = b(`/teams/${team.id}/ownership`, 'POST', { user_id: originalId }); await waiting(1);
  const backCommit = internal(`/${id}/commit`, packet(saved4, g4, '转让后')); await waiting(2);
  await sql.query('COMMIT'); await transferBack; await backCommit;
  // Credential revocation and commits share an exclusive non-key user lock in either order.
  const g5 = await grant(a, id); const saved5 = await internal(`/${id}/load`, { grant_id: g5.grant_id });
  await sql.query('BEGIN'); await sql.query('SELECT id FROM users WHERE id=$1 FOR NO KEY UPDATE', [originalId]);
  const beforePassword = internal(`/${id}/commit`, packet(saved5, g5, '密码前')); await waiting(1, 'FROM users');
  const password1 = a('/auth/password', 'POST', { current_password: 'teams-smoke-password-2026', new_password: 'teams-smoke-password-next-2026' }); await waiting(2, 'FROM users');
  await sql.query('COMMIT'); await beforePassword; await password1;
  await internal(`/${id}/commit`, packet(saved5, g5, '撤session后拒绝'), 401);
  const g6 = await grant(a, id); const saved6 = await internal(`/${id}/load`, { grant_id: g6.grant_id });
  // Pause the final credential UPDATE, after its user lock, rather than its earlier password check.
  await sql.query("CREATE FUNCTION teams_test_credential_gate() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN IF NEW.credential_version<>OLD.credential_version THEN PERFORM pg_advisory_xact_lock(884265001); END IF; RETURN NEW; END $$");
  await sql.query('CREATE TRIGGER teams_test_credential_gate BEFORE UPDATE ON users FOR EACH ROW EXECUTE FUNCTION teams_test_credential_gate()');
  await sql.query('SELECT pg_advisory_lock(884265001)');
  const password2 = a('/auth/password', 'POST', { current_password: 'teams-smoke-password-next-2026', new_password: 'teams-smoke-password-final-2026' }); await waiting(1, 'UPDATE users');
  const afterPassword = internal(`/${id}/commit`, packet(saved6, g6, '密码后拒绝'), 401); await waiting(1, 'FROM users');
  await sql.query('SELECT pg_advisory_unlock(884265001)'); await password2; await afterPassword;
  process.stdout.write(`FORMAL_TEAMS_HTTP_WS_PG_PASS ${id}\n`);
} finally {
  await sql.query('ROLLBACK').catch(() => {}); await sql.query('SELECT pg_advisory_unlock_all()').catch(() => {});
  await sql.query('DROP TRIGGER IF EXISTS teams_test_credential_gate ON users').catch(() => {});
  await sql.query('DROP FUNCTION IF EXISTS teams_test_credential_gate()').catch(() => {});
  for (const socket of sockets) socket.terminate(); await sql.end();
}
