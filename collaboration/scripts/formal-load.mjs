// Explicit isolated T5 protocol workload. No DB/internal/admin/container access.
import { randomBytes, randomUUID } from 'node:crypto';
import { open } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
import { performance } from 'node:perf_hooks';
import { setTimeout as delay } from 'node:timers/promises';
import * as Y from 'yjs';
import WebSocket from 'ws';
import { projectFormalDocument } from '../src/formal-document.mjs';
import { ORIGIN, PROJECT, options, requireCheck, LoadFailure, safeCode, createHttpScheduler, quantiles, fingerprint, validateSearch, validateConfirmation, completionKind, verifyConvergence } from './formal-load-support.mjs';

const root = fileURLToPath(new URL('../../', import.meta.url));
let config;
try { config = options(process.argv.slice(2), root, process.versions.node); }
catch { console.error(JSON.stringify({ status: 'INVALID_INVOCATION', code: 'CHECK_ARGUMENTS_RUNTIME_AND_OUTPUT' })); process.exit(2); }
if (!config.execute) {
  console.log(JSON.stringify({ status: 'PLAN_ONLY', origin: ORIGIN, project: PROJECT, duration_seconds: config.duration, new_accounts: 5, editor_connections: 5, provider_calls: 'FORBIDDEN', requires: ['--execute', '--providers-disabled', '--project', '--output'] }));
} else await run();

