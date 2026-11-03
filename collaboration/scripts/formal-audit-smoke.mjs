// Dedicated audit integration client, never an application entry point.
import assert from 'node:assert/strict';
import { randomUUID } from 'node:crypto';
import WebSocket from 'ws';
import * as Y from 'yjs';

const backend = new URL(process.env.FORMAL_TEST_BACKEND);
const wsUrl = new URL(process.env.FORMAL_TEST_WS);
if (process.env.AUDIT_TEST_DATABASE_NAME !== 'tactics_audit_selftest' || backend.hostname !== '127.0.0.1' || wsUrl.hostname !== '127.0.0.1' || [18220, 18221, 18222].includes(Number(backend.port))) throw new Error('isolated_audit_fixture_required');
const origin = process.env.FORMAL_TEST_ORIGIN; let cookie = ''; let csrf = ''; let ws;
async function api(path, method = 'GET', body) {
  const response = await fetch(`${backend.origin}/api/v1${path}`, { method,
    headers: { origin, cookie, 'x-csrf-token': csrf, 'content-type': 'application/json', 'idempotency-key': randomUUID(), 'x-request-id': 'f'.repeat(32) },
    ...(body === undefined ? {} : { body: JSON.stringify(body) }) });
  assert.equal(response.ok, true, `HTTP_${response.status}`);
  if (response.headers.get('set-cookie')) cookie = response.headers.get('set-cookie').split(';')[0];
  const data = await response.json(); if (data.csrf_token) csrf = data.csrf_token; return data;
}
try {
  const account = await api('/auth/register', 'POST', { username: `audit_${randomUUID().replaceAll('-', '').slice(0, 18)}`, display_name: '审计WS隔离', password: 'audit-ws-public-fixture-2026' });
  const { notebook } = await api('/notebooks/personal');
  const { tactic } = await api(`/notebooks/${notebook.id}/tactics`, 'POST', { title: '审计真实正文', side: 'T' });
  const { ticket } = await api(`/tactics/${tactic.id}/collaboration-ticket`, 'POST', {});
  ws = new WebSocket(wsUrl, { origin }); const inbox = []; const waiters = [];
  const wait = predicate => {
    const index = inbox.findIndex(predicate); if (index >= 0) return Promise.resolve(inbox.splice(index, 1)[0]);
    return new Promise((resolve, reject) => { const waiter = { predicate, resolve, timer: setTimeout(() => { waiters.splice(waiters.indexOf(waiter), 1); reject(new Error('audit_message_timeout')); }, 7000) }; waiters.push(waiter); });
  };
  ws.on('message', raw => { const value = JSON.parse(raw); const index = waiters.findIndex(item => item.predicate(value)); if (index < 0) inbox.push(value); else { const [item] = waiters.splice(index, 1); clearTimeout(item.timer); item.resolve(value); } });
  await new Promise((resolve, reject) => { ws.once('open', resolve); ws.once('error', reject); });
  ws.send(JSON.stringify({ type: 'auth', ticket })); await wait(message => message.type === 'auth.ok');
  let state = await wait(message => message.type === 'sync'); const operations = [];
  const submit = async operation => {
    ws.send(JSON.stringify({ type: 'operation', operation }));
    const result = await wait(message => message.operation_id === operation.operation_id && ['operation.applied', 'operation.rejected'].includes(message.type));
    assert.equal(result.type, 'operation.applied', result.code); operations.push(operation.operation_id); return result;
  };
  state = await submit({ type: 'stage.command', operation_id: randomUUID(), epoch: state.epoch,
    payload: { expected_revision: state.revision, command: { type: 'role.add' } } });
  const doc = new Y.Doc(); Y.applyUpdate(doc, Buffer.from(state.state, 'base64'));
  const vector = Y.encodeStateVector(doc); doc.getMap('fields').get('tactic.purpose').insert(0, 'AUDIT_BODY_SENTINEL');
  const operation = { type: 'text.update', operation_id: randomUUID(), epoch: state.epoch,
    payload: { update: Buffer.from(Y.encodeStateAsUpdate(doc, vector)).toString('base64') } }; doc.destroy();
  state = await submit(operation); ws.send(JSON.stringify({ type: 'operation', operation }));
  const ack = await wait(message => message.type === 'operation.ack' && message.operation_id === operation.operation_id);
  assert.equal(ack.current_revision, state.revision);
  process.stdout.write(JSON.stringify({ actor_id: account.user.id, tactic_id: tactic.id, operations }) + '\n');
} finally { ws?.close(); }
