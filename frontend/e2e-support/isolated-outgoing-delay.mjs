import { Buffer } from 'node:buffer';

const WS_URL = 'ws://127.0.0.1:18220/ws/tactics';
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;
const MAX_BYTES = 2 * 1024 * 1024;
const installed = new WeakSet();
const size = raw => typeof raw === 'string' ? Buffer.byteLength(raw) : raw.byteLength;
function parse(raw) {
  if (size(raw) > MAX_BYTES) return null;
  try { return JSON.parse(typeof raw === 'string' ? raw : raw.toString('utf8')); } catch { return null; }
}

// Test transport only: delay an actual UI frame, never construct or edit a product operation.
export async function installIsolatedOutgoingDelay(page, { targetDocumentId, timeoutMs = 30_000 } = {}) {
  if (!UUID.test(targetDocumentId ?? '') || !Number.isInteger(timeoutMs) || timeoutMs < 15_000 || timeoutMs > 120_000) throw Error('DELAY_INVALID_SCOPE');
  if (installed.has(page)) throw Error('DELAY_ALREADY_INSTALLED_ON_PAGE');
  installed.add(page);
  const connections = new Set();
  let phase = 'idle', armedOnce = false, heldCount = 0, armed = null, held = null, timer = null, deadline = 0;
  function clear(next) {
    globalThis.clearTimeout(timer); timer = null; armed = null; held = null; deadline = 0; phase = next;
  }
  async function discardAndClose(next, connection = held?.connection ?? armed) {
    clear(next); // Erase bytes before any asynchronous close; they can no longer be released.
    if (!connection || connection.closed) return true;
    connection.closed = true; connections.delete(connection);
    const results = await Promise.allSettled([connection.route.close(), connection.server.close()]);
    if (results.some(result => result.status === 'rejected')) { phase = 'close_failed'; return false; }
    return true;
  }
  async function dispose() {
    const closed = await discardAndClose('disposed');
    connections.clear(); page.off('close', onPageClose);
    return closed;
  }
  function onPageClose() { void dispose(); }
  try {
    await page.routeWebSocket(WS_URL, route => {
      const server = route.connectToServer(); // Preserve original URL/Origin/cookies/subprotocols.
      if (phase === 'disposed' || connections.size >= 4) {
        if (phase !== 'disposed') void discardAndClose('connection_limit');
        return; // Both directions, including close, remain natively transparent.
      }
      const connection = { route, server, id: null, actor: null, document: null, epoch: null, closed: false };
      connections.add(connection);
      const closeFrom = (other, code, reason) => {
        if (connection.closed) return;
        connection.closed = true; connections.delete(connection);
        if (armed === connection || held?.connection === connection) clear('closed');
        void other.close({ ...(code === undefined ? {} : { code }), ...(reason === undefined ? {} : { reason }) }).catch(() => { phase = 'close_failed'; });
      };
      route.onClose((code, reason) => closeFrom(server, code, reason));
      server.onClose((code, reason) => closeFrom(route, code, reason));
      server.onMessage(raw => {
        if (connection.closed) return;
        route.send(raw); // Real deletion/conflict/sync/ACK still reaches the page unchanged.
        const message = phase === 'disposed' ? null : parse(raw);
        if (message?.type === 'auth.ok') {
          if (armed === connection || held?.connection === connection) { void discardAndClose('identity_changed', connection); return; }
          connection.id = UUID.test(message.connection_id ?? '') ? message.connection_id : null;
          connection.actor = UUID.test(message.actor_id ?? '') ? message.actor_id : null;
          connection.document = connection.epoch = null;
        }
        if (message?.type === 'sync') {
          if ((armed === connection || held?.connection === connection) && (message.document_id !== connection.document || message.epoch !== connection.epoch)) { void discardAndClose('identity_changed', connection); return; }
          connection.document = message.document_id; connection.epoch = message.epoch;
        }
      });
      route.onMessage(raw => {
        if (connection.closed) return;
        if ((armed === connection || held?.connection === connection) && Date.now() >= deadline) { void discardAndClose('expired', connection); return; }
        if (phase === 'armed' && armed === connection) {
          const message = parse(raw), operation = message?.operation;
          if (message?.type === 'operation' && operation?.type === 'text.update' && UUID.test(operation.operation_id ?? '') && operation.epoch === connection.epoch && typeof operation.payload?.update === 'string' && operation.payload.update.length > 0) {
            held = { raw, connection, metadata: { operationId: operation.operation_id, type: operation.type, connectionId: connection.id, documentId: targetDocumentId, epoch: connection.epoch, bytes: size(raw) } };
            heldCount++; phase = 'held'; return;
          }
        }
        server.send(raw); // Other frames, including auth, presence and stage.command, are never held.
      });
    });
  } catch {
    installed.delete(page); throw Error('DELAY_INSTALL_FAILED');
  }
  page.on('close', onPageClose);
  return Object.freeze({
    arm() {
      const candidates = [...connections].filter(connection => !connection.closed && connection.document === targetDocumentId && connection.id && connection.actor && UUID.test(connection.epoch ?? ''));
      if (armedOnce || phase === 'disposed' || candidates.length !== 1) throw Error('DELAY_ARM_UNAVAILABLE');
      armedOnce = true; armed = candidates[0]; phase = 'armed'; deadline = Date.now() + timeoutMs;
      timer = globalThis.setTimeout(() => { void discardAndClose('expired'); }, timeoutMs); timer.unref?.();
    },
    status() { return { targetDocumentId, phase, armedOnce, heldCount, connections: connections.size, held: held ? { ...held.metadata } : null }; },
    async releaseHeld() {
      if (!held) return false;
      const original = held, connection = original.connection;
      if (Date.now() >= deadline || connection.closed || connection.id !== original.metadata.connectionId || connection.document !== targetDocumentId || connection.epoch !== original.metadata.epoch) {
        await discardAndClose('release_refused', connection); return false;
      }
      clear('released');
      try { connection.server.send(original.raw); return true; }
      catch { await discardAndClose('release_failed', connection); return false; }
    },
    dropHeldAndClose() { return discardAndClose('dropped'); },
    dispose,
  });
}
