import { test } from 'node:test';
import assert from 'node:assert/strict';
import { randomUUID } from 'node:crypto';
import * as Y from 'yjs';
import { CollaborationCore } from '../src/collaboration-core.mjs';
import { createStageInitialData, createStageYDoc, actionDescriptionField, roleNameField, STAGE_DOCUMENT_ID } from '../src/stage-board-schema.mjs';
import { createInitialData, createYDoc, DEFAULT_DOCUMENT_ID } from '../src/prototype-schema.mjs';

class MultiMemoryStore {
  documents = new Map();
  operations = new Map();
  async loadOrCreate(documentId) {
    if (!this.documents.has(documentId)) {
      const initial = documentId === STAGE_DOCUMENT_ID ? createStageInitialData() : createInitialData(documentId);
      const doc = documentId === STAGE_DOCUMENT_ID ? createStageYDoc(initial.fields) : createYDoc(initial.fields);
      this.documents.set(documentId, { documentId, epoch: initial.epoch, revision: 0, state: Buffer.from(Y.encodeStateAsUpdate(doc)), structure: initial.structure });
      doc.destroy();
    }
    const saved = this.documents.get(documentId);
    return { ...saved, state: Buffer.from(saved.state), structure: structuredClone(saved.structure) };
  }
  async findOperation(documentId, id) { return this.operations.get(`${documentId}:${id}`) ?? null; }
  async persist(op) {
    const document = this.documents.get(op.documentId);
    const prior = await this.findOperation(op.documentId, op.operationId);
    if (prior) return { ...prior, duplicate: true };
    if (document.epoch !== op.epoch) throw new Error('stale_epoch');
    if (document.revision !== op.expectedRevision) throw new Error('revision_conflict');
    const revision = document.revision + 1;
    this.documents.set(op.documentId, { ...document, revision, state: Buffer.from(op.state), structure: structuredClone(op.structure) });
    const record = { epoch: op.epoch, actorId: op.actorId, fingerprint: op.fingerprint, type: op.type, revision };
    this.operations.set(`${op.documentId}:${op.operationId}`, record);
    return { operationId: op.operationId, epoch: op.epoch, revision, duplicate: false };
  }
}

function connect(core, actorId, documentId = STAGE_DOCUMENT_ID) {
  const messages = [];
  return core.connect({ documentId, actor: { id: actorId, name: actorId, color: '#cb5a42', role: 'editor' }, send: (message) => messages.push(message) })
    .then((connection) => ({ connection, messages }));
}

function copyDoc(encoded) {
  const doc = new Y.Doc();
  Y.applyUpdate(doc, Buffer.from(encoded, 'base64'));
  return doc;
}

function textOperation(doc, epoch, key, value) {
  const before = Y.encodeStateVector(doc);
  const text = doc.getMap('fields').get(key);
  doc.transact(() => text.insert(text.length, value), 'participant');
  const update = Y.encodeStateAsUpdate(doc, before);
  return { type: 'text.update', operation_id: randomUUID(), epoch, payload: { update: Buffer.from(update).toString('base64') } };
}

function commandOperation(room, command, expectedRevision = room.revision) {
  return {
    type: 'stage.command', operation_id: randomUUID(), epoch: room.epoch,
    payload: { expected_revision: expectedRevision, command },
  };
}

test('dynamic board uses an independent room and structural commits broadcast the matching Yjs state', async () => {
  const store = new MultiMemoryStore();
  const core = new CollaborationCore({ store });
  const [a, b, legacy] = await Promise.all([
    connect(core, 'actor-a'), connect(core, 'actor-b'), connect(core, 'legacy', DEFAULT_DOCUMENT_ID),
  ]);
  assert.notEqual(a.connection.room, legacy.connection.room);
  assert.equal(a.connection.room.documentId, STAGE_DOCUMENT_ID);
  assert.equal(legacy.connection.room.documentId, DEFAULT_DOCUMENT_ID);
  const room = a.connection.room;
  const stage = room.structure.stages[0];
  const operation = commandOperation(room, { type: 'action.add', stage_id: stage.id, kind: 'wait', role_id: room.structure.roles[0].id, group_mode: 'after', description: '留在当前区域，等待队友确认。' });
  await core.operation(a.connection, operation);
  const applied = b.messages.find((message) => message.type === 'operation.applied');
  const field = actionDescriptionField(applied.structure.stages[0].actions[0].id);
  assert.equal(applied.operation_id, operation.operation_id);
  assert.equal(applied.revision, 1);
  assert.deepEqual(applied.structure, room.structure);
  const client = copyDoc(applied.state);
  assert.equal(client.getMap('fields').get(field).toString(), '留在当前区域，等待队友确认。');
  assert.equal(legacy.connection.room.revision, 0);
  assert.equal(legacy.connection.room.structure.document_id, DEFAULT_DOCUMENT_ID);
  client.destroy();
  core.disconnect(a.connection); core.disconnect(b.connection); core.disconnect(legacy.connection);
  room.doc.destroy(); legacy.connection.room.doc.destroy();
});

