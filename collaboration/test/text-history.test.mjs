import { test } from 'node:test';
import assert from 'node:assert/strict';
import * as Y from 'yjs';
import { bindLocalTextHistory, isPersistedTextUpdateOrigin, LOCAL_TEXT_ORIGIN } from '../src/text-history.mjs';

test('local text undo preserves remote edits and undo/redo updates remain persistable', () => {
  const local = new Y.Doc();
  const localText = local.getText('shared');
  localText.insert(0, 'base:');
  const manager = bindLocalTextHistory([localText]);
  const persistedOrigins = [];
  local.on('update', (_update, origin) => {
    if (isPersistedTextUpdateOrigin(origin, manager)) persistedOrigins.push(origin);
  });

  local.transact(() => localText.insert(localText.length, 'A-local'), LOCAL_TEXT_ORIGIN);
  assert.equal(manager.undoStack.length, 1);

  const remote = new Y.Doc();
  Y.applyUpdate(remote, Y.encodeStateAsUpdate(local));
  const remoteText = remote.getText('shared');
  let remoteUpdate;
  remote.on('update', (update, origin) => { if (origin === 'remote-test') remoteUpdate = update; });
  remote.transact(() => remoteText.insert(remoteText.length, 'B-remote'), 'remote-test');
  Y.applyUpdate(local, remoteUpdate, 'server');
  assert.equal(manager.undoStack.length, 1, 'remote update must not add a local undo item');
  assert.equal(isPersistedTextUpdateOrigin('server', manager), false);

  manager.undo();
  assert.equal(localText.toString().includes('A-local'), false);
  assert.equal(localText.toString().includes('B-remote'), true);
  assert.equal(persistedOrigins.at(-1), manager, 'undo transaction should use the manager origin and be persisted');

  manager.redo();
  assert.equal(localText.toString().includes('A-local'), true);
  assert.equal(localText.toString().includes('B-remote'), true);
  assert.equal(persistedOrigins.at(-1), manager, 'redo transaction should use the manager origin and be persisted');
  assert.equal(persistedOrigins.filter((origin) => origin === LOCAL_TEXT_ORIGIN).length, 1);

  manager.destroy();
  remote.destroy();
  local.destroy();
});
