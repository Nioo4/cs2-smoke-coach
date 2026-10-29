import { test } from 'node:test';
import assert from 'node:assert/strict';
import * as Y from 'yjs';
import { applyConfirmedUpdate, createConfirmedDocument, encodeConfirmedDocument } from '../src/confirmed-document.mjs';

function cloneDoc(encodedState) {
  const doc = new Y.Doc();
  Y.applyUpdate(doc, encodedState);
  return doc;
}

function localInsert(doc, field, value) {
  const before = Y.encodeStateVector(doc);
  const text = doc.getMap('fields').get(field);
  doc.transact(() => text.insert(text.length, value), 'local-editor');
  return Y.encodeStateAsUpdate(doc, before);
}

test('one acknowledged local text operation does not confirm another still-pending edit', () => {
  const initial = new Y.Doc();
  const fields = initial.getMap('fields');
  fields.set('role-name', new Y.Text());
  fields.set('stage-description', new Y.Text());
  const initialState = Y.encodeStateAsUpdate(initial);
  const editor = cloneDoc(initialState);
  const confirmed = createConfirmedDocument(initialState);

  const accepted = localInsert(editor, 'role-name', '已确认');
  applyConfirmedUpdate(confirmed, accepted);
  const pending = localInsert(editor, 'stage-description', '尚待确认');

  assert.ok(pending.length > 0);
  assert.equal(editor.getMap('fields').get('stage-description').toString(), '尚待确认');
  assert.equal(confirmed.getMap('fields').get('role-name').toString(), '已确认');
  assert.equal(confirmed.getMap('fields').get('stage-description').toString(), '');

  const discardedDraft = cloneDoc(encodeConfirmedDocument(confirmed));
  assert.equal(discardedDraft.getMap('fields').get('role-name').toString(), '已确认');
  assert.equal(discardedDraft.getMap('fields').get('stage-description').toString(), '');

  discardedDraft.destroy(); confirmed.destroy(); editor.destroy(); initial.destroy();
});