async function run() {
  // wx reserves one new evidence file before any network activity; never overwrite prior evidence.
  let output;
  try { output = await open(config.output, 'wx', 0o600); }
  catch { console.error(JSON.stringify({ status: 'OUTPUT_RESERVATION_FAILED' })); process.exitCode = 2; return; }
  const report = { run_id: randomUUID(), origin: ORIGIN, project_acknowledgement: PROJECT, providers_disabled_acknowledgement: true,
    started_at: new Date().toISOString(), node_version: process.versions.node, ws_version: '8.22.0', yjs_version: '13.6.33',
    requested_seconds: config.duration, status: 'STARTING', real_model: 'NOT_RUN', resource_sampling: 'MAIN_EXTERNAL_REQUIRED',
    workload: { editor_interval_ms: 2000, aggregate_target_writes_per_second: 2.5, presence_interval_ms: 1000, teaching_cycle_interval_ms: 5000, registration_spacing_ms: 13000, http_minimum_start_interval_ms: 550 },
    teaching_preflight: false, teaching_load_cycles: 0,
    accounts: [], counters: { submitted: 0, confirmed: 0, duplicate_ack: 0, rejected: 0, disconnects: 0, teaching_failed: 0 }, http: {} };
  await output.writeFile(JSON.stringify(report));
  const clients = []; const operations = new Map(); const ownLatency = []; const peerLatency = []; const teachingLatency = [];
  let failure = ''; let closing = false; let loadStart = null; let loadEnd = null; let progress;
  const fail = code => { if (!failure) failure = code; };
  const interrupted = () => fail('INTERRUPTED');
  process.on('SIGINT', interrupted); process.on('SIGTERM', interrupted);
  const healthy = () => requireCheck(!failure, failure || 'FAILED');
  async function until(predicate, timeout = 10_000) {
    const start = performance.now();
    while (!predicate()) { healthy(); requireCheck(performance.now() - start < timeout, 'CONFIRMATION_TIMEOUT'); await delay(20); }
    healthy();
  }
  async function pause(ms) {
    const end = performance.now() + ms;
    while (performance.now() < end) { healthy(); await delay(Math.min(100, end - performance.now())); }
  }
  const scheduleHttp = createHttpScheduler({ now: () => performance.now(), sleep: pause });
  function request(client, path, method = 'GET', body, expected = 200, route = path) {
    return scheduleHttp(() => performRequest(client, path, method, body, expected, route));
  }
  async function performRequest(client, path, method, body, expected, route) {
    healthy();
    let response;
    const headers = { Origin: ORIGIN, Accept: 'application/json' };
    if (client?.cookie) headers.Cookie = client.cookie;
    if (method !== 'GET') {
      headers['Content-Type'] = 'application/json'; headers['Idempotency-Key'] = randomUUID();
      if (client?.csrf) headers['X-CSRF-Token'] = client.csrf;
    }
    try { response = await fetch(`${ORIGIN}/api/v1${path}`, { method, headers, ...(body === undefined ? {} : { body: JSON.stringify(body) }), redirect: 'error', signal: AbortSignal.timeout(15_000) }); }
    catch { throw new LoadFailure('HTTP_TRANSPORT_UNCERTAIN_NO_RETRY'); }
    const metric = `${method} ${route} ${response.status}`; report.http[metric] = (report.http[metric] ?? 0) + 1;
    requireCheck(response.status === expected, `HTTP_STATUS_${response.status}`);
    let result; try { result = await response.json(); } catch { throw new LoadFailure('HTTP_INVALID_JSON_UNCERTAIN_NO_RETRY'); }
    if (client) {
      const cookies = response.headers.getSetCookie();
      if (cookies.length) { requireCheck(cookies.length === 1 && cookies[0].startsWith('smokeline_session='), 'UNEXPECTED_SESSION_COOKIE'); client.cookie = cookies[0].split(';')[0]; }
      if (result.csrf_token) client.csrf = result.csrf_token;
    }
    return result;
  }
  function onMessage(client, raw) {
    if (closing) return;
    try {
      const msg = JSON.parse(raw.toString());
      if (msg.type === 'auth.ok') { client.auth = msg; return; }
      if (msg.type === 'operation.rejected') { report.counters.rejected++; fail('OPERATION_REJECTED'); return; }
      if (msg.type === 'protocol.error') { fail('WS_PROTOCOL_ERROR'); return; }
      if (!['sync', 'operation.applied', 'operation.ack'].includes(msg.type)) return;
      requireCheck(msg.document_id === report.tactic_id, 'DOCUMENT_ID_CHANGED');
      const revision = msg.current_revision ?? msg.revision;
      requireCheck(Number.isSafeInteger(revision) && revision >= client.revision, 'REVISION_REGRESSION');
      requireCheck(!client.epoch || client.epoch === msg.epoch, 'EPOCH_CHANGED');
      Y.applyUpdate(client.doc, Buffer.from(msg.state, 'base64'));
      client.epoch = msg.epoch; client.revision = revision; client.projection = msg.projection;
      if (msg.type === 'sync') { client.synced = true; return; }
      const operation = operations.get(msg.operation_id);
      validateConfirmation(msg, operation, client);
      if (msg.type === 'operation.ack') report.counters.duplicate_ack++;
      if (!operation.seen.has(client.index)) {
        operation.seen.add(client.index);
        operation.revision ??= msg.revision;
        requireCheck(operation.revision === msg.revision, 'OPERATION_REVISION_CHANGED');
        if (operation.live) (client === operation.sender ? ownLatency : peerLatency).push(performance.now() - operation.sent);
        if (client === operation.sender && operation.live) report.counters.confirmed++;
      }
    } catch (error) { fail(safeCode(error, 'WS_INVALID_AUTHORITY')); }
  }
  async function connect(client) {
    const issued = await request(client, `/tactics/${report.tactic_id}/collaboration-ticket`, 'POST', {}, 201, '/tactics/:id/collaboration-ticket');
    requireCheck(issued.role === 'editor' && !issued.read_only, 'TICKET_NOT_EDITOR');
    const ws = new WebSocket('ws://127.0.0.1:18220/ws/tactics', { origin: ORIGIN, maxPayload: 12 * 1024 * 1024, handshakeTimeout: 5000 });
    client.ws = ws;
    ws.on('error', () => { if (!closing) fail('WS_TRANSPORT_ERROR'); });
    ws.on('close', () => { if (!closing) { report.counters.disconnects++; fail('EDITOR_DISCONNECTED'); } });
    ws.on('message', raw => onMessage(client, raw));
    await until(() => ws.readyState === WebSocket.OPEN, 6000);
    ws.send(JSON.stringify({ type: 'auth', ticket: issued.ticket }));
    await until(() => client.auth && client.synced);
    requireCheck(client.auth.role === 'editor' && client.auth.read_only === false && client.auth.actor_id === client.userId, 'AUTH_NOT_EXPECTED_EDITOR');
  }
  async function submit(client, type, payload, live = false) {
    healthy(); requireCheck(client.ws.readyState === WebSocket.OPEN, 'EDITOR_NOT_OPEN');
    const operation_id = randomUUID();
    const original = { type, operation_id, epoch: client.epoch, payload };
    const operation = { original, sender: client, sent: performance.now(), seen: new Set(), live };
    operations.set(operation_id, operation);
    if (live) report.counters.submitted++;
    client.ws.send(JSON.stringify({ type: 'operation', operation: original }));
    await until(() => operation.seen.size === 5);
    return operation;
  }
  let target; let publicIds;
  async function teachingCycle() {
    const started = performance.now();
    try {
      const map = await request(null, '/maps/mirage');
      requireCheck(map.targets?.some(item => item.slug === target.slug && item.name_zh === target.name_zh), 'PUBLIC_TARGET_CHANGED');
      const choices = await request(null, `/targets/${encodeURIComponent(target.slug)}/lineups`, 'GET', undefined, 200, '/targets/:slug/lineups');
      requireCheck(choices.origins?.length > 0, 'NO_PUBLIC_ORIGINS');
      publicIds = new Set(choices.origins.map(item => item.lineup.id));
      const detail = await request(null, `/lineups/${choices.origins[0].lineup.id}`, 'GET', undefined, 200, '/lineups/:id');
      requireCheck(detail.target?.slug === target.slug && detail.source === 'local_curated', 'PUBLIC_DETAIL_MISMATCH');
      for (const variant of detail.other_lineups ?? []) publicIds.add(variant.id);
      const result = await request(null, '/search', 'POST', { kind: 'query', text: `Mirage ${target.name_zh} 烟`, input_mode: 'text' });
      validateSearch(result, target, publicIds);
      teachingLatency.push(performance.now() - started);
    } catch (error) { report.counters.teaching_failed++; throw error; }
  }
  try {
    requireCheck((await request(null, '/features')).tactics_enabled === true, 'TACTICS_NOT_ENABLED');
    const map = await request(null, '/maps/mirage');
    target = map.targets?.find(item => item.origin_count > 0 && typeof item.name_zh === 'string');
    requireCheck(Boolean(target), 'PUBLIC_TEACHING_FIXTURE_REQUIRED');
    await teachingCycle(); // Search preflight happens before creating any test identity.
    report.teaching_preflight = true;
    for (let index = 0; index < 5; index++) {
      if (index) await pause(13_000);
      const client = { index, revision: -1, doc: new Y.Doc() }; clients.push(client);
      const registration = await request(client, '/auth/register', 'POST', { username: `t5load_${randomBytes(10).toString('hex')}`, display_name: `T5 editor ${index + 1}`, password: randomBytes(32).toString('base64url') }, 201);
      client.userId = registration.user.id; report.accounts.push(client.userId);
      const csrf = client.csrf; const session = await request(client, '/auth/session');
      requireCheck(session.user.id === client.userId && session.csrf_token === csrf, 'SESSION_IDENTITY_MISMATCH');
    }
    const owner = clients[0];
    const created = await request(owner, '/teams', 'POST', { name: `T5 load ${report.run_id}` }, 201);
    report.team_id = created.team.id; report.notebook_id = created.notebook.id;
    const invite = await request(owner, `/teams/${report.team_id}/invites`, 'POST', {}, 201, '/teams/:id/invites');
    requireCheck(invite.token_available === true && typeof invite.token === 'string', 'INVITE_TOKEN_UNAVAILABLE');
    for (const client of clients.slice(1)) {
      const preview = await request(client, '/invites/preview', 'POST', { token: invite.token });
      requireCheck(preview.team_id === report.team_id && preview.already_member === false, 'INVITE_PREVIEW_MISMATCH');
      const acceptance = await request(client, '/invites/accept', 'POST', { token: invite.token });
      requireCheck(acceptance.team_id === report.team_id && acceptance.member_role === 'viewer', 'INVITE_ACCEPT_MISMATCH');
      await request(owner, `/teams/${report.team_id}/members/${client.userId}`, 'PATCH', { role: 'editor' }, 200, '/teams/:id/members/:id');
    }
    for (const client of clients) {
      const view = await request(client, `/teams/${report.team_id}`, 'GET', undefined, 200, '/teams/:id');
      requireCheck(view.notebook.id === report.notebook_id && view.my_role === (client.index ? 'editor' : 'owner') && view.permissions.read && view.permissions.create, 'TEAM_PERMISSION_MISMATCH');
    }
    const tactic = await request(owner, `/notebooks/${report.notebook_id}/tactics`, 'POST', { title: `T5 load ${report.run_id}`, side: 'T' }, 201, '/notebooks/:id/tactics');
    report.tactic_id = tactic.tactic.id;
    for (const client of clients) await connect(client);
    requireCheck(new Set(clients.map(client => client.auth.connection_id)).size === 5 && new Set(report.accounts).size === 5, 'EDITOR_IDENTITIES_NOT_UNIQUE');
    while (owner.projection.structure.stages.length < 5) await submit(owner, 'stage.command', { expected_revision: owner.revision, command: { type: 'stage.add', after_stage_id: null } });
    const stages = owner.projection.structure.stages;
    report.start_revision = owner.revision; report.connected_editors = 5;
    clients.forEach((client, index) => { client.field = `stage.${stages[index].id}.description`; client.stageId = stages[index].id; });
    loadStart = performance.now(); const deadline = loadStart + config.duration * 1000;
    report.load_started_at = new Date().toISOString(); report.status = 'RUNNING';
    progress = setInterval(() => console.log(JSON.stringify({ run_id: report.run_id, status: failure ? 'FAILED' : 'RUNNING', elapsed_seconds: Math.floor((performance.now() - loadStart) / 1000), submitted: report.counters.submitted, confirmed: report.counters.confirmed })), 60_000);
    async function writer(client) {
      let sequence = 0; let next = loadStart + client.index * 300;
      while (next < deadline) {
        await pause(Math.max(0, next - performance.now())); healthy(); if (performance.now() >= deadline) break;
        const field = client.doc.getMap('fields').get(client.field);
        requireCheck(field instanceof Y.Text, 'EDIT_FIELD_MISSING');
        const vector = Y.encodeStateVector(client.doc); const marker = `T5 ${client.index} ${++sequence}`;
        client.doc.transact(() => { field.delete(0, field.length); field.insert(0, marker); });
        await submit(client, 'text.update', { update: Buffer.from(Y.encodeStateAsUpdate(client.doc, vector)).toString('base64') }, true);
        client.lastMarker = marker; client.writes = sequence;
        next = Math.max(next + 2000, performance.now());
      }
      await pause(Math.max(0, deadline - performance.now()));
    }
    async function teaching() {
      let next = loadStart;
      while (next < deadline) { await pause(Math.max(0, next - performance.now())); if (performance.now() >= deadline) break; await teachingCycle(); report.teaching_load_cycles++; next = Math.max(next + 5000, performance.now()); }
    }
    async function presence() {
      while (performance.now() < deadline) {
        for (const client of clients) { healthy(); client.ws.send(JSON.stringify({ type: 'presence', payload: { stage_id: client.stageId } })); }
        await pause(Math.min(1000, Math.max(0, deadline - performance.now())));
      }
    }
    const tasks = [...clients.map(writer), teaching(), presence()];
    await Promise.all(tasks.map(task => task.catch(error => { fail(safeCode(error, 'WORKLOAD_FAILED')); })));
    healthy(); loadEnd = performance.now();
    requireCheck(loadEnd - loadStart >= config.duration * 1000, 'DURATION_INCOMPLETE');
    const live = [...operations.values()].filter(operation => operation.live);
    requireCheck(live.length > 0 && live.every(operation => operation.seen.size === 5), 'OBSERVER_CONFIRMATION_MISSING');
    requireCheck(ownLatency.length === live.length && peerLatency.length === live.length * 4, 'LATENCY_OBSERVERS_MISSING');
    requireCheck(report.teaching_load_cycles > 0 && report.counters.teaching_failed === 0, 'TEACHING_LOAD_INCOMPLETE');
    requireCheck(report.counters.submitted === report.counters.confirmed && clients.every(client => client.writes > 0), 'UNCONFIRMED_WRITES');
    const revisions = new Set(live.map(operation => operation.revision));
    requireCheck(revisions.size === live.length, 'REVISION_NOT_UNIQUE');
    const persisted = await request(owner, `/tactics/${report.tactic_id}/present`, 'GET', undefined, 200, '/tactics/:id/present');
    const expectedRevision = report.start_revision + live.length;
    report.final_fingerprint = verifyConvergence(clients, persisted, expectedRevision);
    for (const client of clients) {
      requireCheck(persisted.projection.fields[client.field] === client.lastMarker, 'LAST_CONFIRMED_MARKER_MISSING');
      requireCheck(fingerprint(projectFormalDocument(client.doc)) === report.final_fingerprint, 'YJS_PROJECTION_DIVERGENCE');
    }
    report.final_revision = persisted.revision; report.final_epoch = persisted.epoch;
    report.per_editor_confirmed = clients.map(client => client.writes);
    healthy();
    report.status = completionKind(config.duration, (loadEnd - loadStart) / 1000);
  } catch (error) { fail(safeCode(error, 'RUN_FAILED')); report.status = 'FAIL'; process.exitCode = 1; }
  finally {
    if (progress) clearInterval(progress);
    report.failure_code = failure || null;
    report.elapsed_load_seconds = loadStart === null ? 0 : ((loadEnd ?? performance.now()) - loadStart) / 1000;
    report.pending_operations = [...operations.values()].filter(operation => operation.seen.size !== 5).length;
    report.unconfirmed_writes = report.counters.submitted - report.counters.confirmed;
    report.last_confirmed_clients = clients.filter(client => client.synced).map(client => ({ user_id: client.userId, revision: client.revision, epoch: client.epoch, fingerprint: fingerprint(client.projection) }));
    report.latency = { own_save_ack: quantiles(ownLatency), remote_sync: quantiles(peerLatency), teaching_cycle: quantiles(teachingLatency) };
    report.completed_at = new Date().toISOString();
    closing = true;
    for (const client of clients) { client.ws?.close(); client.doc.destroy(); }
    await delay(100);
    for (const client of clients) client.ws?.terminate();
    process.off('SIGINT', interrupted); process.off('SIGTERM', interrupted);
    await output.truncate(0); await output.write(JSON.stringify(report, null, 2), 0, 'utf8'); await output.close();
    console.log(JSON.stringify(report));
  }
}
