import * as Y from 'yjs';

export const LOCAL_TEXT_ORIGIN = 'local-ui';

export function bindLocalTextHistory(textTypes, onChange = () => {}) {
  if (!textTypes.length) return null;
  const manager = new Y.UndoManager(textTypes, {
    trackedOrigins: new Set([LOCAL_TEXT_ORIGIN]),
  });
  for (const event of ['stack-item-added', 'stack-item-popped', 'stack-item-updated', 'stack-cleared']) {
    manager.on(event, onChange);
  }
  return manager;
}

export function isPersistedTextUpdateOrigin(origin, manager) {
  return origin === LOCAL_TEXT_ORIGIN || Boolean(manager && origin === manager);
}
