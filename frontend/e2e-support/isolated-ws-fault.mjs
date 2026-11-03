import { Buffer } from 'node:buffer';

const WS_URL = 'ws://127.0.0.1:18220/ws/tactics';
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;
const MAX_BYTES = 8 * 1024 * 1024;
const installed = new WeakSet();
const size = raw => typeof raw === 'string' ? Buffer.byteLength(raw) : raw.byteLength;
function packet(raw) {
  if (size(raw) > MAX_BYTES) return null;
  try { return JSON.parse(typeof raw === 'string' ? raw : raw.toString('utf8')); } catch { return null; }
}

// Standalone test transport only. No Playwright config, credentials, application hooks or synthetic frames.
export async function installIsolatedWsFault(page, { targetDocumentId, timeoutMs = 30_000 } = {}) {
  if (!UUID.test(targetDocumentId ?? '') || !Number.isInteger(timeoutMs) || timeoutMs < 15_000 || timeoutMs > 120_000) throw Error('FAULT_INVALID_SCOPE');
  if (installed.has(page)) throw Error('FAULT_ALREADY_INSTALLED_ON_PAGE');
  const connections = new Set();
  let phase = 'idle', armedOnce = false, heldCount = 0, armed = null, held = null, timer = null;
  function restore(next = 'passthrough') {
    globalThis.clearTimeout(timer); timer = null; held = null; armed = null; phase = next;
    for (const connection of connections) connection.operations.clear();
  }
  function dispose() {
    restore('disposed');
    for (const connection of connections) { connection.operations.clear(); connection.actor = connection.id = connection.document = connection.epoch = null; }
    connections.clear(); page.off('close', dispose);
    // Playwright has no unrouteWebSocket here; installed handlers remain transparent until this dedicated page closes.
  }
  await page.routeWebSocket(WS_URL, route => {
    const server = route.connectToServer(); // Original URL, Origin, cookies and subprotocols; no handshake overrides.
    if (phase === 'disposed' || connections.size >= 4) {
      if (phase !== 'disposed') restore('connection_limit');
      return; // Leave native bidirectional forwarding/close forwarding intact.
    }
    const connection = { route, server, id: null, actor: null, document: null, epoch: null, operations: new Map(), closed: false };
    connections.add(connection);
    const closeFrom = (other, code, reason) => {
      if (connection.closed) return;
      connection.closed = true; connections.delete(connection);
      if (armed === connection || held?.connection === connection) restore('closed');
      connection.operations.clear();
      void other.close({ ...(code === undefined ? {} : { code }), ...(reason === undefined ? {} : { reason }) }).catch(() => { if (phase !== 'disposed') restore('close_failed'); });
    };
    route.onClose((code, reason) => closeFrom(server, code, reason));
    server.onClose((code, reason) => closeFrom(route, code, reason));
    route.onMessage(raw => {
      if (connection.closed) return;
      if (phase === 'armed' && armed === connection) {
        const message = packet(raw), operation = message?.operation;
        if (message?.type === 'operation' && UUID.test(operation?.operation_id ?? '') && operation.epoch === connection.epoch && ['text.update', 'stage.command'].includes(operation.type)) {
          if (connection.operations.size >= 32 && !connection.operations.has(operation.operation_id)) restore('operation_limit');
          else connection.operations.set(operation.operation_id, { epoch: operation.epoch, type: operation.type });
        }
      }
      server.send(raw); // Never withhold or change a client operation/auth/presence frame.
    });
    server.onMessage(raw => {
      if (connection.closed) return;
      const message = phase === 'disposed' ? null : packet(raw);
      if (message?.type === 'auth.ok') {
        if (armed === connection) restore('identity_changed');
        connection.id = UUID.test(message.connection_id ?? '') ? message.connection_id : null;
        connection.actor = UUID.test(message.actor_id ?? '') ? message.actor_id : null;
        connection.document = connection.epoch = null; connection.operations.clear();
      }
      if (message?.type === 'sync') {
        if (armed === connection && (message.document_id !== connection.document || message.epoch !== connection.epoch)) restore('identity_changed');
        connection.document = message.document_id; connection.epoch = message.epoch;
      }
      const operation = connection.operations.get(message?.operation_id);
      const matching = phase === 'armed' && armed === connection && connection.document === targetDocumentId && connection.id && connection.actor && operation && message.document_id === targetDocumentId && message.epoch === operation.epoch && Number.isSafeInteger(message.revision) && message.revision >= 0 && (
        (message.type === 'operation.applied' && message.connection_id === connection.id && message.actor_id === connection.actor && message.operation_type === operation.type) ||
        (message.type === 'operation.ack' && message.duplicate === true && Number.isSafeInteger(message.current_revision) && message.current_revision >= message.revision)
      );
      if (matching) {
        held = { raw, connection, metadata: { type: message.type, operationId: message.operation_id, connectionId: connection.id, revision: message.revision, bytes: size(raw) } };
        heldCount++; phase = 'held'; connection.operations.clear();
        return;
      }
      if (phase === 'armed' && armed === connection && size(raw) > MAX_BYTES) restore('frame_limit');
      route.send(raw);
    });
  });
  installed.add(page); page.on('close', dispose);
  return Object.freeze({
    arm() {
      const candidates = [...connections].filter(connection => !connection.closed && connection.document === targetDocumentId && connection.id && connection.actor && UUID.test(connection.epoch ?? ''));
      if (armedOnce || phase === 'disposed' || candidates.length !== 1) throw Error('FAULT_ARM_UNAVAILABLE');
      armedOnce = true; armed = candidates[0]; phase = 'armed';
      timer = globalThis.setTimeout(() => restore('expired'), timeoutMs); timer.unref?.();
    },
    status() { return { targetDocumentId, phase, armedOnce, heldCount, connections: connections.size, held: held ? { ...held.metadata } : null }; },
    releaseHeld() {
      if (!held) return false;
      const original = held; restore();
      if (!original.connection.closed) original.connection.route.send(original.raw);
      return true;
    },
    async dropHeldAndRestore({ closeConnection = false } = {}) {
      const connection = held?.connection; restore();
      if (closeConnection && connection && !connection.closed) {
        connection.closed = true; connections.delete(connection);
        const results = await Promise.allSettled([connection.route.close(), connection.server.close()]);
        if (results.some(result => result.status === 'rejected')) { phase = 'close_failed'; throw Error('FAULT_CLOSE_FAILED'); }
      }
    },
    dispose,
  });
}
