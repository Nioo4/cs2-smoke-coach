import { test } from 'node:test';
import assert from 'node:assert/strict';
import { randomUUID } from 'node:crypto';
import * as Y from 'yjs';
import { applyFormalCommand } from '../src/formal-commands.mjs';
import { createFormalSeed, projectFormalDocument } from '../src/formal-document.mjs';
import { assertStageTextUpdateScope } from '../src/collaboration-core.mjs';
import { FormalCollaborationCore } from '../src/formal-collaboration-core.mjs';
import { LeaseManager } from '../src/leases.mjs';

function seeded() {
  const seed = createFormalSeed({ document_id: randomUUID(), title: '正式测试', side: 'T' });
  const doc = new Y.Doc(); Y.applyUpdate(doc, Buffer.from(seed.state_base64, 'base64')); return doc;
}

test('deleted action leases release safely and idempotently only for their original holder', () => {
  const doc = seeded(); const leases = new LeaseManager();
  const objectId = `action:${randomUUID()}`; // Target is no longer in the confirmed document.
  const grant = leases.acquire({ objectId, version: 1, currentVersion: 1,
    connectionId: 'owner', actor: { name: 'owner', color: 'blue' } });
  const room = { doc, leases, clients: new Map() };
  const core = new FormalCollaborationCore({ store: {} });
  const payload = { object_id: objectId, token: grant.token };
  const owner = { id: 'owner', room }; const other = { id: 'other', room };
  assert.equal(core.lease(other, 'release', payload).released, false);
  assert.equal(core.lease(owner, 'release', { ...payload, token: randomUUID() }).released, false);
  assert.throws(() => core.lease(owner, 'release', { ...payload, token: '' }), /VALIDATION_ERROR/);
  assert.equal(core.lease(owner, 'release', payload).released, true);
  assert.equal(core.lease(owner, 'release', payload).released, false);
  assert.throws(() => core.lease(owner, 'acquire', { object_id: objectId, version: 1 }), /VALIDATION_ERROR/);
  assert.throws(() => core.lease(owner, 'renew', payload), /VALIDATION_ERROR/);
  doc.destroy();
});

test('formal copy and stage recovery retain teaching IDs, fresh identities and pure text', () => {
  const doc = seeded(); const source = doc.getMap('document').get('structure');
  const lineup = randomUUID(); source.stages[0].lineup_ids = [lineup];
  doc.getMap('document').set('structure', structuredClone(source));
  const candidate = new Y.Doc(); Y.applyUpdate(candidate, Y.encodeStateAsUpdate(doc));
  applyFormalCommand(doc, candidate, { type: 'stage.copy', stage_id: source.stages[0].id });
  const copied = projectFormalDocument(candidate).structure.stages[1];
  assert.deepEqual(copied.lineup_ids, [lineup]); assert.notEqual(copied.id, source.stages[0].id);
  const recovery = { id: randomUUID(), kind: 'stage', snapshot: { stage: source.stages[0] }, texts: Object.fromEntries([...doc.getMap('fields')].map(([key, text]) => [key, text.toString()])) };
  const restored = new Y.Doc(); Y.applyUpdate(restored, Y.encodeStateAsUpdate(doc));
  applyFormalCommand(doc, restored, { type: 'recovery.restore', recovery_id: recovery.id, after_stage_id: source.stages[0].id }, recovery);
  assert.deepEqual(projectFormalDocument(restored).structure.stages[1].lineup_ids, [lineup]);
  doc.destroy(); candidate.destroy(); restored.destroy();
});

test('raw text updates cannot mutate the protected v2 document root even if semantic contents are restored', () => {
  const doc = seeded(); const client = new Y.Doc(); Y.applyUpdate(client, Y.encodeStateAsUpdate(doc));
  const before = Y.encodeStateVector(client);
  const structure = structuredClone(client.getMap('document').get('structure'));
  client.getMap('document').set('structure', { ...structure, side: 'CT' });
  client.getMap('document').set('structure', structure);
  const update = Y.encodeStateAsUpdate(client, before);
  const candidate = new Y.Doc(); Y.applyUpdate(candidate, Y.encodeStateAsUpdate(doc)); Y.applyUpdate(candidate, update);
  const fields = [...doc.getMap('fields').keys()];
  assert.throws(() => assertStageTextUpdateScope(candidate, doc, update, null, fields), /stale_text_field_update/);
  candidate.destroy(); client.destroy(); doc.destroy();
});

test('lineup assignment is ordered, strict and increments only stage metadata version', () => {
  const doc = seeded(); const source = projectFormalDocument(doc).structure;
  const stage = source.stages[0]; const ids = [randomUUID(), randomUUID()];
  const candidate = new Y.Doc(); Y.applyUpdate(candidate, Y.encodeStateAsUpdate(doc));
  const next = applyFormalCommand(doc, candidate, { type: 'stage.lineups.set', stage_id: stage.id, lineup_ids: ids });
  assert.deepEqual(next.stages[0].lineup_ids, ids);
  assert.equal(next.stages[0].version, stage.version + 1);
  assert.deepEqual(next.stages[0].initial_positions, stage.initial_positions);
  assert.deepEqual(next.stages[0].actions, stage.actions);
  for (const lineupIds of [[ids[0], ids[0]], Array.from({ length: 11 }, randomUUID), ['not-a-uuid']]) {
    assert.throws(() => applyFormalCommand(doc, candidate, { type: 'stage.lineups.set', stage_id: stage.id, lineup_ids: lineupIds }), /invalid_lineup_command/);
  }
  assert.throws(() => applyFormalCommand(doc, candidate, { type: 'stage.lineups.set', stage_id: stage.id, lineup_ids: [], arbitrary: true }), /invalid_lineup_command/);
  doc.destroy(); candidate.destroy();
});
