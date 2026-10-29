import { test } from 'node:test';
import assert from 'node:assert/strict';
import * as Y from 'yjs';
import { createInitialData, createYDoc, validateStructure, validateTextDoc } from '../src/prototype-schema.mjs';

function restore(source) {
  const doc = new Y.Doc();
  Y.applyUpdate(doc, Y.encodeStateAsUpdate(source));
  return doc;
}

test('Yjs state round-trip restores fields and preserves text type identity', () => {
  const data = createInitialData();
  const original = createYDoc(data.fields);
  validateStructure(data.structure);
  validateTextDoc(original, data.structure);
  const restored = restore(original);
  assert.equal(restored.share.get('fields').constructor.name, 'AbstractType');
  validateTextDoc(restored, data.structure, original);
  assert.equal(restored.getMap('fields').get('step-1.role-a').toString(), data.fields['step-1.role-a']);
  original.destroy();
  restored.destroy();
});

test('unknown top-level shared types and same-name Y.Text replacements are rejected', () => {
  const data = createInitialData();
  const base = createYDoc(data.fields);
  const unknownRoot = restore(base);
  unknownRoot.getMap('attacker-root').set('value', 'hidden');
  assert.throws(() => validateTextDoc(unknownRoot, data.structure, base), /unexpected_shared_type/);

  const replacement = restore(base);
  const key = 'step-1.role-a';
  const value = replacement.getMap('fields').get(key).toString();
  replacement.getMap('fields').delete(key);
  const newType = new Y.Text();
  newType.insert(0, value);
  replacement.getMap('fields').set(key, newType);
  assert.throws(() => validateTextDoc(replacement, data.structure, base), /text_field_identity_changed/);
  base.destroy();
  unknownRoot.destroy();
  replacement.destroy();
});

test('out-of-order dependency update is rejected until its prerequisite arrives', () => {
  const data = createInitialData();
  const base = createYDoc(data.fields);
  const writer = restore(base);
  const text = writer.getMap('fields').get('step-1.role-a');
  const before = Y.encodeStateVector(writer);
  text.insert(text.length, '先');
  const prerequisite = Y.encodeStateAsUpdate(writer, before);
  const afterFirst = Y.encodeStateVector(writer);
  text.insert(text.length, '后');
  const dependent = Y.encodeStateAsUpdate(writer, afterFirst);
  const candidate = restore(base);
  Y.applyUpdate(candidate, dependent);
  assert.ok(candidate.store.pendingStructs, 'Yjs should retain the unresolved dependent struct');
  assert.throws(() => validateTextDoc(candidate, data.structure, base), /unresolved_yjs_dependencies/);
  Y.applyUpdate(candidate, prerequisite);
  assert.equal(candidate.store.pendingStructs, null);
  assert.equal(candidate.getMap('fields').get('step-1.role-a').toString(), `${data.fields['step-1.role-a']}先后`);
  validateTextDoc(candidate, data.structure, base);
  base.destroy();
  writer.destroy();
  candidate.destroy();
});

test('every step independently enforces its 100-object limit and text counts Unicode code points', () => {
  const data = createInitialData();
  const many = structuredClone(data.structure);
  for (let index = 0; index < 96; index += 1) many.steps[0].objects.push({ id: `note-${index}`, kind: 'note', label: '注释', x: index % 101, y: index % 101, version: 1 });
  assert.doesNotThrow(() => validateStructure(many));
  many.steps[0].objects.push({ id: 'note-overflow', kind: 'note', label: '注释', x: 50, y: 50, version: 1 });
  assert.throws(() => validateStructure(many), /invalid_objects/);

  const doc = createYDoc(data.fields);
  const text = doc.getMap('fields').get('step-1.role-a');
  doc.transact(() => { text.delete(0, text.length); text.insert(0, '😀'.repeat(2001)); });
  assert.throws(() => validateTextDoc(doc, data.structure), /invalid_text/);
  doc.destroy();
});
