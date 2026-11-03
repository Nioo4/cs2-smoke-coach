import { test } from 'node:test';
import assert from 'node:assert/strict';
import { randomUUID } from 'node:crypto';
import * as Y from 'yjs';
import { copyFormalProjection, createFormalSeed, projectFormalDocument, sha256 } from '../src/formal-document.mjs';
import { createFormalSeedServer } from '../src/formal-seed-server.mjs';

test('formal seed stores structure and plain text in one Y.Doc, independent of the prototype', () => {
  const id = randomUUID(); const seed = createFormalSeed({ document_id: id, title: '个人战术', side: 'CT' });
  const doc = new Y.Doc(); Y.applyUpdate(doc, Buffer.from(seed.state_base64, 'base64'));
  assert.deepEqual([...doc.share.keys()].sort(), ['document', 'fields']);
  assert.deepEqual(projectFormalDocument(doc), seed.projection);
  assert.equal(seed.projection.structure.document_id, id);
  assert.equal(seed.projection.structure.schema_version, 2);
  assert.equal(sha256(seed.projection_json), seed.projection_sha256);
  doc.getMap('fields').get('tactic.title').format(0, 1, { bold: true });
  assert.throws(() => projectFormalDocument(doc), /plain/); doc.destroy();
  assert.throws(() => createFormalSeed({ document_id: id, title: '', side: 'T' }), /invalid_seed/);
  assert.throws(() => createFormalSeed({ document_id: id, title: 'x', side: 'T', owner: 'forged' }), /invalid_seed/);
});

test('seed HTTP requires the internal key and accepts only the formal seed contract', async () => {
  const key = 'local-formal-seed-key-'.repeat(2);
  const server = createFormalSeedServer({ internalKey: key });
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
  const url = `http://127.0.0.1:${server.address().port}`;
  try {
    const options = { method: 'POST', headers: { 'content-type': 'application/json', 'internal-key': key }, body: JSON.stringify({ document_id: randomUUID(), title: '中文种子', side: 'T' }) };
    assert.equal((await fetch(`${url}/internal/seed-stage-document`, { ...options, headers: {} })).status, 401);
    assert.equal((await fetch(`${url}/__t0/test-tickets`, options)).status, 404);
    const result = await fetch(`${url}/internal/seed-stage-document`, options);
    assert.equal(result.status, 200); assert.equal((await result.json()).projection.fields['tactic.title'], '中文种子');
  } finally { await new Promise((resolve) => server.close(resolve)); }
});

test('whole-tactic copying remaps roles/stages/actions/groups and preserves null-role text and lineups', () => {
  const source = createFormalSeed({ document_id: randomUUID(), title: '复制原文', side: 'T' }).projection;
  const stage = source.structure.stages[0]; const actionId = randomUUID();
  stage.lineup_ids = [randomUUID()];
  stage.actions.push({ id: actionId, group_id: randomUUID(), role_id: null, version: 3, kind: 'text', position: { x: 30, y: 40 } });
  source.fields[`action.${actionId}.description`] = '中文😀原文';
  const copied = copyFormalProjection({ document_id: randomUUID(), projection: source });
  const next = copied.projection.structure.stages[0];
  assert.notEqual(next.id, stage.id); assert.notEqual(next.actions[0].id, actionId);
  assert.notEqual(next.actions[0].group_id, stage.actions[0].group_id);
  assert.notEqual(copied.projection.structure.roles[0].id, source.structure.roles[0].id);
  assert.equal(next.actions[0].role_id, null); assert.equal(next.actions[0].version, 1);
  assert.deepEqual(next.lineup_ids, stage.lineup_ids);
  assert.equal(copied.projection.fields[`action.${next.actions[0].id}.description`], '中文😀原文');
});
