import { test } from 'node:test';
import assert from 'node:assert/strict';
import * as Y from 'yjs';
import {
  actionDescriptionField, createStageInitialData, createStageYDoc, expectedStageTextFields,
  stageNameField, validateStageStructure, validateStageTextDoc,
} from '../src/stage-board-schema.mjs';

function copyDoc(source) {
  const doc = new Y.Doc();
  Y.applyUpdate(doc, Y.encodeStateAsUpdate(source));
  return doc;
}

test('dynamic document schema seeds a blank stage board with isolated Y.Text fields', () => {
  const data = createStageInitialData();
  const doc = createStageYDoc(data.fields);
  validateStageStructure(data.structure);
  validateStageTextDoc(doc, data.structure);
  assert.equal(data.structure.document_id, 'mirage-stage-prototype');
  assert.equal(data.structure.stages.length, 1);
  assert.equal(data.structure.stages[0].actions.length, 0);
  assert.equal(doc.getMap('fields').get(stageNameField(data.structure.stages[0].id)).toString(), '阶段 1');
  assert.deepEqual([...doc.getMap('fields').keys()].sort(), expectedStageTextFields(data.structure).sort());
  doc.destroy();
});

test('move paths start at the sequential stage position and reject same-role simultaneous actions', () => {
  const data = createStageInitialData();
  const role = data.structure.roles[0];
  const group = crypto.randomUUID();
  const first = { id: crypto.randomUUID(), group_id: group, kind: 'move', role_id: role.id, path: [{ x: 50, y: 50 }, { x: 60, y: 60 }] };
  const second = { id: crypto.randomUUID(), group_id: group, kind: 'wait', role_id: role.id };
  data.structure.stages[0].actions = [first, second];
  data.fields[actionDescriptionField(first.id)] = '移动说明';
  data.fields[actionDescriptionField(second.id)] = '等待说明';
  const doc = createStageYDoc(data.fields);
  assert.throws(() => validateStageStructure(data.structure), /same_role_group_conflict/);
  data.structure.stages[0].actions = [first];
  assert.doesNotThrow(() => validateStageStructure(data.structure));
  first.path[0] = { x: 49, y: 50 };
  assert.throws(() => validateStageStructure(data.structure), /move_start_mismatch/);
  doc.destroy();
});

test('Yjs round-trip keeps the dynamic schema and rejects identity replacement and attributed text', () => {
  const data = createStageInitialData();
  const action = { id: crypto.randomUUID(), group_id: crypto.randomUUID(), kind: 'wait', role_id: data.structure.roles[0].id };
  data.structure.stages[0].actions.push(action);
  data.fields[actionDescriptionField(action.id)] = '等候';
  const source = createStageYDoc(data.fields);
  const restored = copyDoc(source);
  validateStageTextDoc(restored, data.structure, source);

  const replacement = copyDoc(source);
  const root = replacement.getMap('fields');
  const key = actionDescriptionField(action.id);
  const content = root.get(key).toString();
  root.delete(key);
  const newText = new Y.Text();
  newText.insert(0, content);
  root.set(key, newText);
  assert.throws(() => validateStageTextDoc(replacement, data.structure, source), /text_field_identity_changed/);

  const formatted = copyDoc(source);
  formatted.getMap('fields').get(key).format(0, 1, { bold: true });
  assert.throws(() => validateStageTextDoc(formatted, data.structure, source), /text_must_be_plain/);
  source.destroy(); restored.destroy(); replacement.destroy(); formatted.destroy();
});

test('dynamic description limit counts Unicode code points and requires saved actions to have text', () => {
  const data = createStageInitialData();
  const action = { id: crypto.randomUUID(), group_id: crypto.randomUUID(), kind: 'wait', role_id: data.structure.roles[0].id };
  data.structure.stages[0].actions.push(action);
  const key = actionDescriptionField(action.id);
  data.fields[key] = '😀'.repeat(2001);
  const doc = createStageYDoc(data.fields);
  assert.throws(() => validateStageTextDoc(doc, data.structure), /invalid_text/);
  data.fields[key] = '   ';
  const blank = createStageYDoc(data.fields);
  assert.throws(() => validateStageTextDoc(blank, data.structure), /empty_action_description/);
  doc.destroy(); blank.destroy();
});
