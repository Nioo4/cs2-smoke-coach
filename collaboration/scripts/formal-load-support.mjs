import { resolve, relative, isAbsolute } from 'node:path';
import { canonicalJson, sha256 } from '../src/formal-document.mjs';

export const ORIGIN = 'http://127.0.0.1:18220';
export const PROJECT = 'smokeline-tactics-t5-local';
export class LoadFailure extends Error {}
export function requireCheck(condition, code) { if (!condition) throw new LoadFailure(code); }
export function safeCode(error, fallback) { return error instanceof LoadFailure && /^[A-Z][A-Z0-9_]{2,70}$/.test(error.message) ? error.message : fallback; }
export function createHttpScheduler({ now, sleep, minimumStartInterval = 550 }) {
  let tail = Promise.resolve(); let lastStart = -Infinity;
  return work => {
    const scheduled = tail.then(async () => {
      const wait = Math.max(0, minimumStartInterval - (now() - lastStart));
      if (wait) await sleep(wait);
      lastStart = now();
      return work();
    });
    // Keep the queue usable; each caller still receives its own rejection, never a retry.
    tail = scheduled.then(() => undefined, () => undefined);
    return scheduled;
  };
}
export function options(args, root, nodeVersion) {
  const flags = new Set(['--execute', '--providers-disabled']);
  const values = new Set(['--project', '--duration-seconds', '--output']);
  const parsed = {};
  for (let i = 0; i < args.length; i += 1) {
    const key = args[i];
    requireCheck(!(key in parsed), 'DUPLICATE_ARGUMENT');
    if (flags.has(key)) parsed[key] = true;
    else { requireCheck(values.has(key) && args[i + 1] && !args[i + 1].startsWith('--'), 'INVALID_ARGUMENT'); parsed[key] = args[++i]; }
  }
  const duration = Number(parsed['--duration-seconds'] ?? 1800);
  requireCheck(Number.isInteger(duration) && duration >= 5 && duration <= 1800, 'INVALID_DURATION');
  if (!parsed['--execute']) return { execute: false, duration, origin: ORIGIN, project: PROJECT };
  requireCheck(nodeVersion === '24.22.2', 'PORTABLE_NODE_24_22_2_REQUIRED');
  requireCheck(parsed['--providers-disabled'] === true, 'PROVIDERS_DISABLED_ACK_REQUIRED');
  requireCheck(parsed['--project'] === PROJECT, 'ISOLATED_PROJECT_ACK_REQUIRED');
  requireCheck(typeof parsed['--output'] === 'string', 'NEW_OUTPUT_REQUIRED');
  const output = resolve(root, parsed['--output']);
  const allowed = ['.workcache', 'docs/evidence/tactics'].some(base => {
    const path = relative(resolve(root, base), output);
    return path && !path.startsWith('..') && !isAbsolute(path);
  });
  requireCheck(allowed && output.endsWith('.json'), 'OUTPUT_BOUNDARY_REQUIRED');
  return { execute: true, duration, origin: ORIGIN, project: PROJECT, output };
}
export const fingerprint = value => sha256(canonicalJson(value));
export function validateConfirmation(message, operation, recipient) {
  requireCheck(Boolean(operation) && message.operation_id === operation.original.operation_id && message.epoch === operation.original.epoch, 'CONFIRMATION_OPERATION_MISMATCH');
  if (message.type === 'operation.ack') {
    requireCheck(recipient === operation.sender && message.duplicate === true, 'ACK_NOT_OWN_DUPLICATE');
    requireCheck(Number.isSafeInteger(message.revision) && message.revision >= 0 && Number.isSafeInteger(message.current_revision) && message.current_revision >= message.revision, 'ACK_REVISION_INVALID');
    // This proves only the original sender's save; other recipients still need applied.
    return;
  }
  requireCheck(message.type === 'operation.applied' && message.connection_id === operation.sender.auth.connection_id && message.actor_id === operation.sender.userId && message.operation_type === operation.original.type, 'CONFIRMATION_ACTOR_MISMATCH');
}
export function quantiles(samples) {
  if (!samples.length) return { count: 0, p50_ms: null, p95_ms: null, max_ms: null };
  const ordered = [...samples].sort((a, b) => a - b);
  const at = p => Math.round(ordered[Math.max(0, Math.ceil(p * ordered.length) - 1)] * 100) / 100;
  return { count: ordered.length, p50_ms: at(.5), p95_ms: at(.95), max_ms: at(1) };
}
export function validateSearch(result, target, publicIds) {
  requireCheck(result?.source === 'local_curated' && result.intent?.target_slug === target.slug, 'SEARCH_NOT_LOCAL_TARGET');
  const routes = result.type === 'route_matches' ? result.routes : result.type === 'match' ? [result] : [];
  requireCheck(Array.isArray(routes) && routes.length > 0 && routes.every(item => item.lineup && publicIds.has(item.lineup.id) && (item.target ?? item.lineup.target)?.slug === target.slug), 'SEARCH_ROUTES_MISMATCH');
}
export function completionKind(duration, elapsed) { return duration === 1800 && elapsed >= 1800 ? 'FULL_30_MIN_PROTOCOL_PASS' : 'SHORT_SELFTEST_PASS'; }
export function verifyConvergence(clients, persisted, expectedRevision) {
  const hash = fingerprint(persisted.projection);
  requireCheck(persisted.revision === expectedRevision, 'PERSISTED_REVISION_MISMATCH');
  for (const client of clients) requireCheck(client.revision === expectedRevision && client.epoch === persisted.epoch && fingerprint(client.projection) === hash, 'FINAL_DIVERGENCE');
  return hash;
}
