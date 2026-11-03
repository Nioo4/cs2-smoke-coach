import { createServer } from 'node:http';
import { timingSafeEqual } from 'node:crypto';
import { copyFormalProjection, createFormalSeed, sha256 } from './formal-document.mjs';

export function createFormalSeedServer({ internalKey }) {
  if (typeof internalKey !== 'string' || Buffer.byteLength(internalKey) < 32) throw new Error('formal_internal_key_required');
  return createServer((request, response) => {
    const reply = (status, value) => { response.writeHead(status, { 'content-type': 'application/json', 'cache-control': 'no-store' }); response.end(JSON.stringify(value)); };
    void (async () => {
      const supplied = request.headers['internal-key'];
      if (typeof supplied !== 'string' || !timingSafeEqual(Buffer.from(sha256(supplied), 'hex'), Buffer.from(sha256(internalKey), 'hex'))) return reply(401, { error: 'internal_auth_required' });
      const copy = request.url === '/internal/copy-stage-document';
      if (request.method !== 'POST' || (!copy && request.url !== '/internal/seed-stage-document')) return reply(404, { error: 'not_found' });
      if (request.headers['content-type']?.split(';')[0] !== 'application/json') return reply(415, { error: 'json_required' });
      const chunks = []; let size = 0;
      for await (const chunk of request) { size += chunk.length; if (size > (copy ? 3 * 1024 * 1024 : 8192)) return reply(413, { error: 'request_too_large' }); chunks.push(chunk); }
      try { reply(200, (copy ? copyFormalProjection : createFormalSeed)(JSON.parse(Buffer.concat(chunks).toString('utf8')))); }
      catch { reply(422, { error: 'invalid_seed_request' }); }
    })().catch(() => { if (!response.headersSent) reply(500, { error: 'seed_unavailable' }); else response.destroy(); });
  });
}
