import { test } from 'node:test';
import assert from 'node:assert/strict';
import { randomUUID } from 'node:crypto';
import * as Y from 'yjs';
import { CollaborationCore } from '../src/collaboration-core.mjs';
import { createInitialData, createYDoc } from '../src/prototype-schema.mjs';
import { LeaseManager } from '../src/leases.mjs';
import { leaseGrantMessage, mapMoveOperation, parseLeaseGrant } from '../src/client-protocol.mjs';
import { mapObjectCenter } from '../src/drag-protocol.mjs';

class MemoryStore {
  loadCount = 0;
  document = null;
  operations = new Map();
  failBeforeCommit = false;
  failAfterCommit = false;

  async loadOrCreate(documentId) {
    this.loadCount += 1;
    await new Promise((resolve) => setTimeout(resolve, 5));
    if (!this.document) {
      const initial = createInitialData(documentId);
      const doc = createYDoc(initial.fields);
      this.document = { documentId, epoch: initial.epoch, revision: 0, state: Buffer.from(Y.encodeStateAsUpdate(doc)), structure: initial.structure };
      doc.destroy();
    }
    return { ...this.document, state: Buffer.from(this.document.state), structure: structuredClone(this.document.structure) };
  }

  async findOperation(_documentId, operationId) { return this.operations.get(operationId) ?? null; }

  async persist(operation) {
    if (this.failBeforeCommit) { this.failBeforeCommit = false; throw new Error('injected_database_failure'); }
    const prior = this.operations.get(operation.operationId);
    if (prior) {
      if (prior.actorId !== operation.actorId || prior.epoch !== operation.epoch || prior.fingerprint !== operation.fingerprint) throw new Error('operation_id_reused');
      return { operationId: operation.operationId, epoch: operation.epoch, revision: prior.revision, duplicate: true };
    }
    if (this.document.epoch !== operation.epoch) throw new Error('stale_epoch');
    if (this.document.revision !== operation.expectedRevision) throw new Error('revision_conflict');
    const revision = this.document.revision + 1;
    this.document = { ...this.document, revision, state: Buffer.from(operation.state), structure: structuredClone(operation.structure) };
    const record = { epoch: operation.epoch, actorId: operation.actorId, fingerprint: operation.fingerprint, type: operation.type, revision };
    this.operations.set(operation.operationId, record);
    if (this.failAfterCommit) { this.failAfterCommit = false; throw new Error('injected_commit_response_loss'); }
    return { operationId: operation.operationId, epoch: operation.epoch, revision, duplicate: false };
  }
}

function connect(core, actorId, name = actorId) {
  const messages = [];
  return core.connect({ actor: { id: actorId, name, color: actorId === 'actor-a' ? '#cb5a42' : '#4b8b82', role: 'editor' }, send: (message) => messages.push(message) }).then((connection) => ({ connection, messages }));
}

function clientDoc(sync) {
  const doc = new Y.Doc();
  Y.applyUpdate(doc, Buffer.from(sync.state, 'base64'));
  return doc;
}

function makeTextOperation(doc, text, value, operationId = randomUUID()) {
  let update;
  doc.once('update', (next, origin) => { if (origin === 'local-test') update = next; });
  doc.transact(() => text.insert(text.length, value), 'local-test');
  return { type: 'text.update', operation_id: operationId, epoch: null, payload: { update: Buffer.from(update).toString('base64') } };
}

test('parallel first connections share one room load and concurrent Chinese inserts converge', async () => {
  const store = new MemoryStore();
  const core = new CollaborationCore({ store });
  const [a, b] = await Promise.all([connect(core, 'actor-a'), connect(core, 'actor-b')]);
  assert.equal(store.loadCount, 1);
  const room = a.connection.room;
  assert.equal(room, b.connection.room);
  const syncA = a.messages.find((message) => message.type === 'sync');
  const syncB = b.messages.find((message) => message.type === 'sync');
  const docA = clientDoc(syncA);
  const docB = clientDoc(syncB);
  const opA = makeTextOperation(docA, docA.getMap('fields').get('step-1.role-a'), '甲');
  const opB = makeTextOperation(docB, docB.getMap('fields').get('step-1.role-a'), '乙');
  opA.epoch = room.epoch;
  opB.epoch = room.epoch;
  await Promise.all([core.operation(a.connection, opA), core.operation(b.connection, opB)]);
  const result = room.doc.getMap('fields').get('step-1.role-a').toString();
  assert.match(result, /甲/);
  assert.match(result, /乙/);
  assert.equal(room.revision, 2);
  assert.equal(a.messages.filter((message) => message.type === 'operation.applied').length, 2);
  docA.destroy(); docB.destroy(); core.disconnect(a.connection); core.disconnect(b.connection); room.doc.destroy();
});

test('injected database failure leaves memory unchanged and broadcasts no success', async () => {
  const store = new MemoryStore();
  const core = new CollaborationCore({ store });
  const a = await connect(core, 'actor-a');
  const room = a.connection.room;
  const before = room.doc.getMap('fields').get('step-1.role-a').toString();
  const doc = clientDoc(a.messages.find((message) => message.type === 'sync'));
  const operation = makeTextOperation(doc, doc.getMap('fields').get('step-1.role-a'), '未落盘');
  operation.epoch = room.epoch;
  a.messages.length = 0;
  store.failBeforeCommit = true;
  await assert.rejects(core.operation(a.connection, operation), /injected_database_failure/);
  assert.equal(room.revision, 0);
  assert.equal(room.doc.getMap('fields').get('step-1.role-a').toString(), before);
  assert.equal(a.messages.some((message) => message.type === 'operation.applied'), false);
  doc.destroy(); core.disconnect(a.connection); room.doc.destroy();
});

