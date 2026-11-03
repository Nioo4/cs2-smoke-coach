import { createFormalSeedServer } from '../src/formal-seed-server.mjs';
import { attachFormalWebSockets } from '../src/formal-websocket-server.mjs';

const host = process.env.TACTICS_FORMAL_HOST ?? '127.0.0.1';
if (host !== '127.0.0.1') throw new Error('this development entry must remain loopback-only');
const server = createFormalSeedServer({ internalKey: process.env.COLLAB_INTERNAL_KEY });
if (process.env.COLLAB_BACKEND_URL) attachFormalWebSockets(server, {
  backendUrl: process.env.COLLAB_BACKEND_URL, internalKey: process.env.COLLAB_INTERNAL_KEY,
  publicBaseUrl: process.env.PUBLIC_BASE_URL,
});
server.requestTimeout = 5_000;
server.headersTimeout = 5_000;
server.listen(Number(process.env.TACTICS_FORMAL_PORT ?? 4186), host, () => {
  process.stdout.write(`FORMAL_SEED_LISTENING ${JSON.stringify(server.address())}\n`);
});
