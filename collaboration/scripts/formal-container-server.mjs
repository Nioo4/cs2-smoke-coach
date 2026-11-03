import { createFormalSeedServer } from '../src/formal-seed-server.mjs';
import { attachFormalWebSockets } from '../src/formal-websocket-server.mjs';

// Separate from the loopback-only development entry. No test-ticket issuer is imported.
if (process.env.TACTICS_CONTAINER_MODE !== 'local-isolated') throw new Error('isolated_container_mode_required');
const port = Number(process.env.TACTICS_FORMAL_PORT ?? 4186);
if (!Number.isSafeInteger(port) || port < 1024 || port > 65535) throw new Error('invalid_formal_port');
const server = createFormalSeedServer({ internalKey: process.env.COLLAB_INTERNAL_KEY });
const [internalHandler] = server.listeners('request');
server.removeAllListeners('request');
server.on('request', (request, response) => {
  if (request.method === 'GET' && request.url === '/healthz') {
    response.writeHead(200, { 'content-type': 'application/json', 'cache-control': 'no-store' });
    response.end('{"status":"ok","service":"tactics-collaboration"}');
    return;
  }
  internalHandler(request, response);
});
const { wss } = attachFormalWebSockets(server, {
  backendUrl: process.env.COLLAB_BACKEND_URL,
  internalKey: process.env.COLLAB_INTERNAL_KEY,
  publicBaseUrl: process.env.PUBLIC_BASE_URL,
});
server.requestTimeout = 5_000;
server.headersTimeout = 5_000;
server.keepAliveTimeout = 5_000;
server.maxHeadersCount = 64;
const alive = new WeakMap();
wss.on('connection', socket => {
  alive.set(socket, true);
  socket.on('pong', () => alive.set(socket, true));
});
const heartbeat = setInterval(() => {
  for (const socket of wss.clients) {
    if (!alive.get(socket)) socket.terminate();
    else { alive.set(socket, false); socket.ping(); }
  }
}, 20_000);
heartbeat.unref();
let stopping = false;
function shutdown() {
  if (stopping) return;
  stopping = true; clearInterval(heartbeat);
  for (const socket of wss.clients) socket.close(1001, 'service_shutdown');
  server.close(() => process.exit(0));
  server.closeIdleConnections();
  setTimeout(() => {
    for (const socket of wss.clients) socket.terminate();
    server.closeAllConnections();
    process.exit(1);
  }, 7_000).unref();
}
process.on('SIGTERM', shutdown);
process.on('SIGINT', shutdown);
server.listen(port, '0.0.0.0', () => process.stdout.write('FORMAL_CONTAINER_LISTENING\n'));
