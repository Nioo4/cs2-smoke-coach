import { test } from 'node:test';
import assert from 'node:assert/strict';
import { PostgresStore } from '../src/postgres-store.mjs';

test('postgres pool bounds connection and query waits and handles idle-client errors', async () => {
  const store = new PostgresStore('postgresql://t0-test:unused@127.0.0.1:1/t0_test', {
    connectionTimeoutMillis: 100,
    queryTimeoutMillis: 200,
    statementTimeoutMillis: 200,
  });
  try {
    assert.equal(store.pool.options.connectionTimeoutMillis, 100);
    assert.equal(store.pool.options.query_timeout, 200);
    assert.equal(store.pool.options.statement_timeout, 200);
    assert.equal(store.pool.listenerCount('error'), 1);

    store.pool.emit('error', Object.assign(new Error('connection reset'), { code: 'ECONNRESET' }));
    assert.equal(store.poolErrorCount, 1);
    assert.equal(store.lastPoolError.code, 'ECONNRESET');
    assert.equal('message' in store.lastPoolError, false);

    const startedAt = Date.now();
    await assert.rejects(store.initialize());
    assert.ok(Date.now() - startedAt < 2_000, 'connection failure should be bounded');
  } finally {
    await store.close();
  }
});
