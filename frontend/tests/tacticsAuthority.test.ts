// Test-first authority safety audit. Known red regressions intentionally remain until main approves fix.
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import * as Y from 'yjs';
import { TacticCollaborationClient, encodeBytes, projectDocument } from '../src/tactics/collaborationClient';

const tacticId = '10000000-0000-4000-8000-000000000001';
const roleId = '20000000-0000-4000-8000-000000000002';
const stageId = '30000000-0000-4000-8000-000000000003';
const epoch = '40000000-0000-4000-8000-000000000004';
const actorId = '50000000-0000-4000-8000-000000000005';
const connectionId = '60000000-0000-4000-8000-000000000006';
const foreignId = '70000000-0000-4000-8000-000000000007';
class Socket extends EventTarget {
  static OPEN = 1;
  static instances: Socket[] = [];
  readyState = 1;
  sent: Record<string, unknown>[] = [];
  transmissions: { at: number; value: Record<string, unknown> }[] = [];
  constructor(readonly url: URL) { super(); Socket.instances.push(this); }
  send(value: string) { const parsed = JSON.parse(value); this.sent.push(parsed); this.transmissions.push({ at: Date.now(), value: parsed }); }
  close(reason = '') { this.readyState = 3; this.dispatchEvent(new CloseEvent('close', { code: 1008, reason })); }
  message(value: object) { this.dispatchEvent(new MessageEvent('message', { data: JSON.stringify(value) })); }
}
const clients: TacticCollaborationClient[] = [];
const documents: Y.Doc[] = [];
function state(doc: Y.Doc, revision = 5, nextEpoch = epoch) {
  return { document_id: tacticId, epoch: nextEpoch, revision, state: encodeBytes(Y.encodeStateAsUpdate(doc)), projection: projectDocument(doc), recoveries: [], participants: [], locks: [] };
}
async function pending() {
  const doc = new Y.Doc(); documents.push(doc);
  doc.getMap('document').set('structure', { schema_kind: 'stage-board', schema_version: 2, document_id: tacticId, map: 'mirage', side: 'T', roles: [{ id: roleId, number: 1, color: '#cb5a42' }], stages: [{ id: stageId, version: 1, initial_positions: { [roleId]: { x: 50, y: 50, version: 1 } }, actions: [], lineup_ids: [] }] });
  for (const [key, value] of Object.entries({ 'tactic.title': 'fixture title', 'tactic.purpose': 'base', 'tactic.applicability': '', [`role.${roleId}.name`]: 'role', [`stage.${stageId}.name`]: 'stage', [`stage.${stageId}.description`]: '', [`stage.${stageId}.condition`]: '' })) {
    const text = new Y.Text(); text.insert(0, value); doc.getMap('fields').set(key, text);
  }
  const client = new TacticCollaborationClient(tacticId, 'memory-only-fixture'); clients.push(client); await client.connect();
  const socket = Socket.instances.at(-1)!; socket.dispatchEvent(new Event('open'));
  socket.message({ type: 'auth.ok', actor_id: actorId, connection_id: connectionId, role: 'editor', read_only: false });
  socket.message({ type: 'sync', ...state(doc) });
  const initialBytes = Y.encodeStateAsUpdate(doc);
  client.doc.transact(() => client.doc.getMap<Y.Text>('fields').get('tactic.purpose')!.insert(4, '_pending'), 'local-ui');
  const operation = socket.sent.filter(message => message.type === 'operation').at(-1)!.operation as { operation_id: string; type: string; epoch: string };
  Y.applyUpdate(doc, Y.encodeStateAsUpdate(client.doc)); // Valid candidate containing the original update.
  return { client, socket, doc, operation, initialBytes };
}
function snapshot(client: TacticCollaborationClient) {
  return { confirmed: JSON.stringify(client.state.confirmed), working: JSON.stringify(projectDocument(client.doc)), confirmedDoc: JSON.stringify(projectDocument(client.confirmedDoc)) };
}
function expectNoTrust(client: TacticCollaborationClient, before: ReturnType<typeof snapshot>) {
  expect(client.state.pending, 'invalid authority must not acknowledge pending work').toBe(1);
  expect(client.state.revision, 'invalid authority must not move confirmed revision').toBe(5);
  expect(client.state.epoch, 'invalid ACK must not rotate the active epoch').toBe(epoch);
  expect(JSON.stringify(client.state.confirmed) === before.confirmed, 'public confirmed snapshot unchanged').toBe(true);
  expect(JSON.stringify(projectDocument(client.doc)) === before.working, 'working Doc not partially mutated').toBe(true);
  expect(JSON.stringify(projectDocument(client.confirmedDoc)) === before.confirmedDoc, 'confirmed Doc not partially replaced').toBe(true);
}
beforeEach(() => {
  Socket.instances = []; vi.stubGlobal('WebSocket', Socket);
  vi.stubGlobal('fetch', vi.fn(async () => ({ ok: true, status: 201, headers: new Headers(), json: async () => ({ ticket: 'test-only' }) })));
});
afterEach(() => { clients.splice(0).forEach(client => client.dispose()); documents.splice(0).forEach(doc => doc.destroy()); vi.useRealTimers(); vi.unstubAllGlobals(); });

