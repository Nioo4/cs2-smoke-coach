import * as Y from 'yjs';

export function createConfirmedDocument(encodedState) {
  const doc = new Y.Doc();
  Y.applyUpdate(doc, encodedState, 'server-confirmed');
  return doc;
}

export function replaceConfirmedDocument(current, encodedState) {
  current?.destroy();
  return createConfirmedDocument(encodedState);
}

export function applyConfirmedUpdate(doc, update) {
  if (!(doc instanceof Y.Doc)) throw new TypeError('confirmed_document_required');
  Y.applyUpdate(doc, update, 'server-confirmed');
}

export function encodeConfirmedDocument(doc) {
  if (!(doc instanceof Y.Doc)) throw new TypeError('confirmed_document_required');
  return Y.encodeStateAsUpdate(doc);
}
