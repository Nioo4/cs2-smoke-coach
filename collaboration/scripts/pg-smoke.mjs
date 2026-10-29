import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { randomUUID } from 'node:crypto';
import assert from 'node:assert/strict';
import * as Y from 'yjs';
import { CollaborationCore } from '../src/collaboration-core.mjs';
import { PostgresStore } from '../src/postgres-store.mjs';

const databaseUrl = process.env.TACTICS_T0_DATABASE_URL ?? process.env.DATABASE_URL;
const scriptPath = fileURLToPath(import.meta.url);
const [mode, workerMode, documentId, marker] = process.argv.slice(2);

if (mode === 'worker') {
  await runWorker({ mode: workerMode, documentId, marker, databaseUrl });
} else {
  if (!databaseUrl) throw new Error('Set TACTICS_T0_DATABASE_URL to the isolated T0 PostgreSQL connection string.');
  const testDocumentId = `pg-restart-${randomUUID()}`;
  const expected = `跨进程恢复-${randomUUID()}`;
  const write = spawnSync(process.execPath, [scriptPath, 'worker', 'write', testDocumentId, expected], { env: { ...process.env, TACTICS_T0_DATABASE_URL: databaseUrl }, encoding: 'utf8', timeout: 30_000 });
  assert.equal(write.status, 0, `write worker failed: ${write.stdout}\n${write.stderr}`);
  assert.match(write.stdout, /T0_PG_WRITE_ACK/);
  const read = spawnSync(process.execPath, [scriptPath, 'worker', 'read', testDocumentId, expected], { env: { ...process.env, TACTICS_T0_DATABASE_URL: databaseUrl }, encoding: 'utf8', timeout: 30_000 });
  assert.equal(read.status, 0, `read worker failed: ${read.stdout}\n${read.stderr}`);
  assert.match(read.stdout, /T0_PG_RESTART_READBACK_PASS/);
  await verifyCommitTimeout(databaseUrl);
  await verifyStagePersistence(databaseUrl);
  process.stdout.write(`${write.stdout.trim()}\n${read.stdout.trim()}\nT0_REAL_POSTGRES_PROCESS_RESTART_PASS document=${testDocumentId}\n`);
}

async function verifyStagePersistence(databaseUrl) {
  const documentId = `t0-stage-test-${randomUUID()}`;
  const firstStore = new PostgresStore(databaseUrl);
  const firstCore = new CollaborationCore({ store: firstStore });
  let firstRoom;
  let writer;
  try {
    await firstStore.initialize();
    firstRoom = await firstCore.room(documentId);
    const messages = [];
    writer = await firstCore.connect({ documentId, actor: { id: 'pg-stage-writer', name: '隔离PG阶段测试', color: '#cb5a42', role: 'editor' }, send: (message) => messages.push(message) });
    const sync = messages.find((message) => message.type === 'sync');
    const stageId = sync.structure.stages[0].id;
    const roleId = sync.structure.roles[0].id;
    const operation = {
      type: 'stage.command', operation_id: randomUUID(), epoch: sync.epoch,
      payload: { expected_revision: sync.revision, command: { type: 'action.add', stage_id: stageId, kind: 'wait', role_id: roleId, group_mode: 'after', description: '隔离PG动作说明回读校验。' } },
    };
    await firstCore.operation(writer, operation);
    assert.equal(firstRoom.revision, 1);
    assert.equal(messages.some((message) => message.type === 'operation.applied' && message.operation_id === operation.operation_id), true);
  } finally {
    if (writer) firstCore.disconnect(writer);
    firstRoom?.doc.destroy();
    await firstStore.close();
  }

  const readStore = new PostgresStore(databaseUrl);
  const readCore = new CollaborationCore({ store: readStore });
  let readRoom;
  try {
    await readStore.initialize();
    const messages = [];
    const reader = await readCore.connect({ documentId, actor: { id: 'pg-stage-reader', name: '隔离PG阶段读回', color: '#4b8b82', role: 'editor' }, send: (message) => messages.push(message) });
    readRoom = reader.room;
    const sync = messages.find((message) => message.type === 'sync');
    assert.equal(sync.revision, 1);
    assert.equal(sync.structure.stages[0].actions.length, 1);
    const action = sync.structure.stages[0].actions[0];
    assert.equal(action.kind, 'wait');
    assert.equal(sync.fields[`action.${action.id}.description`], '隔离PG动作说明回读校验。');
    readCore.disconnect(reader);
    process.stdout.write(`T0_STAGE_PG_STRUCTURAL_COMMIT_READBACK_PASS document=${documentId} revision=${sync.revision}\n`);
  } finally {
    readRoom?.doc.destroy();
    await readStore.close();
  }
}