it('queues rapid typing below the real server in-flight and per-second limits without losing input', async () => {
  vi.useFakeTimers();
  const { client, socket, doc, operation } = await pending();
  const text = client.doc.getMap<Y.Text>('fields').get('tactic.purpose')!;
  const acquired = client.acquire(`action:${foreignId}`, 1);
  const leaseRequest = socket.sent.at(-1)!;
  socket.message({ type: 'lease.granted', request_id: leaseRequest.request_id, object_id: `action:${foreignId}`, token: 'fixture', expires_at: Date.now() + 10000, renew_every_ms: 3000 });
  await acquired;
  const expected = '迁移后确认文字保存与审计同事务，旧内容保持。'.repeat(3);
  let next = '';
  for (const character of expected) {
    next += character;
    client.doc.transact(() => { text.delete(0, text.length); text.insert(0, next); }, 'local-ui');
    client.presence(stageId, 'tactic.purpose', text.length);
    client.preview(`action:${foreignId}`, { x: 50, y: 50 });
  }
  const operations = client.recoveryExport().pending_operations;
  expect(operations[0].operation_id).toBe(operation.operation_id);
  expect(operations.length).toBeGreaterThan(16);
  expect(socket.sent.filter(value => value.type === 'operation')).toHaveLength(1);
  await expect(client.command({ type: 'role.add' })).rejects.toThrow('请先等待');
  vi.advanceTimersByTime(500);
  expect(socket.sent.filter(value => value.type === 'operation')).toHaveLength(1);
  let revision = 6;
  let acknowledged = 0;
  for (const pendingOperation of operations) {
    const sent = socket.sent.filter(value => value.type === 'operation');
    expect(sent.at(-1)?.operation).toEqual(pendingOperation);
    expect(sent.length - acknowledged).toBeLessThan(16); // Exact server backlog boundary.
    Y.applyUpdate(doc, Uint8Array.from(atob(pendingOperation.payload.update as string), character => character.charCodeAt(0)));
    socket.message({ type: 'operation.applied', ...state(doc, revision++), operation_id: pendingOperation.operation_id, actor_id: actorId, connection_id: connectionId, operation_type: pendingOperation.type });
    expect(client.state.connected, client.state.error).toBe(true);
    expect(text.toString()).toBe(expected);
    acknowledged++;
    vi.advanceTimersByTime(100);
  }
  expect(client.state.pending).toBe(0);
  expect(client.state.confirmed?.fields['tactic.purpose']).toBe(expected);
  expect(socket.sent.some(value => value.type === 'presence')).toBe(true);
  expect(socket.sent.some(value => value.type === 'lease.renew')).toBe(true);
  expect(socket.sent.some(value => value.type === 'map.preview')).toBe(true);
  for (const transmission of socket.transmissions) {
    // Stronger than the server's fixed window: count all actual messages in every 1s span.
    expect(socket.transmissions.filter(item => item.at >= transmission.at && item.at <= transmission.at + 1000).length).toBeLessThanOrEqual(50);
  }
});

