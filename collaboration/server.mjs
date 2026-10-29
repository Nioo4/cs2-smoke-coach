import { createServer } from 'node:http';
import { readFile, stat } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { randomBytes, randomUUID } from 'node:crypto';
import { WebSocketServer } from 'ws';
import { CollaborationCore } from './src/collaboration-core.mjs';
import { PostgresStore } from './src/postgres-store.mjs';
import { leaseGrantMessage, leaseRenewedMessage } from './src/client-protocol.mjs';
import { authenticateSocket, closeSocketAuth, createSocketAuthState } from './src/socket-auth.mjs';
import { decodeRequestPath } from './src/http-path.mjs';
import { readTicketDocumentId } from './src/ticket-body.mjs';

const here = path.dirname(fileURLToPath(import.meta.url));
const host = process.env.TACTICS_T0_HOST ?? '127.0.0.1';
const port = Number(process.env.TACTICS_T0_PORT ?? 4173);
if (process.env.TACTICS_T0_MODE !== 'local' || host !== '127.0.0.1' || process.env.NODE_ENV === 'production') {
  throw new Error('T0 prototype must run in explicit local mode bound to 127.0.0.1');
}
if (!process.env.DATABASE_URL) throw new Error('DATABASE_URL is required for the isolated T0 database');

const store = new PostgresStore(process.env.DATABASE_URL);
await store.initialize();
const core = new CollaborationCore({ store, authorize: async (connection) => connection.actor.role === 'editor' });
await core.room();

const tickets = new Map();
const actors = [
  { id: 'test-a', name: '参与者 A', color: '#cb5a42', role: 'editor' },
  { id: 'test-b', name: '参与者 B', color: '#4b8b82', role: 'editor' },
];

const server = createServer((request, response) => {
  void handleRequest(request, response).catch(() => {
    if (!response.headersSent) respond(response, 500, 'request_failed');
    else response.destroy();
  });
});

async function handleRequest(request, response) {
  const remote = request.socket.remoteAddress;
  const local = remote === '127.0.0.1' || remote === '::1' || remote === '::ffff:127.0.0.1';
  if (!local) return respond(response, 403, 'loopback_only');
  let url;
  try { url = new URL(request.url ?? '/', `http://${request.headers.host ?? '127.0.0.1'}`); }
  catch { return respond(response, 400, 'invalid_url'); }
  if (request.method === 'POST' && url.pathname === '/__t0/test-tickets') {
    if (request.headers.origin !== `http://${host}:${port}`) return respond(response, 403, 'same_origin_required');
    let documentId;
    try {
      request.setTimeout(3_000, () => request.destroy(new Error('ticket_request_timeout')));
      documentId = await readTicketDocumentId(request);
    } catch (error) {
      return respond(response, error?.message === 'ticket_request_too_large' ? 413 : 400, error?.message === 'unknown_document' ? 'unknown_document' : 'invalid_ticket_request');
    } finally {
      request.setTimeout(0);
    }
    const issued = actors.map((actor) => {
      const token = randomBytes(32).toString('base64url');
      tickets.set(token, { actor, documentId, expiresAt: Date.now() + 5 * 60_000 });
      return { actor_id: actor.id, actor: actor.name, token };
    });
    response.writeHead(201, { 'content-type': 'application/json; charset=utf-8', 'cache-control': 'no-store' });
    return response.end(JSON.stringify({ expires_in_seconds: 300, participants: issued }));
  }
  if (request.method === 'GET' && url.pathname === '/__t0/health') {
    response.writeHead(200, { 'content-type': 'application/json; charset=utf-8', 'cache-control': 'no-store' });
    return response.end(JSON.stringify({ status: 'local-only', database: 'isolated-postgresql-16', prototype: true }));
  }
  if (request.method !== 'GET') return respond(response, 405, 'method_not_allowed');
  const requested = url.pathname === '/' ? '/index.html' : decodeRequestPath(url.pathname);
  if (requested === null) return respond(response, 400, 'invalid_path');
  const target = path.resolve(here, 'dist', `.${requested}`);
  if (!target.startsWith(`${path.resolve(here, 'dist')}${path.sep}`)) return respond(response, 404, 'not_found');
  try {
    const info = await stat(target);
    if (!info.isFile()) return respond(response, 404, 'not_found');
    const body = await readFile(target);
    const type = target.endsWith('.html') ? 'text/html; charset=utf-8' : target.endsWith('.js') ? 'text/javascript; charset=utf-8' : target.endsWith('.css') ? 'text/css; charset=utf-8' : target.endsWith('.svg') ? 'image/svg+xml' : 'application/octet-stream';
    response.writeHead(200, { 'content-type': type, 'cache-control': 'no-store', 'x-content-type-options': 'nosniff' });
    response.end(body);
  } catch {
    respond(response, 404, 'not_found');
  }
}

