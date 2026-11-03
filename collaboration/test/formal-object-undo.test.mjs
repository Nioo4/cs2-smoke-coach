import assert from 'node:assert/strict';
import { randomUUID } from 'node:crypto';
import test from 'node:test';
import * as Y from 'yjs';
import { FormalCollaborationCore } from '../src/formal-collaboration-core.mjs';
import { createFormalSeed, projectFormalDocument } from '../src/formal-document.mjs';
import { FormalError } from '../src/formal-http-store.mjs';

// Pure technical fixture: no HTTP/PG. Keeps an independent confirmed state and operation ledger.
async function fixture() {
  const id = randomUUID(); const epoch = randomUUID(); const operations = new Map(); const recoveries = [];
  let saved = { ...createFormalSeed({ document_id: id, title: '对象撤销纯测', side: 'T' }), document_id: id, epoch, revision: 0 };
  const store = {
    fail: null, deny: false, loseAck: false,
    async consume() { return { tactic_id: id, grant_id: randomUUID(), role: 'editor', actor: { id: randomUUID(), name: 'tester', color: '#315d57' } }; },
    async load() { return { ...structuredClone(saved), recoveries: recoveries.map(({ id, kind, entity_id }) => ({ id, kind, entity_id })) }; },
    async release() {},
    async readRecovery(_, recoveryId) { return structuredClone(recoveries.find((item) => item.id === recoveryId)); },
    async findOperation(_, __, operationId) { return operations.get(operationId); },
    async persist(_, context, body) {
      if (this.deny) throw new FormalError('USER_FORBIDDEN');
      if (this.fail) throw new FormalError(this.fail);
      if (body.expected_revision !== saved.revision) throw new FormalError('TACTIC_CONFLICT');
      const next = body.projection;
      for (const stage of saved.projection.structure.stages) {
        const remaining = next.structure.stages.find((item) => item.id === stage.id);
        const removed = remaining ? stage.actions.filter((action) => !remaining.actions.some((item) => item.id === action.id)) : [stage];
        for (const entity of removed) {
          if (recoveries.some((item) => item.entity_id === entity.id)) continue;
          const keys = remaining ? [`action.${entity.id}.description`] : [`stage.${entity.id}.name`, `stage.${entity.id}.description`, `stage.${entity.id}.condition`, ...entity.actions.map((action) => `action.${action.id}.description`)];
          recoveries.push({ id: randomUUID(), kind: remaining ? 'action' : 'stage', entity_id: entity.id,
            snapshot: remaining ? { stage_id: stage.id, action: structuredClone(entity) } : { stage: structuredClone(entity) },
            texts: Object.fromEntries(keys.map((key) => [key, saved.projection.fields[key]])) });
        }
      }
      saved = { ...body, document_id: id, epoch, revision: saved.revision + 1 };
      operations.set(body.operation_id, { actor_id: context.actor.id, fingerprint: body.fingerprint, type: body.type, revision: saved.revision });
      if (this.loseAck) { this.loseAck = false; throw new FormalError('COLLAB_UNAVAILABLE'); }
      return { revision: saved.revision, duplicate: false };
    },
  };
  const core = new FormalCollaborationCore({ store }); const sent = []; const remote = [];
  const own = await core.connect({ ticket: 'own', send: (message) => sent.push(message), close() {} });
  const other = await core.connect({ ticket: 'other', send: (message) => remote.push(message), close() {} });
  const projection = () => projectFormalDocument(own.room.doc);
  async function command(value, connection = own) {
    let objectId;
    const structure = projection().structure;
    if (value.type === 'position.set') objectId = `position:${value.stage_id}:${value.role_id}`;
    else if (value.type.startsWith('action.') && value.type !== 'action.add') objectId = `action:${value.action_id}`;
    let lease;
    if (objectId) {
      const version = value.type === 'position.set' ? structure.stages.find((stage) => stage.id === value.stage_id).initial_positions[value.role_id].version : structure.stages.flatMap((stage) => stage.actions).find((action) => action.id === value.action_id).version;
      const granted = core.lease(connection, 'acquire', { object_id: objectId, version });
      lease = { token: granted.token, version };
    }
    const operation = { type: 'stage.command', operation_id: randomUUID(), epoch, payload: { expected_revision: own.room.revision, command: { ...value, ...(lease ? { lease } : {}) } } };
    try { await core.operation(connection, operation); }
    finally { if (lease) core.lease(connection, 'release', { object_id: objectId, token: lease.token }); }
    return operation;
  }
  const undoOperation = () => ({ type: 'stage.command', operation_id: randomUUID(), epoch, payload: { expected_revision: own.room.revision, command: { type: 'object.undo', source_operation_id: own.undo.sourceId, recovery_id: own.undo.recoveryId } } });
  const undo = () => core.operation(own, undoOperation());
  async function text(key, value, connection = other) {
    const client = new Y.Doc(); Y.applyUpdate(client, Y.encodeStateAsUpdate(own.room.doc));
    const vector = Y.encodeStateVector(client); const field = client.getMap('fields').get(key);
    client.transact(() => { field.delete(0, field.length); field.insert(0, value); });
    const update = Buffer.from(Y.encodeStateAsUpdate(client, vector)).toString('base64'); client.destroy();
    await core.operation(connection, { type: 'text.update', operation_id: randomUUID(), epoch, payload: { update } });
  }
  const stageId = projection().structure.stages[0].id; const roleId = projection().structure.roles[0].id;
  await command({ type: 'role.add' }); const secondRoleId = projection().structure.roles[1].id;
  for (const [x, kind] of [[20, 'move'], [40, 'move'], [0, 'wait']]) await command({ type: 'action.add', stage_id: stageId, role_id: kind === 'wait' ? secondRoleId : roleId, kind, description: `动作${x}`, group_mode: 'after', ...(kind === 'move' ? { path: [{ x: 50, y: 50 }, { x, y: 30 }] } : {}) });
  await command({ type: 'stage.add', after_stage_id: stageId });
  return { core, own, other, store, sent, remote, projection, command, undo, undoOperation, text, stageId, roleId, recoveries };
}