async function runWorker({ mode: workerMode, documentId: id, marker: expected, databaseUrl: url }) {
  if (!url || !['write', 'read'].includes(workerMode) || !id || !expected) throw new Error('invalid_pg_smoke_worker_arguments');
  const store = new PostgresStore(url);
  const core = new CollaborationCore({ store });
  let room;
  try {
    await store.initialize();
    room = await core.room(id);
    if (workerMode === 'write') {
      const messages = [];
      const connection = await core.connect({ documentId: id, actor: { id: 'pg-smoke-writer', name: '隔离PG测试', color: '#cb5a42', role: 'editor' }, send: (message) => messages.push(message) });
      const sync = messages.find((message) => message.type === 'sync');
      const client = new Y.Doc();
      Y.applyUpdate(client, Buffer.from(sync.state, 'base64'));
      const text = client.getMap('fields').get('step-1.role-a');
      let update;
      client.once('update', (next, origin) => { if (origin === 'pg-smoke') update = next; });
      client.transact(() => text.insert(text.length, expected), 'pg-smoke');
      await core.operation(connection, { type: 'text.update', operation_id: randomUUID(), epoch: room.epoch, payload: { update: Buffer.from(update).toString('base64') } });
      assert.equal(room.revision, 1);
      process.stdout.write(`T0_PG_WRITE_ACK revision=${room.revision}\n`);
      client.destroy();
      core.disconnect(connection);
    } else {
      const messages = [];
      const connection = await core.connect({ documentId: id, actor: { id: 'pg-smoke-reader', name: '隔离PG读取', color: '#4b8b82', role: 'editor' }, send: (message) => messages.push(message) });
      const sync = messages.find((message) => message.type === 'sync');
      assert.equal(sync.revision, 1);
      assert.match(sync.fields['step-1.role-a'], new RegExp(escapeRegExp(expected)));
      process.stdout.write(`T0_PG_RESTART_READBACK_PASS revision=${sync.revision}\n`);
      core.disconnect(connection);
    }
  } finally {
    room?.doc.destroy();
    await store.close();
  }
}

async function verifyCommitTimeout(databaseUrl) {
  const documentId = `pg-timeout-${randomUUID()}`;
  const marker = `等待超时后仍保留-${randomUUID()}`;
  const store = new PostgresStore(databaseUrl, { statementTimeoutMillis: 300 });
  const core = new CollaborationCore({ store });
  let room;
  let lockClient;
  let clientDoc;
  let connection;
  const messages = [];
  try {
    await store.initialize();
    room = await core.room(documentId);
    connection = await core.connect({ documentId, actor: { id: 'pg-timeout-writer', name: '隔离PG故障测试', color: '#cb5a42', role: 'editor' }, send: (message) => messages.push(message) });
    const sync = messages.find((message) => message.type === 'sync');
    clientDoc = new Y.Doc();
    Y.applyUpdate(clientDoc, Buffer.from(sync.state, 'base64'));
    const text = clientDoc.getMap('fields').get('step-1.role-a');
    let update;
    clientDoc.once('update', (next, origin) => { if (origin === 'pg-timeout') update = next; });
    clientDoc.transact(() => text.insert(text.length, marker), 'pg-timeout');
    assert.match(text.toString(), new RegExp(escapeRegExp(marker)), 'the unsaved local editor state remains available');

    lockClient = await store.pool.connect();
    await lockClient.query('BEGIN');
    await lockClient.query('SELECT revision FROM tactics_t0_documents WHERE document_id = $1 FOR UPDATE', [documentId]);
    const operation = {
      type: 'text.update', operation_id: randomUUID(), epoch: room.epoch,
      payload: { update: Buffer.from(update).toString('base64') },
    };
    let failure;
    try { await core.operation(connection, operation); } catch (error) { failure = error; }
    assert.ok(failure, 'persist must fail when its row lock exceeds the statement timeout');
    core.reject(connection, operation, failure);
    assert.equal(messages.some((message) => ['operation.applied', 'operation.ack'].includes(message.type)), false, 'failed persistence must not acknowledge the write');
    assert.equal(messages.at(-2)?.type, 'operation.rejected');
    assert.equal(messages.at(-1)?.type, 'sync');
    assert.equal(room.revision, 0);
    assert.equal(room.doc.getMap('fields').get('step-1.role-a').toString().includes(marker), false);
    assert.equal(messages.at(-1).fields['step-1.role-a'].includes(marker), false, 'authoritative sync must not claim the unsaved edit was saved');

    await lockClient.query('ROLLBACK');
    lockClient.release();
    lockClient = null;
    const persisted = await store.loadOrCreate(documentId);
    assert.equal(persisted.revision, 0);
    assert.equal(persisted.structure.steps.length, room.structure.steps.length);
    process.stdout.write(`T0_REAL_POSTGRES_TIMEOUT_NO_FALSE_ACK_PASS document=${documentId} pending_payload_bytes=${Buffer.byteLength(operation.payload.update, 'base64')}\n`);
  } finally {
    if (lockClient) {
      await lockClient.query('ROLLBACK').catch(() => {});
      lockClient.release();
    }
    if (connection) core.disconnect(connection);
    clientDoc?.destroy();
    room?.doc.destroy();
    await store.close();
  }
}

function escapeRegExp(value) { return value.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'); }
