import { randomUUID } from 'node:crypto';
import { WebSocketServer } from 'ws';
import { FormalCollaborationCore } from './formal-collaboration-core.mjs';
import { FormalHttpStore } from './formal-http-store.mjs';
import { FORMAL_UUID } from './formal-document.mjs';

export function attachFormalWebSockets(server, { backendUrl, internalKey, publicBaseUrl }) {
  const base = new URL(publicBaseUrl);
  if (!['http:', 'https:'].includes(base.protocol) || base.username || base.password || base.search || base.hash || base.pathname !== '/') throw new Error('invalid_formal_public_origin');
  const core = new FormalCollaborationCore({ store: new FormalHttpStore({ baseUrl: backendUrl, internalKey }) });
  const wss = new WebSocketServer({ noServer: true, maxPayload: 2 * 1024 * 1024, perMessageDeflate: false });
  server.on('upgrade', (request, socket, head) => {
    if (request.url !== '/ws/tactics' || request.headers.origin !== base.origin) { socket.write('HTTP/1.1 403 Forbidden\r\nConnection: close\r\n\r\n'); socket.destroy(); return; }
    wss.handleUpgrade(request, socket, head, (ws) => wss.emit('connection', ws));
  });
  wss.on('connection', (socket) => {
    let connection = null; let authenticating = false; let closed = false; let pending = 0;
    let windowStart = Date.now(); let messages = 0;
    const send = (message) => { if (socket.readyState === socket.OPEN) socket.send(JSON.stringify(message)); };
    const authTimer = setTimeout(() => socket.close(1008, 'auth_timeout'), 5_000);
    authTimer.unref?.();
    socket.on('message', (raw) => {
      if (Date.now() - windowStart > 1000) { windowStart = Date.now(); messages = 0; }
      if (++messages > 50) { socket.close(1008, 'message_limit'); return; }
      let message;
      try { message = JSON.parse(raw.toString()); } catch { send({ type: 'protocol.error', code: 'INVALID_JSON' }); return; }
      const tracked = !connection || message?.type === 'operation';
      if (tracked && pending >= 16) { socket.close(1008, 'message_limit'); return; }
      if (tracked) pending += 1;
      void (async () => {
        if (!connection) {
          if (authenticating || !message || Object.keys(message).sort().join(',') !== 'ticket,type' || message.type !== 'auth' || typeof message.ticket !== 'string') throw new Error('USER_AUTH_REQUIRED');
          authenticating = true;
          try {
            connection = await core.connect({ ticket: message.ticket, connectionId: randomUUID(), send, close: (code, reason) => socket.close(code, reason) });
            if (closed || socket.readyState !== socket.OPEN) core.disconnect(connection);
            clearTimeout(authTimer);
          } finally { authenticating = false; }
          return;
        }
        if (message.type === 'operation' && Object.keys(message).sort().join(',') === 'operation,type') await core.operation(connection, message.operation);
        else if (message.type === 'presence' && Object.keys(message).sort().join(',') === 'payload,type') core.presence(connection, message.payload);
        else if (message.type === 'map.preview' && Object.keys(message).sort().join(',') === 'payload,type') core.preview(connection, message.payload);
        else if (['lease.acquire', 'lease.renew', 'lease.release'].includes(message.type) && Object.keys(message).sort().join(',') === 'payload,request_id,type' && FORMAL_UUID.test(message.request_id)) {
          const kind = message.type.split('.')[1]; const result = core.lease(connection, kind, message.payload);
          send({ type: kind === 'acquire' ? 'lease.granted' : kind === 'renew' ? 'lease.renewed' : 'lease.released', request_id: message.request_id, object_id: result.objectId,
            ...(result.token ? { token: result.token } : {}), ...(result.expiresAt ? { expires_at: result.expiresAt, renew_every_ms: result.renewEveryMs } : {}), ...(kind === 'release' ? { released: result.released } : {}) });
        } else throw new Error('VALIDATION_ERROR');
      })().catch((error) => {
        const rawCode = error?.code ?? error?.message;
        const code = typeof rawCode === 'string' && /^[A-Z][A-Z_]+$/.test(rawCode) ? rawCode : rawCode === 'stale_text_field_update' ? 'COLLAB_STALE_FIELD' : 'VALIDATION_ERROR';
        if (message?.type === 'operation') {
          send({ type: 'operation.rejected', operation_id: message.operation?.operation_id ?? null, code, details: error.details ?? {} });
          if (connection && !error.authoritativeSyncSent && !['USER_AUTH_REQUIRED', 'USER_FORBIDDEN', 'TACTIC_NOT_FOUND', 'ENDPOINT_NOT_FOUND'].includes(code)) { send(core.sync(connection.room)); core.sendUndoState(connection); }
        } else send({ type: 'protocol.error', request_id: message?.request_id ?? null, code, details: error.details ?? {} });
        if (!connection || ['USER_AUTH_REQUIRED', 'USER_FORBIDDEN', 'TACTIC_NOT_FOUND', 'ENDPOINT_NOT_FOUND'].includes(code)) socket.close(1008, 'authorization_invalid');
      }).finally(() => { if (tracked) pending -= 1; });
    });
    socket.on('close', () => { closed = true; clearTimeout(authTimer); core.disconnect(connection); });
    socket.on('error', () => socket.close(1011, 'socket_failed'));
  });
  let checking = false;
  const timer = setInterval(() => { core.sweepLocks(); if (!checking) { checking = true; void core.checkConnections().finally(() => { checking = false; }); } }, 1000);
  timer.unref?.();
  server.on('close', () => { clearInterval(timer); for (const socket of wss.clients) socket.terminate(); wss.close(); });
  return { core, wss };
}