function semantic(structure) {
  return JSON.parse(JSON.stringify(structure, (key, value) => key === 'version' ? undefined : value));
}

const cases = [
  ['role.add', (f) => ({ type: 'role.add' })],
  ['stage.add', (f) => ({ type: 'stage.add', after_stage_id: f.stageId })],
  ['stage.copy', (f) => ({ type: 'stage.copy', stage_id: f.stageId })],
  ['stage.reorder', (f) => ({ type: 'stage.reorder', stage_id: f.stageId, to_index: 1 })],
  ['stage.reinherit', (f) => ({ type: 'stage.reinherit', stage_id: f.projection().structure.stages[1].id })],
  ['position.set', (f) => ({ type: 'position.set', stage_id: f.stageId, role_id: f.roleId, position: { x: 60, y: 70 } })],
  ['action.add', (f) => ({ type: 'action.add', stage_id: f.stageId, role_id: f.roleId, kind: 'observe', description: '观察', group_mode: 'after', target: { x: 50, y: 70 } })],
  ['action.update', (f) => ({ type: 'action.update', stage_id: f.stageId, action_id: f.projection().structure.stages[0].actions[0].id, changes: { path: [{ x: 50, y: 50 }, { x: 25, y: 80 }] } })],
  ['action.group', (f) => ({ type: 'action.group', stage_id: f.stageId, action_id: f.projection().structure.stages[0].actions[2].id, group_index: 0 })],
  ['action.reorder', (f) => ({ type: 'action.reorder', stage_id: f.stageId, action_id: f.projection().structure.stages[0].actions[0].id, to_index: 2, group_mode: 'after' })],
  ['stage.lineups.set', (f) => ({ type: 'stage.lineups.set', stage_id: f.stageId, lineup_ids: [randomUUID()] })],
  ['tactic.side.set', () => ({ type: 'tactic.side.set', side: 'CT' })],
];
for (const [name, build] of cases) test(`object undo restores ${name} with monotonic versions and strict private state`, async () => {
  const f = await fixture();
  if (name === 'stage.reinherit') await f.command({ type: 'position.set', stage_id: f.projection().structure.stages[1].id, role_id: f.roleId, position: { x: 70, y: 70 } });
  const before = f.projection(); await f.command(build(f)); const after = f.projection();
  const marker = f.sent.findLast((message) => message.type === 'object.undo.state');
  assert.deepEqual(Object.keys(marker).sort(), ['type', 'undo']);
  assert.deepEqual(Object.keys(marker.undo).sort(), ['is_delete', 'recovery_id', 'source_operation_id']);
  assert.equal(f.remote.filter((message) => message.type === 'object.undo.state').every((message) => message.undo === null), true);
  await f.undo(); const result = f.projection(); assert.deepEqual(semantic(result.structure), semantic(before.structure));
  assert.deepEqual(result.fields, before.fields); assert.equal(f.own.undo, null);
  for (const stage of result.structure.stages) { const prior = after.structure.stages.find((item) => item.id === stage.id); if (prior) assert.ok(stage.version >= prior.version); }
});

