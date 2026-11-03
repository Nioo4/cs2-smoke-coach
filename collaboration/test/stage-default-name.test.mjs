import assert from 'node:assert/strict';
import { randomUUID } from 'node:crypto';
import test from 'node:test';
import * as Y from 'yjs';
import { buildStageCommand } from '../src/stage-commands.mjs';
import { createStageInitialData, createStageYDoc, stageNameField } from '../src/stage-board-schema.mjs';
import { createFormalSeed, projectFormalDocument } from '../src/formal-document.mjs';
import { applyFormalCommand } from '../src/formal-commands.mjs';

test('formal repeated insertion after one stage increments default names without moving or renaming existing stages', () => {
  const seed = createFormalSeed({ document_id: randomUUID(), title: '默认阶段名称', side: 'T' });
  const doc = new Y.Doc(); Y.applyUpdate(doc, Buffer.from(seed.state_base64, 'base64'));
  const first = projectFormalDocument(doc).structure.stages[0];
  function add() {
    const candidate = new Y.Doc(); Y.applyUpdate(candidate, Y.encodeStateAsUpdate(doc));
    applyFormalCommand(doc, candidate, { type: 'stage.add', after_stage_id: first.id });
    Y.applyUpdate(doc, Y.encodeStateAsUpdate(candidate)); candidate.destroy();
  }
  add(); const secondId = projectFormalDocument(doc).structure.stages[1].id;
  add(); const result = projectFormalDocument(doc);
  assert.deepEqual(result.structure.stages.map(stage => result.fields[stageNameField(stage.id)]), ['阶段 1', '阶段 3', '阶段 2']);
  assert.equal(result.structure.stages[2].id, secondId);
  assert.equal(result.fields[stageNameField(first.id)], '阶段 1');
  assert.deepEqual(result.structure.stages[1].initial_positions, first.initial_positions);
  const custom = doc.getMap('fields').get(stageNameField(first.id)); custom.delete(0, custom.length); custom.insert(0, '自定义开局');
  const renamed = doc.getMap('fields').get(stageNameField(secondId)); renamed.delete(0, renamed.length); renamed.insert(0, '阶段 8');
  add(); const next = projectFormalDocument(doc);
  assert.equal(next.fields[stageNameField(next.structure.stages[1].id)], '阶段 9');
  assert.equal(next.fields[stageNameField(first.id)], '自定义开局');
  assert.equal(next.fields[stageNameField(secondId)], '阶段 8');
  doc.destroy();
});

test('prototype uses the same default-name rule while retaining custom names', () => {
  const initial = createStageInitialData(); const doc = createStageYDoc(initial.fields);
  const first = initial.structure.stages[0];
  const current = doc.getMap('fields').get(stageNameField(first.id)); current.delete(0, current.length); current.insert(0, '自定义开局');
  const once = buildStageCommand(initial.structure, { type: 'stage.add', after_stage_id: first.id }, doc);
  const added = once.structure.stages[1];
  assert.equal(once.fieldSources[stageNameField(added.id)].value, '阶段 1');
  const name = new Y.Text(); name.insert(0, '阶段 7'); doc.getMap('fields').set(stageNameField(added.id), name);
  const next = buildStageCommand(once.structure, { type: 'stage.add', after_stage_id: first.id }, doc);
  assert.equal(next.fieldSources[stageNameField(next.structure.stages[1].id)].value, '阶段 8');
  assert.equal(doc.getMap('fields').get(stageNameField(first.id)).toString(), '自定义开局');
  assert.deepEqual(next.structure.stages[1].initial_positions, first.initial_positions);
  doc.destroy();
});
