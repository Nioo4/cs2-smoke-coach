import { randomUUID } from 'node:crypto';
import * as Y from 'yjs';
import WebSocket from 'ws';

const socket = new WebSocket(process.env.FORMAL_TEST_WS, { origin: process.env.FORMAL_TEST_ORIGIN });
const timeout = setTimeout(() => { process.exitCode = 1; socket.terminate(); }, 30000);
let operationId;
socket.on('open', () => socket.send(JSON.stringify({ type: 'auth', ticket: process.env.FORMAL_TEST_TICKET })));
socket.on('message', (raw) => {
  const message = JSON.parse(raw.toString());
  if (message.type === 'sync' && !operationId) {
    if (process.env.FORMAL_HISTORY_LINEUPS) {
      operationId = randomUUID();
      socket.send(JSON.stringify({ type: 'operation', operation: { type: 'stage.command', operation_id: operationId, epoch: message.epoch, payload: { expected_revision: message.revision, command: { type: 'stage.lineups.set', stage_id: message.projection.structure.stages[0].id, lineup_ids: JSON.parse(process.env.FORMAL_HISTORY_LINEUPS) } } } }));
      return;
    }
    const doc = new Y.Doc(); Y.applyUpdate(doc, Buffer.from(message.state, 'base64')); const vector = Y.encodeStateVector(doc);
    doc.getMap('fields').get('tactic.purpose').insert(0, '活跃会话真实修改'); operationId = randomUUID();
    socket.send(JSON.stringify({ type: 'operation', operation: { type: 'text.update', operation_id: operationId, epoch: message.epoch, payload: { update: Buffer.from(Y.encodeStateAsUpdate(doc, vector)).toString('base64') } } })); doc.destroy();
  } else if (message.type === 'operation.applied' && message.operation_id === operationId) process.stdout.write('ACTIVE_CHANGED\n');
  else if (message.type === 'operation.rejected' || message.type === 'protocol.error') { process.exitCode = 1; socket.terminate(); }
});
socket.on('close', () => clearTimeout(timeout));
socket.on('error', () => { clearTimeout(timeout); process.exitCode = 1; });