for (const kind of ['action', 'stage']) test(`delete undo restores ${kind} as a new independent copy with order, text and groups`, async () => {
  const f = await fixture();
  // Group two different-role wait actions to exercise surviving group membership.
  if (kind === 'action') await f.command({ type: 'action.group', stage_id: f.stageId, action_id: f.projection().structure.stages[0].actions[2].id, group_index: 1 });
  const before = f.projection(); const deleted = kind === 'stage' ? f.stageId : before.structure.stages[0].actions[1].id;
  await f.command(kind === 'stage' ? { type: 'stage.delete', stage_id: deleted } : { type: 'action.delete', stage_id: f.stageId, action_id: deleted });
  assert.equal(f.own.undo.isDelete, true); await f.undo(); const restored = f.projection();
  if (kind === 'stage') {
    const fresh = restored.structure.stages[0]; assert.notEqual(fresh.id, deleted);
    assert.deepEqual(fresh.lineup_ids, before.structure.stages[0].lineup_ids);
    assert.equal(restored.fields[`stage.${fresh.id}.name`], before.fields[`stage.${deleted}.name`]);
    assert.equal(fresh.actions.length, 3); assert.ok(fresh.actions.every((action) => !before.structure.stages[0].actions.some((old) => old.id === action.id)));
  } else {
    const actions = restored.structure.stages[0].actions; assert.notEqual(actions[1].id, deleted);
    assert.equal(actions[1].group_id, actions[2].group_id);
    assert.equal(restored.fields[`action.${actions[1].id}.description`], before.fields[`action.${deleted}.description`]);
    assert.deepEqual(actions[1].path, before.structure.stages[0].actions[1].path);
  }
});

test('undo recovery.restore removes the newly restored copy but keeps its durable source', async () => {
  const f = await fixture(); const actionId = f.projection().structure.stages[0].actions[0].id;
  await f.command({ type: 'action.delete', stage_id: f.stageId, action_id: actionId });
  const before = f.projection(); const recovery = f.recoveries.find((item) => item.entity_id === actionId);
  await f.command({ type: 'recovery.restore', recovery_id: recovery.id, stage_id: f.stageId }); await f.undo();
  assert.deepEqual(semantic(f.projection().structure), semantic(before.structure)); assert.ok(f.recoveries.includes(recovery));
});

test('undo stage recovery removes only its new independent copy', async () => {
  const f = await fixture(); await f.command({ type: 'stage.delete', stage_id: f.stageId });
  const recovery = f.recoveries.find((item) => item.entity_id === f.stageId); const before = f.projection();
  await f.command({ type: 'recovery.restore', recovery_id: recovery.id, after_stage_id: null }); await f.undo();
  assert.deepEqual(semantic(f.projection().structure), semantic(before.structure)); assert.deepEqual(f.projection().fields, before.fields);
});

for (const kind of ['wait', 'observe', 'utility', 'text']) test(`deletion undo preserves ${kind} action attributes and pure text`, async () => {
  const f = await fixture(); const fields = kind === 'utility' ? { thrower_role_id: f.roleId, utility_type: 'flash', target: { x: 22, y: 33 } }
    : kind === 'text' ? { role_id: null, position: { x: 44, y: 55 } }
      : { role_id: f.roleId, ...(kind === 'observe' ? { target: { x: 22, y: 33 } } : {}) };
  await f.command({ type: 'action.add', kind, stage_id: f.stageId, description: '独立说明', group_mode: 'after', ...fields });
  const original = f.projection().structure.stages[0].actions.at(-1);
  await f.command({ type: 'action.delete', stage_id: f.stageId, action_id: original.id }); await f.undo();
  const restored = f.projection().structure.stages[0].actions.at(-1);
  assert.equal(restored.kind, kind); assert.notEqual(restored.id, original.id);
  for (const [key, value] of Object.entries(fields)) assert.deepEqual(restored[key], value);
  assert.equal(f.projection().fields[`action.${restored.id}.description`], '独立说明');
});