it('reconnects and retries a burst in original order one at a time, including duplicate ACK and remote edits', async () => {
  vi.useFakeTimers();
  const { client, socket, doc } = await pending();
  const text = client.doc.getMap<Y.Text>('fields').get('tactic.purpose')!;
  for (const character of '连续输入三十个字后断线重试仍保留每一个原始操作以及尚未确认的文本内容') client.doc.transact(() => text.insert(text.length, character), 'local-ui');
  const original = client.recoveryExport().pending_operations;
  expect(original.length).toBeGreaterThan(30);
  const working = text.toString();
  socket.close('message_limit');
  expect(client.state.error).toContain('输入请求过于密集');
  vi.advanceTimersByTime(1000);
  expect(socket.sent.filter(value => value.type === 'operation')).toHaveLength(1);
  await client.connect();
  const next = Socket.instances.at(-1)!;
  next.message({ type: 'auth.ok', actor_id: actorId, connection_id: connectionId, role: 'editor', read_only: false });
  next.message({ type: 'sync', ...state(doc, 6) });
  expect(next.sent.filter(value => value.type === 'operation')).toHaveLength(0);
  client.retry();
  expect(next.sent.filter(value => value.type === 'operation')).toHaveLength(1);
  expect(next.sent[0].operation).toEqual(original[0]);
  let revision = 6;
  for (const [index, operation] of original.entries()) {
    expect(next.sent.filter(value => value.type === 'operation').at(-1)?.operation).toEqual(operation);
    if (index === 0) next.message({ type: 'operation.ack', ...state(doc, revision), current_revision: revision, duplicate: true, operation_id: operation.operation_id });
    else {
      Y.applyUpdate(doc, Uint8Array.from(atob(operation.payload.update as string), character => character.charCodeAt(0)));
      next.message({ type: 'operation.applied', ...state(doc, ++revision), operation_id: operation.operation_id, actor_id: actorId, connection_id: connectionId, operation_type: operation.type });
    }
    if (index === 1) {
      doc.getMap<Y.Text>('fields').get('tactic.applicability')!.insert(0, '其他窗口');
      next.message({ type: 'operation.applied', ...state(doc, ++revision), operation_id: foreignId, actor_id: actorId, connection_id: foreignId, operation_type: 'text.update' });
      next.message({ type: 'operation.ack', ...state(doc, 6), current_revision: revision, duplicate: true, operation_id: original[0].operation_id });
    }
    expect(text.toString()).toBe(working);
    expect(client.state.connected, client.state.error).toBe(true);
    vi.advanceTimersByTime(100);
  }
  expect(client.state.pending).toBe(0);
  expect(client.state.confirmed?.fields['tactic.purpose']).toBe(working);
  expect(client.state.confirmed?.fields['tactic.applicability']).toBe('其他窗口');
  expect(next.sent.filter(value => value.type === 'operation').map(value => value.operation)).toEqual(original);
});

it.each(['close', 'reconnect', 'dispose'] as const)('cancels delayed operation transmission on %s', async end => {
  vi.useFakeTimers();
  const { client, socket, doc, operation } = await pending();
  client.doc.transact(() => client.doc.getMap<Y.Text>('fields').get('tactic.purpose')!.insert(0, 'queued'), 'local-ui');
  socket.message({ type: 'operation.applied', ...state(doc, 6), operation_id: operation.operation_id, actor_id: actorId, connection_id: connectionId, operation_type: operation.type });
  expect(client.state.pending).toBe(1);
  const count = socket.sent.filter(value => value.type === 'operation').length;
  if (end === 'close') socket.close();
  else if (end === 'reconnect') await client.connect();
  else client.dispose();
  vi.advanceTimersByTime(1000);
  expect(socket.sent.filter(value => value.type === 'operation')).toHaveLength(count);
  expect(client.recoveryExport().pending_operations).toHaveLength(1);
});

