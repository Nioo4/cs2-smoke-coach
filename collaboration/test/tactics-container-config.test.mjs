import assert from 'node:assert/strict';
import { readFile, stat } from 'node:fs/promises';
import test from 'node:test';
const root = new URL('../../', import.meta.url);
const read = path => readFile(new URL(path, root), 'utf8');

test('isolated compose exposes only loopback nginx and fixed new storage identities', async () => {
  const compose = await read('compose.tactics.local.yml');
  assert.ok(compose.startsWith('name: smokeline-tactics-t5-local\n'));
  assert.equal((compose.match(/^    ports:/gm) ?? []).length, 1);
  assert.ok(compose.includes('"127.0.0.1:18220:8080"'));
  assert.ok(!compose.includes('env_file:') && !compose.includes('${'));
  assert.ok(compose.includes('internal: true'));
  for (const suffix of ['postgres', 'media', 'backups', 'spool']) assert.ok(compose.includes(`name: smokeline-tactics-t5-local-${suffix}`));
  assert.ok(compose.includes('APP_ENV: test') && compose.includes('TACTICS_ENABLED: "true"'));
  assert.equal((compose.match(/PUBLIC_BASE_URL: http:\/\/127\.0\.0\.1:18220/g) ?? []).length, 2);
  assert.ok(compose.includes('TEMP_ROOT: /data/tactics-spool'));
  assert.ok(!compose.includes('size=256m'));
  assert.ok(compose.includes('chmod 0700 /spool') && compose.includes('chown 10001:10001 /spool'));
  assert.ok(compose.includes('find /spool -mindepth 1 -maxdepth 1'));
  assert.ok(!compose.includes('privileged:') && !compose.includes('network_mode: host'));
  assert.equal((await stat(new URL('deploy/tactics.empty.env', root))).size, 0);
});

test('local nginx keeps every original teaching/media safety line and adds exact internal and WS rules', async () => {
  const original = (await read('deploy/nginx.conf')).replaceAll('\r', '').split('\n').filter(Boolean);
  const next = (await read('deploy/nginx.tactics.local.conf')).replaceAll('\r', '').split('\n');
  let index = 0;
  for (const line of original) {
    const found = next.indexOf(line, index);
    assert.ok(found >= 0, 'base nginx changed: synchronize the local config safety copy');
    index = found + 1;
  }
  const config = next.join('\n');
  for (const rule of ['location = /api/v1/internal { return 404; }', 'location ^~ /api/v1/internal/ { return 404; }', 'location = /ws/tactics {', 'proxy_set_header Upgrade $http_upgrade;', 'proxy_set_header Origin $http_origin;', 'proxy_read_timeout 75s;', 'proxy_buffering off;', 'proxy_request_buffering off;']) assert.ok(config.includes(rule));
  for (const path of ['register', 'login', 'recover']) assert.ok(config.includes(`location = /api/v1/auth/${path} {`));
  assert.ok(config.indexOf('location ^~ /api/v1/internal/') < config.indexOf('location ^~ /api/v1/ {'));
});

test('container entry is separate, bounded and imports no prototype ticket issuer', async () => {
  const entry = await read('collaboration/scripts/formal-container-server.mjs');
  const original = await read('collaboration/scripts/formal-seed-server.mjs');
  assert.ok(original.includes("host !== '127.0.0.1'"));
  assert.ok(entry.includes("TACTICS_CONTAINER_MODE !== 'local-isolated'"));
  assert.ok(entry.includes("request.url === '/healthz'"));
  assert.ok(entry.includes("server.listen(port, '0.0.0.0'"));
  assert.ok(entry.includes('server.requestTimeout = 5_000') && entry.includes('server.headersTimeout = 5_000'));
  assert.ok(entry.includes('socket.ping()') && entry.includes('20_000'));
  assert.ok(entry.includes("process.on('SIGTERM', shutdown)"));
  assert.ok(!entry.includes('/__t0/') && !entry.includes('test-tickets'));
  const dockerfile = await read('collaboration/Dockerfile');
  assert.ok(dockerfile.includes('npm ci --omit=dev --ignore-scripts'));
  assert.ok(dockerfile.includes('USER node'));
  assert.ok(!dockerfile.includes('COPY . '));
  const frontend = await read('deploy/tactics.frontend.Dockerfile');
  const nodeImage = 'node:24.22.2-alpine3.23@sha256:9b69af715c7f9d038150b54ccccbaeffa9064bbc93e2904c29591ca097316af1';
  assert.ok(dockerfile.startsWith(`FROM ${nodeImage}\n`));
  assert.ok(frontend.startsWith(`FROM ${nodeImage} AS build\n`));
  for (const module of ['stage-playback.mjs', 'stage-board-interactions.mjs', 'text-editing.mjs', 'text-history.mjs']) assert.ok(frontend.includes(`collaboration/src/${module}`));
  assert.ok(frontend.includes('USER 10002:10002'));
  assert.ok(!frontend.includes('stage-board.js'));
  const ignored = await read('.dockerignore');
  for (const excluded of ['admin.txt', '**/admin.txt', 'mirage_map/', 'label-map/']) assert.ok(ignored.split(/\r?\n/).includes(excluded));
});