test('geometry undo preserves concurrent plain text while new-field undo rejects text edits including ABA', async () => {
  const f = await fixture(); await f.command({ type: 'position.set', stage_id: f.stageId, role_id: f.roleId, position: { x: 60, y: 70 } });
  const description = `action.${f.projection().structure.stages[0].actions[0].id}.description`;
  await f.text(description, '队友新文字'); await f.undo(); assert.equal(f.projection().fields[description], '队友新文字');
  await f.command({ type: 'role.add' }); const role = f.projection().structure.roles.at(-1); const key = `role.${role.id}.name`; const original = f.projection().fields[key];
  await f.text(key, '改过'); await f.text(key, original);
  const revision = f.own.room.revision; await assert.rejects(f.undo(), /TACTIC_CONFLICT/); assert.equal(f.own.room.revision, revision);
});

test('any later structure including unrelated ABA invalidates the old connection opportunity', async () => {
  const f = await fixture(); await f.command({ type: 'role.add' }); const operation = f.undoOperation(); const oldReceipt = f.own.undo;
  await f.command({ type: 'tactic.side.set', side: 'CT' }, f.other); await f.command({ type: 'tactic.side.set', side: 'T' }, f.other);
  operation.payload.expected_revision = f.own.room.revision;
  await assert.rejects(f.core.operation(f.own, operation), /TACTIC_CONFLICT/);
  assert.equal(f.own.undo, null); assert.equal(f.own.room.structureGeneration > 0, true);
  f.own.undo = oldReceipt; // Even an accidentally retained receipt must fail the generation fence.
  await assert.rejects(f.core.operation(f.own, { ...operation, operation_id: randomUUID() }), /TACTIC_CONFLICT/);
});

test('borrowed own leases survive; conflicting derived-object locks and partial temporary leases roll back', async () => {
  const f = await fixture(); await f.command({ type: 'position.set', stage_id: f.stageId, role_id: f.roleId, position: { x: 90, y: 80 } });
  const action = f.projection().structure.stages[0].actions[0]; const id = `action:${action.id}`;
  const foreign = f.core.lease(f.other, 'acquire', { object_id: id, version: action.version });
  await assert.rejects(f.undo(), /COLLAB_LOCKED/); assert.equal(f.own.room.leases.leases.get(id).connectionId, f.other.id);
  assert.ok([...f.own.room.leases.leases.values()].every((lease) => lease.connectionId === f.other.id));
  f.core.lease(f.other, 'release', { object_id: id, token: foreign.token });
  const own = f.core.lease(f.own, 'acquire', { object_id: id, version: action.version }); await f.undo();
  assert.equal(f.own.room.leases.leases.get(id).token, own.token);
});

test('partial short leases acquired before a later collision are all released without touching the foreign lock', async () => {
  const f = await fixture(); await f.command({ type: 'position.set', stage_id: f.stageId, role_id: f.roleId, position: { x: 90, y: 80 } });
  const id = `position:${f.stageId}:${f.roleId}`; const version = f.projection().structure.stages[0].initial_positions[f.roleId].version;
  const foreign = f.core.lease(f.other, 'acquire', { object_id: id, version });
  // action:* sorts before position:*, so undo acquires a temporary derived-action lease first.
  await assert.rejects(f.undo(), /COLLAB_LOCKED/);
  assert.deepEqual([...f.own.room.leases.leases.keys()], [id]); assert.equal(f.own.room.leases.leases.get(id).token, foreign.token);
});