test('operation retry verifies actor, epoch and payload fingerprint before idempotent ack', async () => {
  const store = new MemoryStore();
  const core = new CollaborationCore({ store });
  const a = await connect(core, 'actor-a');
  const b = await connect(core, 'actor-b');
  const room = a.connection.room;
  const doc = clientDoc(a.messages.find((message) => message.type === 'sync'));
  const operation = makeTextOperation(doc, doc.getMap('fields').get('step-1.role-a'), '幂等');
  operation.epoch = room.epoch;
  await core.operation(a.connection, operation);
  const appliedCount = a.messages.filter((message) => message.type === 'operation.applied').length;
  await core.operation(a.connection, operation);
  assert.equal(a.messages.at(-1).type, 'operation.ack');
  assert.equal(a.messages.at(-1).duplicate, true);
  assert.equal(typeof a.messages.at(-1).state, 'string', 'duplicate acknowledgements carry authoritative document state');
  const ackDoc = clientDoc(a.messages.at(-1));
  assert.match(ackDoc.getMap('fields').get('step-1.role-a').toString(), /幂等/);
  assert.deepEqual(a.messages.at(-1).structure, room.structure);
  ackDoc.destroy();
  await assert.rejects(core.operation(b.connection, operation), /operation_id_reused/);
  const changed = { ...operation, payload: { update: `${operation.payload.update}AAAA` } };
  await assert.rejects(core.operation(a.connection, changed), /operation_id_reused/);
  await assert.rejects(core.operation(a.connection, { ...operation, epoch: randomUUID() }), /stale_epoch/);
  assert.equal(room.revision, 1);
  assert.equal(a.messages.filter((message) => message.type === 'operation.applied').length, appliedCount);
  doc.destroy(); core.disconnect(a.connection); core.disconnect(b.connection); room.doc.destroy();
});

test('retry reconciles memory after commit succeeds but its response is lost', async () => {
  const store = new MemoryStore();
  const core = new CollaborationCore({ store });
  const a = await connect(core, 'actor-a');
  const room = a.connection.room;
  const doc = clientDoc(a.messages.find((message) => message.type === 'sync'));
  const operation = makeTextOperation(doc, doc.getMap('fields').get('step-1.role-a'), '提交成功但回执丢失');
  operation.epoch = room.epoch;
  store.failAfterCommit = true;
  await assert.rejects(core.operation(a.connection, operation), /injected_commit_response_loss/);
  assert.equal(room.revision, 0);
  const retry = await core.operation(a.connection, operation);
  assert.equal(retry.duplicate, true);
  assert.equal(room.revision, 1);
  assert.match(room.doc.getMap('fields').get('step-1.role-a').toString(), /提交成功但回执丢失/);
  assert.ok(a.messages.some((message) => message.type === 'operation.ack' && message.duplicate));
  assert.ok(a.messages.some((message) => message.type === 'sync' && message.revision === 1));
  doc.destroy(); core.disconnect(a.connection); room.doc.destroy();
});

test('map leases conflict per object, permit other objects, release on disconnect and expire', () => {
  let now = 0;
  const manager = new LeaseManager({ now: () => now, leaseMs: 10_000, renewalMs: 3_000 });
  const actorA = { id: 'actor-a', name: '参与者 A', color: '#cb5a42' };
  const actorB = { id: 'actor-b', name: '参与者 B', color: '#4b8b82' };
  const first = manager.acquire({ objectId: 'role-a-start', version: 1, connectionId: 'connection-a', actor: actorA, currentVersion: 1 });
  assert.throws(() => manager.acquire({ objectId: 'role-a-start', version: 1, connectionId: 'connection-b', actor: actorB, currentVersion: 1 }), (error) => error.code === 'COLLAB_LOCKED');
  manager.acquire({ objectId: 'role-b-start', version: 1, connectionId: 'connection-b', actor: actorB, currentVersion: 1 });
  assert.equal(manager.preview({ objectId: 'role-a-start', token: first.token, connectionId: 'connection-a', x: 20, y: 30 }), true);
  assert.equal(manager.preview({ objectId: 'role-a-start', token: first.token, connectionId: 'connection-a', x: 21, y: 31 }), false);
  manager.releaseConnection('connection-a');
  assert.equal(manager.snapshot().some((lease) => lease.objectId === 'role-a-start'), false);
  now += 10_001;
  assert.equal(manager.snapshot().length, 0);
});

test('asymmetric three-point arrow drag shares the rendered bounding-box center and persists the intended move', async () => {
  const store = new MemoryStore();
  const core = new CollaborationCore({ store });
  const a = await connect(core, 'actor-a');
  const room = a.connection.room;
  const arrow = room.structure.steps[0].objects.find((object) => object.id === 'arrow-a-mid');
  const start = mapObjectCenter(arrow);
  assert.deepEqual(start, { x: 45, y: 62 });

  const acquired = core.acquireLease(a.connection, { step_id: 'step-1', object_id: arrow.id, version: arrow.version });
  const lease = parseLeaseGrant(leaseGrantMessage('drag-request', acquired));
  const drag = { objectId: arrow.id, stepId: 'step-1', version: arrow.version, x: 70, y: 30 };
  const operation = mapMoveOperation({ operationId: randomUUID(), epoch: room.epoch, drag, lease });
  await core.operation(a.connection, operation);

  assert.deepEqual(room.structure.steps[0].objects.find((object) => object.id === arrow.id).points, [
    { x: 58, y: 45 }, { x: 73, y: 30 }, { x: 82, y: 15 },
  ]);
  assert.equal(room.revision, 1);
  core.disconnect(a.connection); room.doc.destroy();
});
