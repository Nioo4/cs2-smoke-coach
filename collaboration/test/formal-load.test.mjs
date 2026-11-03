import assert from 'node:assert/strict';
import test from 'node:test';
import { fileURLToPath } from 'node:url';
import { options, PROJECT, ORIGIN, LoadFailure, safeCode, createHttpScheduler, quantiles, fingerprint, validateSearch, validateConfirmation, completionKind, verifyConvergence } from '../scripts/formal-load-support.mjs';

const root = fileURLToPath(new URL('../../', import.meta.url));
const approved = ['--execute', '--providers-disabled', '--project', PROJECT, '--output', '.workcache/t5-load-test.json'];
test('load writes require exact runtime, explicit local project, providers-disabled and new bounded output', () => {
  assert.equal(options([], root, '24.15.0').execute, false);
  assert.equal(options(approved, root, '24.22.2').origin, ORIGIN);
  assert.throws(() => options(approved, root, '24.15.0'), /PORTABLE_NODE/);
  assert.throws(() => options(approved.filter(arg => arg !== '--providers-disabled'), root, '24.22.2'), /PROVIDERS_DISABLED/);
  assert.throws(() => options(approved.map(arg => arg === PROJECT ? 'old-shares-project' : arg), root, '24.22.2'), /ISOLATED_PROJECT/);
  assert.throws(() => options(approved.map(arg => arg.endsWith('.json') ? '../outside.json' : arg), root, '24.22.2'), /OUTPUT_BOUNDARY/);
  assert.throws(() => options([...approved, '--origin', 'http://localhost:4195'], root, '24.22.2'), /INVALID_ARGUMENT/);
  assert.throws(() => options([...approved, '--execute'], root, '24.22.2'), /DUPLICATE/);
});
test('short selftest never satisfies actual 1800-second completion', () => {
  assert.equal(completionKind(10, 1900), 'SHORT_SELFTEST_PASS');
  assert.equal(completionKind(1800, 1799.99), 'SHORT_SELFTEST_PASS');
  assert.equal(completionKind(1800, 1800), 'FULL_30_MIN_PROTOCOL_PASS');
  for (const seconds of ['0', 'NaN', '1801', '5.5']) assert.throws(() => options(['--duration-seconds', seconds], root, '24.22.2'), /INVALID_DURATION/);
});
test('search preflight accepts only actual curated routes for selected target and known public IDs', () => {
  const target = { slug: 'vip' }; const ids = new Set(['known']);
  const result = { type: 'route_matches', source: 'local_curated', intent: { target_slug: 'vip' }, routes: [{ target, lineup: { id: 'known' } }] };
  validateSearch(result, target, ids);
  validateSearch({ type: 'match', source: 'local_curated', intent: { target_slug: 'vip' }, lineup: { id: 'known', target } }, target, ids);
  assert.throws(() => validateSearch({ ...result, source: 'ai' }, target, ids), /SEARCH_NOT_LOCAL/);
  assert.throws(() => validateSearch({ ...result, type: 'clarification' }, target, ids), /SEARCH_ROUTES/);
  assert.throws(() => validateSearch({ ...result, routes: [{ target: { slug: 'other' }, lineup: { id: 'known' } }] }, target, ids), /SEARCH_ROUTES/);
  assert.throws(() => validateSearch(result, target, new Set()), /SEARCH_ROUTES/);
});
test('semantic convergence verifies persisted revision, epoch and all clients rather than binary encoding', () => {
  const saved = { revision: 12, epoch: 'epoch', projection: { fields: { b: 'two', a: 'one' }, structure: { stages: [] } } };
  const clients = Array.from({ length: 5 }, () => ({ ...saved, projection: { structure: { stages: [] }, fields: { a: 'one', b: 'two' } } }));
  assert.equal(verifyConvergence(clients, saved, 12), fingerprint(saved.projection));
  assert.throws(() => verifyConvergence(clients, saved, 11), /PERSISTED_REVISION/);
  assert.throws(() => verifyConvergence([{ ...clients[0], epoch: 'stale' }], saved, 12), /FINAL_DIVERGENCE/);
  assert.throws(() => verifyConvergence([{ ...clients[0], projection: {} }], saved, 12), /FINAL_DIVERGENCE/);
});
test('latency quantiles use nearest ranks, distinguish no samples and do not mutate input', () => {
  const samples = [5, 1, 4, 2, 3];
  assert.deepEqual(quantiles(samples), { count: 5, p50_ms: 3, p95_ms: 5, max_ms: 5 });
  assert.deepEqual(samples, [5, 1, 4, 2, 3]);
  assert.deepEqual(quantiles([]), { count: 0, p50_ms: null, p95_ms: null, max_ms: null });
});
test('unexpected errors never reflect arbitrary server messages or secret-looking content', () => {
  assert.equal(safeCode(new Error('SECRET_COOKIE_VALUE'), 'SAFE_FAILURE'), 'SAFE_FAILURE');
  assert.equal(safeCode(new Error('response body or draft text'), 'SAFE_FAILURE'), 'SAFE_FAILURE');
  assert.equal(safeCode(new LoadFailure('HTTP_STATUS_503'), 'SAFE_FAILURE'), 'HTTP_STATUS_503');
});
test('duplicate ACK binds only original sender operation and epoch without invented identity fields', () => {
  const sender = { auth: { connection_id: 'own-connection' }, userId: 'own-user' };
  const operation = { sender, original: { operation_id: 'operation', epoch: 'epoch', type: 'text.update' } };
  const ack = { type: 'operation.ack', operation_id: 'operation', epoch: 'epoch', revision: 7, current_revision: 9, duplicate: true };
  validateConfirmation(ack, operation, sender);
  for (let index = 0; index < 4; index++) assert.throws(() => validateConfirmation(ack, operation, { index }), /ACK_NOT_OWN/);
  assert.throws(() => validateConfirmation({ ...ack, epoch: 'old' }, operation, sender), /OPERATION_MISMATCH/);
  assert.throws(() => validateConfirmation({ ...ack, operation_id: 'other' }, operation, sender), /OPERATION_MISMATCH/);
  assert.throws(() => validateConfirmation(ack, undefined, sender), /OPERATION_MISMATCH/);
  assert.throws(() => validateConfirmation({ ...ack, duplicate: false }, operation, sender), /ACK_NOT_OWN/);
  assert.throws(() => validateConfirmation({ ...ack, current_revision: 6 }, operation, sender), /ACK_REVISION/);
});
test('applied still requires original actor, connection and operation type for every recipient', () => {
  const sender = { auth: { connection_id: 'own-connection' }, userId: 'own-user' };
  const operation = { sender, original: { operation_id: 'operation', epoch: 'epoch', type: 'text.update' } };
  const applied = { type: 'operation.applied', operation_id: 'operation', epoch: 'epoch', actor_id: 'own-user', connection_id: 'own-connection', operation_type: 'text.update' };
  for (let index = 0; index < 5; index++) validateConfirmation(applied, operation, { index });
  for (const change of [{ actor_id: 'other' }, { connection_id: 'same-account-other-window' }, { operation_type: 'stage.command' }]) assert.throws(() => validateConfirmation({ ...applied, ...change }, operation, sender), /ACTOR_MISMATCH/);
});
test('one shared HTTP queue spaces concurrent request starts without retries or overlapping callbacks', async () => {
  let clock = 0; let active = 0; const starts = []; const calls = [];
  const schedule = createHttpScheduler({ now: () => clock, sleep: async ms => { clock += ms; } });
  const work = id => async () => {
    assert.equal(active, 0); active++;
    starts.push(clock); calls.push(id);
    await Promise.resolve(); clock += 100; active--;
    if (id === 1) throw new Error('simulated-once');
    return id;
  };
  const outcomes = await Promise.allSettled([schedule(work(0)), schedule(work(1)), schedule(work(2)), schedule(work(3))]);
  assert.deepEqual(starts, [0, 550, 1100, 1650]);
  assert.deepEqual(calls, [0, 1, 2, 3]);
  assert.deepEqual(outcomes.map(item => item.status), ['fulfilled', 'rejected', 'fulfilled', 'fulfilled']);
});
test('HTTP scheduler respects slower responses and a longer registration gap without extra blanket delay', async () => {
  let clock = 0; const starts = [];
  const schedule = createHttpScheduler({ now: () => clock, sleep: async ms => { clock += ms; } });
  await schedule(async () => { starts.push(clock); clock += 900; });
  await schedule(async () => { starts.push(clock); });
  clock += 13000;
  await schedule(async () => { starts.push(clock); });
  assert.deepEqual(starts, [0, 900, 13900]);
});
