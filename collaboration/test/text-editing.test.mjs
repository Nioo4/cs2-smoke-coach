import { test } from 'node:test';
import assert from 'node:assert/strict';
import * as Y from 'yjs';
import { applyCompositionEdit, applyTextDiff, captureComposition } from '../src/text-editing.mjs';

function createDoc(value, name = 'text') {
  const doc = new Y.Doc();
  const text = doc.getText(name);
  if (value) text.insert(0, value);
  return { doc, text };
}

function remoteInsert(doc, index, value) {
  const remote = new Y.Doc();
  Y.applyUpdate(remote, Y.encodeStateAsUpdate(doc));
  const text = remote.getText('text');
  let update;
  remote.once('update', (next, origin) => { if (origin === 'remote-test') update = next; });
  remote.transact(() => text.insert(index, value), 'remote-test');
  Y.applyUpdate(doc, update, 'remote');
  remote.destroy();
}

function remoteDelete(doc, index, length) {
  const remote = new Y.Doc();
  Y.applyUpdate(remote, Y.encodeStateAsUpdate(doc));
  const text = remote.getText('text');
  let update;
  remote.once('update', (next, origin) => { if (origin === 'remote-test') update = next; });
  remote.transact(() => text.delete(index, length), 'remote-test');
  Y.applyUpdate(doc, update, 'remote');
  remote.destroy();
}

test('composition replacement deletes only its original characters around a concurrent insertion', () => {
  const { doc, text } = createDoc('abc');
  const composition = captureComposition(text);
  remoteInsert(doc, 1, '队友');

  const result = applyCompositionEdit(doc, text, composition, '甲', 1, 1);
  assert.equal(result.ok, true);
  assert.equal(result.value, '甲队友');
  const caret = Y.createAbsolutePositionFromRelativePosition(result.selection.start, doc);
  assert.equal(caret.index, 1);
  doc.destroy();
});

test('composition replacement preserves concurrent text inside the replaced base range', () => {
  const { doc, text } = createDoc('abcd');
  const composition = captureComposition(text);
  remoteInsert(doc, 2, '队友');

  const result = applyCompositionEdit(doc, text, composition, 'a甲d', 2, 2);
  assert.equal(result.ok, true);
  assert.equal(result.value, 'a甲队友d');
  doc.destroy();
});

test('composition deletion removes original base text without deleting concurrent insertion', () => {
  const { doc, text } = createDoc('abc');
  const composition = captureComposition(text);
  remoteInsert(doc, 1, '队友');

  const result = applyCompositionEdit(doc, text, composition, 'ac', 1, 1);
  assert.equal(result.ok, true);
  assert.equal(result.value, 'a队友c');
  doc.destroy();
});

test('composition skips base characters already deleted remotely', () => {
  const { doc, text } = createDoc('abc');
  const composition = captureComposition(text);
  remoteDelete(doc, 1, 1);

  const result = applyCompositionEdit(doc, text, composition, 'a甲c', 2, 2);
  assert.equal(result.ok, true);
  assert.equal(result.value, 'a甲c');
  doc.destroy();
});

test('composition end returns merged text and caret for the next ordinary input', () => {
  const { doc, text } = createDoc('abc');
  const composition = captureComposition(text);
  remoteInsert(doc, 1, '队友');

  const result = applyCompositionEdit(doc, text, composition, '甲', 1, 1);
  assert.equal(result.value, text.toString());
  const caret = Y.createAbsolutePositionFromRelativePosition(result.selection.start, doc);
  assert.equal(caret.index, 1);
  const nextValue = `${result.value.slice(0, caret.index)}新${result.value.slice(caret.index)}`;
  applyTextDiff(doc, text, nextValue);
  assert.equal(text.toString(), '甲新队友');
  doc.destroy();
});

test('unresolvable composition anchors report conflict without mutating text', () => {
  const first = createDoc('abc');
  const composition = captureComposition(first.text);
  const other = createDoc('xyz', 'other');
  const result = applyCompositionEdit(other.doc, other.text, composition, '甲', 1, 1);
  assert.deepEqual(result, { ok: false });
  assert.equal(other.text.toString(), 'xyz');
  first.doc.destroy();
  other.doc.destroy();
});