it('does not start the acknowledgement timeout for text still waiting in the send queue', async () => {
  vi.useFakeTimers();
  const { client, socket, doc } = await pending();
  const text = client.doc.getMap<Y.Text>('fields').get('tactic.purpose')!;
  for (let index = 0; index < 110; index++) client.doc.transact(() => text.insert(text.length, '字'), 'local-ui');
  const queued = client.recoveryExport().pending_operations;
  for (const [index, operation] of queued.entries()) {
    Y.applyUpdate(doc, Uint8Array.from(atob(operation.payload.update as string), character => character.charCodeAt(0)));
    socket.message({ type: 'operation.applied', ...state(doc, 6 + index), operation_id: operation.operation_id, actor_id: actorId, connection_id: connectionId, operation_type: operation.type });
    vi.advanceTimersByTime(100);
    expect(client.state.recovery, client.state.error).toBe(false);
  }
  expect(client.state.pending).toBe(0);
  expect(client.state.confirmed?.fields['tactic.purpose']).toBe('base_pending' + '字'.repeat(110));
});

it('accepts a legitimate duplicate ACK original revision below current_revision after the mandated sync', async () => {
  const { client, socket, doc, operation } = await pending();
  socket.message({ type: 'sync', ...state(doc, 9) });
  expect(client.state.pending).toBe(1);
  socket.message({ type: 'operation.ack', ...state(doc, 6), current_revision: 9, duplicate: true, operation_id: operation.operation_id });
  expect(client.state.pending).toBe(0); expect(client.state.revision).toBe(9);
  expect(client.state.confirmed?.fields['tactic.purpose']).toBe('base_pending');
});
it('accepts a valid remote applied state without confirming unrelated local pending work', async () => {
  const { client, socket, doc, operation } = await pending();
  socket.message({ type: 'operation.applied', ...state(doc, 6), operation_id: foreignId, actor_id: actorId, connection_id: foreignId, operation_type: 'text.update' });
  expect(client.state.revision).toBe(6); expect(client.state.pending).toBe(1);
  expect(client.recoveryExport().pending_operations[0].operation_id).toBe(operation.operation_id);
});
it('confirms a valid own applied receipt with matching operation type, actor and connection', async () => {
  const { client, socket, doc, operation } = await pending();
  socket.message({ type: 'operation.applied', ...state(doc, 6), operation_id: operation.operation_id, actor_id: actorId, connection_id: connectionId, operation_type: operation.type });
  expect(client.state.pending).toBe(0); expect(client.state.revision).toBe(6);
  expect(client.state.confirmed?.fields['tactic.purpose']).toBe('base_pending');
});