test('within-group order changes increment every displaced action version and undo respects those leases', async () => {
  const f = await fixture(); const actions = f.projection().structure.stages[0].actions;
  await f.command({ type: 'action.group', stage_id: f.stageId, action_id: actions[2].id, group_index: 1 });
  const before = f.projection().structure.stages[0].actions;
  const untouchedId = `action:${before[2].id}`;
  const blocking = f.core.lease(f.other, 'acquire', { object_id: untouchedId, version: before[2].version });
  await assert.rejects(f.command({ type: 'action.reorder', stage_id: f.stageId, action_id: before[1].id, to_index: 2, group_mode: 'parallel' }), /COLLAB_LOCKED/);
  assert.deepEqual(f.projection().structure.stages[0].actions, before);
  assert.equal(f.own.room.leases.leases.get(untouchedId).token, blocking.token);
  f.core.lease(f.other, 'release', { object_id: untouchedId, token: blocking.token });
  await f.command({ type: 'action.reorder', stage_id: f.stageId, action_id: before[1].id, to_index: 2, group_mode: 'parallel' });
  const after = f.projection().structure.stages[0].actions;
  assert.equal(after[1].id, before[2].id); assert.equal(after[2].id, before[1].id);
  assert.equal(after[1].version, before[2].version + 1); assert.equal(after[2].version, before[1].version + 1);
  assert.equal(after[1].group_id, before[2].group_id); assert.equal(after[2].group_id, before[1].group_id);
  const objectId = `action:${after[1].id}`; const foreign = f.core.lease(f.other, 'acquire', { object_id: objectId, version: after[1].version });
  await assert.rejects(f.undo(), /COLLAB_LOCKED/);
  f.core.lease(f.other, 'release', { object_id: objectId, token: foreign.token }); await f.undo();
  const result = f.projection().structure.stages[0].actions;
  assert.deepEqual(result.map((action) => action.id), before.map((action) => action.id));
  assert.equal(result[1].version, after[2].version + 1); assert.equal(result[2].version, after[1].version + 1);
});

test('PG CAS rejection reloads authority and clears the stale chance without a success broadcast', async () => {
  const f = await fixture(); await f.command({ type: 'role.add' }); const count = f.sent.filter((message) => message.type === 'operation.applied').length;
  f.store.fail = 'TACTIC_CONFLICT'; await assert.rejects(f.undo(), /TACTIC_CONFLICT/);
  assert.equal(f.own.undo, null); assert.equal(f.sent.filter((message) => message.type === 'operation.applied').length, count);
  assert.equal(f.own.room.leases.leases.size, 0);
});

test('tampered recovery IDs cannot substitute another durable source', async () => {
  const f = await fixture(); await f.command({ type: 'stage.delete', stage_id: f.stageId });
  const operation = f.undoOperation(); operation.payload.command.recovery_id = randomUUID();
  await assert.rejects(f.core.operation(f.own, operation), /TACTIC_CONFLICT/);
  assert.equal(f.own.undo, null); // Conflict reload invalidates the chance rather than rebinding it.
});

test('DB failure, current authorization and CAS never falsely consume or broadcast; ACK retry is idempotent', async () => {
  const f = await fixture(); await f.command({ type: 'role.add' }); const operation = f.undoOperation(); const receipt = f.own.undo;
  const applied = () => f.sent.filter((message) => message.type === 'operation.applied').length;
  const count = applied(); f.store.fail = 'COLLAB_UNAVAILABLE'; await assert.rejects(f.core.operation(f.own, operation), /COLLAB_UNAVAILABLE/);
  assert.equal(f.own.undo, receipt); assert.equal(applied(), count); assert.equal(f.own.room.leases.leases.size, 0);
  f.store.fail = null; f.store.deny = true; await assert.rejects(f.core.operation(f.own, operation), /USER_FORBIDDEN/); assert.equal(applied(), count);
  f.store.deny = false; f.store.loseAck = true; await assert.rejects(f.core.operation(f.own, operation), /COLLAB_UNAVAILABLE/);
  await f.core.operation(f.own, operation); const ack = f.sent.findLast((message) => message.type === 'operation.ack');
  assert.equal(ack.duplicate, true); const revision = f.own.room.revision; await f.core.operation(f.own, operation); assert.equal(f.own.room.revision, revision);
  assert.equal(f.own.undo, null);
});

test('only the source connection can undo; strict envelope, epoch, readonly and reload lifecycle fail closed', async () => {
  const f = await fixture(); await f.command({ type: 'role.add' }); const operation = f.undoOperation();
  await assert.rejects(f.core.operation(f.other, operation), /TACTIC_CONFLICT/);
  await assert.rejects(f.core.operation(f.own, { ...operation, epoch: randomUUID() }), /COLLAB_STALE_EPOCH/);
  await assert.rejects(f.core.operation(f.own, { ...operation, operation_id: randomUUID(), payload: { ...operation.payload, command: { ...operation.payload.command, before: {} } } }), /VALIDATION_ERROR/);
  f.own.readOnly = true; await assert.rejects(f.core.operation(f.own, operation), /USER_FORBIDDEN/); f.own.readOnly = false;
  await f.core.reload(f.own.room, f.own.context); assert.equal(f.own.undo, null);
  await assert.rejects(f.core.operation(f.own, operation), /TACTIC_CONFLICT/);
});