const wss = new WebSocketServer({ noServer: true, maxPayload: 2 * 1024 * 1024, perMessageDeflate: false });
server.on('upgrade', (request, socket, head) => {
  const origin = request.headers.origin;
  if (request.url !== '/collab' || origin !== `http://${host}:${port}` || !['127.0.0.1', '::1', '::ffff:127.0.0.1'].includes(request.socket.remoteAddress)) {
    socket.write('HTTP/1.1 403 Forbidden\r\nConnection: close\r\n\r\n');
    socket.destroy();
    return;
  }
  wss.handleUpgrade(request, socket, head, (websocket) => wss.emit('connection', websocket, request));
});

wss.on('connection', (socket) => {
  const connectionId = randomUUID();
  const authState = createSocketAuthState();
  const authTimer = setTimeout(() => socket.close(4401, 'auth_timeout'), 5_000);
  const send = (message) => { if (socket.readyState === socket.OPEN) socket.send(JSON.stringify(message)); };
  socket.on('message', async (raw) => {
    let message;
    try { message = JSON.parse(raw.toString('utf8')); } catch { socket.close(4400, 'invalid_json'); return; }
    if (!authState.connection) {
      if (authState.closed || authState.authenticating) { socket.close(4401, 'auth_in_progress'); return; }
      if (message?.type !== 'auth' || typeof message.ticket !== 'string' || Object.keys(message).some((key) => !['type', 'ticket'].includes(key))) { socket.close(4401, 'auth_required'); return; }
      const ticket = tickets.get(message.ticket);
      tickets.delete(message.ticket);
      if (!ticket || ticket.expiresAt < Date.now()) { socket.close(4401, 'invalid_ticket'); return; }
      clearTimeout(authTimer);
      try {
        const connection = await authenticateSocket({ core, socket, state: authState, connectionId, actor: ticket.actor, documentId: ticket.documentId, send });
        if (!connection) return;
        send({ type: 'auth.ok', actor_id: ticket.actor.id, document_id: ticket.documentId });
      } catch {
        if (!authState.closed) socket.close(1011, 'room_unavailable');
      }
      return;
    }
    const connection = authState.connection;
    try {
      if (message?.type === 'presence') {
        await core.presence(connection, message.payload);
      } else if (message?.type === 'lease.acquire') {
        const lease = core.acquireLease(connection, message.payload);
        send(leaseGrantMessage(message.request_id, lease));
      } else if (message?.type === 'lease.renew') {
        const lease = core.renewLease(connection, message.payload);
        send(leaseRenewedMessage(message.request_id, lease));
      } else if (message?.type === 'lease.release') {
        core.releaseLease(connection, message.payload);
      } else if (message?.type === 'map.preview') {
        core.preview(connection, message.payload);
      } else if (message?.type === 'operation') {
        await core.operation(connection, message.operation);
      } else {
        throw Object.assign(new Error('unsupported_message'), { code: 'unsupported_message' });
      }
    } catch (error) {
      if (message?.type === 'operation') core.reject(connection, message.operation, error);
      else send({ type: 'protocol.error', request_id: message?.request_id ?? null, code: error?.code ?? 'request_failed' });
    }
  });
  socket.on('close', () => { clearTimeout(authTimer); closeSocketAuth(core, authState); });
  socket.on('error', () => { clearTimeout(authTimer); closeSocketAuth(core, authState); });
});

function respond(response, status, body) {
  response.writeHead(status, { 'content-type': 'text/plain; charset=utf-8', 'cache-control': 'no-store' });
  response.end(body);
}

server.listen(port, host, () => process.stdout.write(`TACTICS_T0_READY http://${host}:${port}/ pid=${process.pid}\n`));
async function shutdown() {
  for (const socket of wss.clients) socket.close(1001, 'server_shutdown');
  server.close();
  await store.close();
  process.exit(0);
}
process.on('SIGINT', shutdown);
process.on('SIGTERM', shutdown);