test('structure edits are revision-checked and same-role parallel conflicts are rejected', async () => {
  const core = new CollaborationCore({ store: new MultiMemoryStore() });
  const a = await connect(core, 'actor-a');
  const room = a.connection.room;
  const roleId = room.structure.roles[0].id;
  const stageId = room.structure.stages[0].id;
  await core.operation(a.connection, commandOperation(room, { type: 'action.add', stage_id: stageId, kind: 'wait', role_id: roleId, group_mode: 'after', description: '先等待' }));
  const firstGroup = room.structure.stages[0].actions[0].group_id;
  const before = room.revision;
  await assert.rejects(core.operation(a.connection, commandOperation(room, { type: 'action.add', stage_id: stageId, kind: 'wait', role_id: roleId, group_mode: 'parallel', description: '同组再次等待' })), /same_role_group_conflict/);
  assert.equal(room.structure.stages[0].actions[0].group_id, firstGroup);
  assert.equal(room.revision, before);

  const stale = commandOperation(room, { type: 'stage.add', after_stage_id: stageId }, room.revision - 1);
  await assert.rejects(core.operation(a.connection, stale), /revision_conflict/);
  assert.equal(room.revision, before);
  core.disconnect(a.connection); room.doc.destroy();
});

test('deleting an action retains a recovery copy and rejects its late text update', async () => {
  const core = new CollaborationCore({ store: new MultiMemoryStore() });
  const [a, b] = await Promise.all([connect(core, 'actor-a'), connect(core, 'actor-b')]);
  const room = a.connection.room;
  const stageId = room.structure.stages[0].id;
  const addOperation = commandOperation(room, { type: 'action.add', stage_id: stageId, kind: 'wait', role_id: room.structure.roles[0].id, group_mode: 'after', description: '动作说明保留在恢复副本' });
  await core.operation(a.connection, addOperation);
  const afterCreate = b.messages.filter((message) => message.type === 'operation.applied').at(-1);
  const action = afterCreate.structure.stages[0].actions[0];
  const field = actionDescriptionField(action.id);
  const staleClient = copyDoc(afterCreate.state);
  const staleUpdate = textOperation(staleClient, room.epoch, field, '晚到文字');

  await core.operation(a.connection, commandOperation(room, { type: 'action.delete', stage_id: stageId, action_id: action.id }));
  assert.equal(room.doc.getMap('fields').has(field), false);
  const savedRecovery = room.structure.recovery.find((entry) => entry.kind === 'action');
  assert.equal(savedRecovery.texts[field], '动作说明保留在恢复副本');
  const afterDelete = b.messages.filter((message) => message.type === 'operation.applied').at(-1);
  const liveClient = copyDoc(afterDelete.state);
  const activeTextOperation = textOperation(liveClient, room.epoch, roleNameField(room.structure.roles[0].id), ' 协作');
  await core.operation(b.connection, activeTextOperation);
  assert.equal(room.revision, 3, 'a live field edit may carry known historical tombstones');
  await assert.rejects(core.operation(b.connection, staleUpdate), /stale_text_field_update/);
  assert.equal(room.revision, 3);
  assert.equal(room.doc.getMap('fields').has(field), false);
  staleClient.destroy(); liveClient.destroy();
  core.disconnect(a.connection); core.disconnect(b.connection); room.doc.destroy();
});