it.each([
  ['wrong envelope document', { document_id: foreignId }],
  ['wrong ACK epoch', { epoch: foreignId }],
  ['unknown operation ID', { operation_id: foreignId }],
  ['missing operation ID', { operation_id: undefined }],
  ['non-string operation ID', { operation_id: 1 }],
  ['missing duplicate flag', { duplicate: undefined }],
  ['false duplicate flag', { duplicate: false }],
  ['missing current_revision', { current_revision: undefined }],
  ['current revision regression', { revision: 4, current_revision: 4 }],
  ['original revision above current', { revision: 7, current_revision: 6 }],
  ['negative original revision', { revision: -1 }],
  ['fractional original revision', { revision: 5.5 }],
  ['string original revision', { revision: '6' }],
  ['fractional current revision', { current_revision: 6.5 }],
  ['string current revision', { current_revision: '6' }],
  ['null current revision', { current_revision: null }],
  ['unsafe integer current revision', { current_revision: Number.MAX_SAFE_INTEGER + 1 }],
] as const)('does not trust duplicate ACK with %s', async (_label, change) => {
  const { client, socket, doc, operation } = await pending(); const before = snapshot(client);
  socket.message({ type: 'operation.ack', ...state(doc, 6), current_revision: 6, operation_id: operation.operation_id, duplicate: true, ...change });
  expectNoTrust(client, before);
});
it.each(['operation.applied', 'sync'] as const)('does not accept a same-epoch backward revision in %s', async type => {
  const { client, socket, doc, operation } = await pending(); const before = snapshot(client);
  socket.message({ type, ...state(doc, 4), operation_id: operation.operation_id, actor_id: actorId, connection_id: connectionId, operation_type: 'text.update' });
  expectNoTrust(client, before);
});
it('does not confirm a local operation from another connection, even with same actor and operation ID', async () => {
  const { client, socket, doc, operation } = await pending();
  socket.message({ type: 'operation.applied', ...state(doc, 6), operation_id: operation.operation_id, actor_id: actorId, connection_id: foreignId, operation_type: 'text.update' });
  expect(client.state.pending).toBe(1);
  expect(client.recoveryExport().pending_operations[0].operation_id).toBe(operation.operation_id);
});
it('does not acknowledge an operation.applied whose type differs from the pending operation', async () => {
  const { client, socket, doc, operation } = await pending(); const before = snapshot(client);
  socket.message({ type: 'operation.applied', ...state(doc, 6), operation_id: operation.operation_id, actor_id: actorId, connection_id: connectionId, operation_type: 'stage.command' });
  expectNoTrust(client, before);
});
it('does not acknowledge an own-connection applied receipt with an actor different from auth.ok', async () => {
  const { client, socket, doc, operation } = await pending(); const before = snapshot(client);
  socket.message({ type: 'operation.applied', ...state(doc, 6), operation_id: operation.operation_id, actor_id: foreignId, connection_id: connectionId, operation_type: operation.type });
  expectNoTrust(client, before);
});
it('validates foreign Yjs document identity before mutating either live Doc', async () => {
  const { client, socket, doc, operation } = await pending(); const before = snapshot(client);
  const structure = structuredClone(doc.getMap('document').get('structure')) as { document_id: string };
  structure.document_id = foreignId; doc.getMap('document').set('structure', structure);
  socket.message({ type: 'operation.ack', ...state(doc, 6), current_revision: 6, operation_id: operation.operation_id, duplicate: true });
  expectNoTrust(client, before);
});
it.each(['%%%not-base64%%%', encodeBytes(Uint8Array.of(255, 255, 255))])('preserves pending and confirmed work on an undecodable state', async badState => {
  const { client, socket, doc, operation } = await pending(); const before = snapshot(client);
  socket.message({ type: 'operation.ack', ...state(doc, 6), state: badState, current_revision: 6, operation_id: operation.operation_id, duplicate: true });
  expectNoTrust(client, before); expect(client.canWrite()).toBe(false);
  expect(client.recoveryExport().pending_operations).toHaveLength(1);
});
it('merges same-epoch remote authority without losing or falsely confirming local pending text', async () => {
  const { client, socket, doc, operation, initialBytes } = await pending();
  const localText = client.doc.getMap<Y.Text>('fields').get('tactic.purpose'); const undo = client.undo;
  const remote = new Y.Doc(); documents.push(remote); Y.applyUpdate(remote, initialBytes);
  remote.getMap<Y.Text>('fields').get('tactic.applicability')!.insert(0, 'remote-only');
  socket.message({ type: 'operation.applied', ...state(remote, 6), operation_id: foreignId, actor_id: actorId, connection_id: foreignId, operation_type: 'text.update' });
  expect(client.state.pending).toBe(1); expect(client.undo).toBe(undo);
  expect(client.doc.getMap('fields').get('tactic.purpose')).toBe(localText);
  expect(localText?.toString()).toBe('base_pending');
  expect(client.state.confirmed?.fields['tactic.purpose']).toBe('base');
  expect(client.state.projection?.fields['tactic.applicability']).toBe('remote-only');
  Y.applyUpdate(doc, Y.encodeStateAsUpdate(remote));
  socket.message({ type: 'operation.ack', ...state(doc, 6), current_revision: 9, duplicate: true, operation_id: operation.operation_id });
  expect(client.state.pending).toBe(0); expect(client.state.revision).toBe(9);
  expect(client.state.confirmed?.fields['tactic.purpose']).toBe('base_pending');
});
it('accepts an explicit valid sync with a new epoch after auth but preserves old-epoch pending for export only', async () => {
  const { client, socket, doc, operation } = await pending();
  const text = doc.getMap<Y.Text>('fields').get('tactic.purpose')!;
  text.delete(0, text.length); text.insert(0, 'new-authority');
  socket.message({ type: 'sync', ...state(doc, 0, foreignId) });
  expect(client.state.epoch).toBe(foreignId); expect(client.state.revision).toBe(0);
  expect(client.state.confirmed?.fields['tactic.purpose']).toBe('new-authority');
  expect(client.state.pending).toBe(1); expect(client.state.recovery).toBe(true); expect(client.canWrite()).toBe(false);
  expect(client.recoveryExport().pending_operations[0].epoch).toBe(operation.epoch);
  const count = socket.sent.length; client.retry(); expect(socket.sent).toHaveLength(count);
});
it('compares projection keys structurally rather than rejecting a different object key insertion order', async () => {
  const { client, socket, doc, operation } = await pending(); const packet = state(doc, 6);
  const projection = { fields: Object.fromEntries(Object.entries(packet.projection.fields).reverse()), structure: Object.fromEntries(Object.entries(packet.projection.structure).reverse()) };
  socket.message({ type: 'operation.ack', ...packet, projection, current_revision: 6, duplicate: true, operation_id: operation.operation_id });
  expect(client.state.pending).toBe(0); expect(client.state.revision).toBe(6);
});
it('rejects a projection that disagrees with encoded state before acknowledging or replacing either Doc', async () => {
  const { client, socket, doc, operation } = await pending(); const before = snapshot(client); const packet = state(doc, 6);
  packet.projection.fields['tactic.purpose'] = 'not the encoded state';
  socket.message({ type: 'operation.ack', ...packet, current_revision: 6, duplicate: true, operation_id: operation.operation_id });
  expectNoTrust(client, before); expect(client.canWrite()).toBe(false);
});
it.each(['extra-root', 'document-sequence', 'fields-sequence', 'non-YText'] as const)('rejects raw Yjs root/type mismatch %s without promoting it into an expected map', async kind => {
  const { client, socket, doc, operation } = await pending(); const before = snapshot(client); const packet = state(doc, 6);
  const bad = new Y.Doc(); documents.push(bad);
  if (kind === 'extra-root' || kind === 'non-YText') {
    Y.applyUpdate(bad, Y.encodeStateAsUpdate(doc));
    if (kind === 'extra-root') bad.getMap('extra').set('unexpected', true);
    else bad.getMap('fields').set('tactic.purpose', 'not a Y.Text');
  } else if (kind === 'document-sequence') {
    bad.getText('document').insert(0, 'not map entries'); bad.getMap('fields').set('text', new Y.Text('value'));
  } else {
    bad.getMap('document').set('structure', packet.projection.structure); bad.getText('fields').insert(0, 'not map entries');
  }
  socket.message({ type: 'operation.ack', ...packet, state: encodeBytes(Y.encodeStateAsUpdate(bad)), current_revision: 6, duplicate: true, operation_id: operation.operation_id });
  expectNoTrust(client, before); expect(client.canWrite()).toBe(false);
});
it.each([{ schema_version: 1 }, { schema_kind: 'old-static' }])('rejects a different encoded schema %j', async change => {
  const { client, socket, doc, operation } = await pending(); const before = snapshot(client);
  doc.getMap('document').set('structure', { ...(doc.getMap('document').get('structure') as object), ...change });
  socket.message({ type: 'operation.ack', ...state(doc, 6), current_revision: 6, duplicate: true, operation_id: operation.operation_id });
  expectNoTrust(client, before);
});
it('does not use operation.applied as a route to a new epoch', async () => {
  const { client, socket, doc, operation } = await pending(); const before = snapshot(client);
  socket.message({ type: 'operation.applied', ...state(doc, 6, foreignId), operation_id: operation.operation_id, actor_id: actorId, connection_id: connectionId, operation_type: operation.type });
  expectNoTrust(client, before);
});
it.each([undefined, null, 'false', 0, 1, {}, []])('does not coerce a non-boolean auth read_only value %j', async readOnly => {
  const { client, socket } = await pending(); const before = snapshot(client);
  socket.message({ type: 'auth.ok', actor_id: actorId, connection_id: connectionId, role: 'editor', read_only: readOnly });
  expectNoTrust(client, before); expect(client.canWrite()).toBe(false);
});
it.each([{ role: 'editor', read_only: true }, { role: 'viewer', read_only: false }])('rejects contradictory auth role/read_only %j', async role => {
  const { client, socket } = await pending(); const before = snapshot(client);
  socket.message({ type: 'auth.ok', actor_id: actorId, connection_id: connectionId, ...role });
  expectNoTrust(client, before); expect(client.canWrite()).toBe(false);
});
it('does not enable writing between auth.ok and a valid initial sync', async () => {
  const { doc } = await pending();
  const client = new TacticCollaborationClient(tacticId, 'fixture'); clients.push(client); await client.connect();
  const socket = Socket.instances.at(-1)!;
  socket.message({ type: 'auth.ok', actor_id: actorId, connection_id: connectionId, role: 'editor', read_only: false });
  expect(client.canWrite()).toBe(false); expect(client.state.connected).toBe(false);
  socket.message({ type: 'sync', ...state(doc, 6) });
  expect(client.canWrite()).toBe(true); expect(client.state.connected).toBe(true);
});
it.each([false, true])('ignores a late duplicate ACK after exact retry and own applied, with laterPending=%s', async laterPending => {
  vi.useFakeTimers();
  const { client, socket, doc, operation } = await pending();
  vi.advanceTimersByTime(11000); expect(client.state.recovery).toBe(true);
  client.retry();
  const sent = socket.sent.filter(message => message.type === 'operation');
  expect(sent).toHaveLength(2); expect(sent[1]).toEqual(sent[0]);
  socket.message({ type: 'operation.applied', ...state(doc, 6), operation_id: operation.operation_id, actor_id: actorId, connection_id: connectionId, operation_type: operation.type });
  expect(client.state.pending).toBe(0);
  if (laterPending) client.doc.transact(() => client.doc.getMap<Y.Text>('fields').get('tactic.applicability')!.insert(0, 'later unconfirmed'), 'local-ui');
  socket.message({ type: 'sync', ...state(doc, 6) }); // Retry path's legitimate fresh sync.
  const before = snapshot(client);
  const pendingIds = client.recoveryExport().pending_operations.map(item => item.operation_id);
  socket.message({ type: 'operation.ack', ...state(doc, 6), operation_id: operation.operation_id, current_revision: 6, duplicate: true });
  expect(client.state.connected).toBe(true); expect(client.state.error).toBe('');
  expect(client.state.revision).toBe(6); expect(client.state.pending).toBe(laterPending ? 1 : 0);
  expect(client.recoveryExport().pending_operations.map(item => item.operation_id)).toEqual(pendingIds);
  expect(snapshot(client)).toEqual(before);
});
