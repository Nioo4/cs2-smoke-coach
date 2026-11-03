import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { useSyncExternalStore } from 'react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import * as Y from 'yjs';
import { TacticCollaborationClient, decodeBytes, encodeBytes, projectDocument } from '../src/tactics/collaborationClient';
import TacticTextField from '../src/tactics/TacticTextField';
import TacticEditor from '../src/tactics/TacticEditor';
import { MemoryRouter } from 'react-router-dom';

class Socket extends EventTarget {
  static OPEN = 1;
  static instances: Socket[] = [];
  readyState = 1;
  sent: Record<string, unknown>[] = [];
  constructor(readonly url: URL) { super(); Socket.instances.push(this); }
  send(value: string) { this.sent.push(JSON.parse(value)); }
  close() { this.readyState = 3; this.dispatchEvent(new Event('close')); }
  message(value: object) { this.dispatchEvent(new MessageEvent('message', { data: JSON.stringify(value) })); }
}
const clients: TacticCollaborationClient[] = [];
const docs: Y.Doc[] = [];
function seed() {
  const doc = new Y.Doc(); docs.push(doc);
  doc.getMap('document').set('structure', { schema_kind: 'stage-board', schema_version: 2, document_id: 'tactic', map: 'mirage', side: 'T', roles: [{ id: 'role', number: 1, color: '#cb5a42' }], stages: [{ id: 'stage', version: 1, initial_positions: { role: { x: 50, y: 50, version: 1 } }, actions: [], lineup_ids: [] }] });
  for (const [key, value] of Object.entries({ 'tactic.title': 'title', 'tactic.purpose': 'abcd', 'tactic.applicability': '', 'stage.stage.name': '阶段', 'stage.stage.description': '', 'stage.stage.condition': '', 'role.role.name': '角色' })) { const text = new Y.Text(); text.insert(0, value); doc.getMap('fields').set(key, text); }
  return doc;
}
function authority(doc: Y.Doc, revision = 1, epoch = 'epoch') { return { document_id: 'tactic', epoch, revision, state: encodeBytes(Y.encodeStateAsUpdate(doc)), projection: projectDocument(doc), participants: [], recoveries: [], locks: [] }; }
function auth(readOnly = false, connectionId = 'window-a') { return { type: 'auth.ok', actor_id: 'same-account', connection_id: connectionId, role: readOnly ? 'viewer' : 'editor', read_only: readOnly }; }
async function connected() {
  const client = new TacticCollaborationClient('tactic', 'csrf'); clients.push(client); await client.connect();
  const socket = Socket.instances.at(-1)!; socket.dispatchEvent(new Event('open'));
  socket.message(auth());
  const doc = seed(); socket.message({ type: 'sync', ...authority(doc) });
  return { client, socket, doc };
}
async function editorWithStages(count = 1, mobile = false, narrowFine = false, readOnly = false) {
  vi.stubGlobal('matchMedia', vi.fn((query: string) => ({ matches: query === '(any-pointer: fine)' ? narrowFine : mobile || (narrowFine && query.includes('max-width') && !query.includes('pointer: coarse')), addEventListener: vi.fn(), removeEventListener: vi.fn() })));
  render(<MemoryRouter><TacticEditor tacticId="tactic" session={{ user: { id: 'u', username: 'user', display_name: '玩家' }, csrf_token: 'csrf' }} /></MemoryRouter>);
  await waitFor(() => expect(Socket.instances).toHaveLength(1));
  const socket = Socket.instances[0]; const doc = seed();
  if (count > 1) {
    const structure = structuredClone(doc.getMap('document').get('structure')) as { stages: { id: string }[] };
    structure.stages.push({ ...structure.stages[0], id: 'second' }); doc.getMap('document').set('structure', structure);
    for (const suffix of ['name', 'description', 'condition']) { const text = new Y.Text(); if (suffix === 'name') text.insert(0, '第二阶段'); doc.getMap('fields').set(`stage.second.${suffix}`, text); }
  }
  act(() => { socket.message(auth(readOnly)); socket.message({ type: 'sync', ...authority(doc) }); });
  await screen.findByLabelText('战术名称');
  fireEvent.load(screen.getByAltText('Mirage 战术地图底图'));
  if (!mobile) fireEvent.click(screen.getByRole('button', { name: '新增动作' }));
  else fireEvent.click(screen.getByRole('button', { name: '展开动作与说明' }));
  return { socket, doc };
}
beforeEach(() => {
  Socket.instances = [];
  vi.stubGlobal('WebSocket', Socket);
  vi.stubGlobal('fetch', vi.fn(async () => ({ ok: true, status: 201, headers: new Headers(), json: async () => ({ ticket: 'business-ticket' }) })));
});
afterEach(() => { clients.splice(0).forEach(client => client.dispose()); docs.splice(0).forEach(doc => doc.destroy()); vi.useRealTimers(); vi.unstubAllGlobals(); });
it('does not show saved or overwrite newer editor text when an older receipt and remote text arrive before the newest receipt', async () => {
  const { socket, doc } = await editorWithStages();
  const input = screen.getByLabelText('战术目的');
  const saveSlot = document.querySelector('.editor-save-slot')!;
  fireEvent.change(input, { target: { value: 'abcd_first' } });
  const first = socket.sent.find(message => message.type === 'operation')!.operation as { operation_id: string; payload: { update: string } };
  fireEvent.change(input, { target: { value: 'abcd_first_second' } });
  expect(saveSlot).toHaveTextContent('正在保存 2 项');
  Y.applyUpdate(doc, decodeBytes(first.payload.update));
  act(() => socket.message({ type: 'operation.applied', ...authority(doc, 2), operation_id: first.operation_id, actor_id: 'same-account', connection_id: 'window-a', operation_type: 'text.update' }));
  expect(saveSlot).toHaveTextContent('正在保存 1 项');
  expect(saveSlot).not.toHaveTextContent('已保存');
  expect(input).toHaveValue('abcd_first_second');
  await waitFor(() => expect(socket.sent.filter(message => message.type === 'operation')).toHaveLength(2));
  const second = socket.sent.filter(message => message.type === 'operation')[1].operation as typeof first;
  doc.getMap<Y.Text>('fields').get('tactic.purpose')!.insert(0, '远端_');
  act(() => socket.message({ type: 'operation.applied', ...authority(doc, 3), operation_id: 'remote', actor_id: 'same-account', connection_id: 'window-b', operation_type: 'text.update' }));
  expect(input).toHaveValue('远端_abcd_first_second');
  act(() => socket.message({ type: 'operation.ack', ...authority(doc, 2), current_revision: 3, duplicate: true, operation_id: first.operation_id }));
  expect(saveSlot).toHaveTextContent('正在保存 1 项');
  expect(saveSlot).not.toHaveTextContent('已保存');
  expect(input).toHaveValue('远端_abcd_first_second');
  Y.applyUpdate(doc, decodeBytes(second.payload.update));
  act(() => socket.message({ type: 'operation.applied', ...authority(doc, 4), operation_id: second.operation_id, actor_id: 'same-account', connection_id: 'window-a', operation_type: 'text.update' }));
  expect(saveSlot).toHaveTextContent('已保存 · 版本 4');
  expect(input).toHaveValue('远端_abcd_first_second');
});

it('keeps a delayed then rejected action unconfirmed and retains its form through the following authoritative sync', async () => {
  const { socket, doc } = await editorWithStages();
  const saveSlot = document.querySelector('.editor-save-slot')!;
  fireEvent.change(screen.getByLabelText('动作类型'), { target: { value: 'wait' } });
  fireEvent.change(screen.getByLabelText('新动作说明'), { target: { value: '等待同步后再行动' } });
  fireEvent.click(screen.getByRole('button', { name: '添加到阶段' }));
  await waitFor(() => expect(socket.sent.filter(message => message.type === 'operation')).toHaveLength(1));
  const packet = socket.sent.find(message => message.type === 'operation')!;
  const operation = packet.operation as { operation_id: string };
  expect(saveSlot).toHaveTextContent('正在保存 1 项');
  expect(saveSlot).not.toHaveTextContent('已保存');
  expect(screen.getByLabelText('新动作说明')).toHaveValue('等待同步后再行动');
  await act(async () => {
    socket.message({ type: 'operation.rejected', operation_id: operation.operation_id, code: 'COLLAB_UNAVAILABLE' });
    socket.message({ type: 'sync', ...authority(doc, 2) });
  });
  expect(saveSlot).toHaveTextContent('草稿未确认');
  expect(saveSlot).not.toHaveTextContent('已保存');
  expect(screen.getByLabelText('新动作说明')).toHaveValue('等待同步后再行动');
  expect(screen.getByLabelText('动作类型')).toHaveValue('wait');
  fireEvent.click(screen.getByRole('button', { name: '按原操作重试' }));
  await waitFor(() => expect(socket.sent.filter(message => message.type === 'operation')).toHaveLength(2));
  expect(socket.sent.filter(message => message.type === 'operation')[1]).toEqual(packet);
  expect(saveSlot).not.toHaveTextContent('已保存');
  expect(screen.getByLabelText('新动作说明')).toHaveValue('等待同步后再行动');
});

it('automatically authenticates same-origin and confirms only authoritative state, then handles duplicate ACK current revision', async () => {
  const { client, socket, doc } = await connected();
  expect(socket.url.pathname).toBe('/ws/tactics'); expect(socket.url.search).toBe('');
  expect(socket.sent[0]).toEqual({ type: 'auth', ticket: 'business-ticket' });
  const text = client.doc.getMap<Y.Text>('fields').get('tactic.purpose')!;
  client.doc.transact(() => text.insert(4, 'local'), 'local-ui');
  expect(client.state.pending).toBe(1);
  expect(client.state.confirmed?.fields['tactic.purpose']).toBe('abcd');
  const operation = socket.sent.find(message => message.type === 'operation')!.operation as { operation_id: string };
  socket.message({ type: 'operation.ack', ...authority(doc, 1), current_revision: 4, operation_id: operation.operation_id, duplicate: true });
  expect(client.state.revision).toBe(4); expect(client.state.pending).toBe(0);
  expect(client.state.confirmed?.fields['tactic.purpose']).toBe('abcd');
});
it('preserves a rejected operation and retries the exact original ID/body while refusing an old epoch', async () => {
  const { client, socket, doc } = await connected();
  client.doc.transact(() => client.doc.getMap<Y.Text>('fields').get('tactic.purpose')!.insert(0, 'x'), 'local-ui');
  const original = socket.sent.find(message => message.type === 'operation')!;
  socket.message({ type: 'operation.rejected', operation_id: (original.operation as { operation_id: string }).operation_id, code: 'TACTIC_CONFLICT' });
  expect(client.state.recovery).toBe(true); client.retry();
  expect(socket.sent.at(-1)).toEqual(original);
  socket.message({ type: 'sync', ...authority(doc, 2, 'new-epoch') });
  const count = socket.sent.length; client.retry(); expect(socket.sent).toHaveLength(count);
  expect(client.state.error).toContain('旧草稿只能导出');
});
it('updates renewed lease expiration and drops failed renewal tokens before reacquiring', async () => {
  vi.useFakeTimers(); vi.setSystemTime(100000);
  const { client, socket } = await connected();
  const acquired = client.acquire('action:a', 3);
  const request = socket.sent.at(-1)!;
  socket.message({ type: 'lease.granted', request_id: request.request_id, object_id: 'action:a', token: 'old-token', expires_at: Date.now() + 10000, renew_every_ms: 3000 });
  await acquired;
  vi.advanceTimersByTime(3000);
  const renewal = socket.sent.at(-1)!;
  socket.message({ type: 'lease.renewed', request_id: renewal.request_id, object_id: 'action:a', expires_at: Date.now() + 10000 });
  expect((await client.acquire('action:a', 3)).expires_at).toBe(113000);
  vi.advanceTimersByTime(3000);
  client.setLocalDraft({ action: 'a' });
  socket.message({ type: 'protocol.error', request_id: socket.sent.at(-1)!.request_id, code: 'LEASE_EXPIRED' });
  expect(client.state.recovery).toBe(true);
  client.retry();
  const again = client.acquire('action:a', 3);
  expect(socket.sent.at(-1)?.type).toBe('lease.acquire');
  const next = socket.sent.at(-1)!;
  socket.message({ type: 'lease.granted', request_id: next.request_id, object_id: 'action:a', token: 'new-token', expires_at: Date.now() + 10000, renew_every_ms: 3000 });
  expect((await again).token).toBe('new-token');
});
it('throttles selection presence to ten messages per second with final state', async () => {
  vi.useFakeTimers();
  const { client, socket } = await connected();
  for (let index = 0; index < 100; index++) client.presence('stage', 'tactic.purpose', index % 4);
  expect(socket.sent.filter(message => message.type === 'presence')).toHaveLength(1);
  vi.advanceTimersByTime(100);
  expect(socket.sent.filter(message => message.type === 'presence')).toHaveLength(2);
});
it('cancels an overdue queued pointer before immediately sending a newer clear and keeps subsequent sends spaced', async () => {
  vi.useFakeTimers(); vi.setSystemTime(1000);
  const { client, socket } = await connected();
  client.presence('stage', undefined, undefined, { pointer: { x: 10, y: 10 } });
  vi.setSystemTime(1050);
  client.presence('stage', undefined, undefined, { pointer: { x: 20, y: 20 } });
  // Wall time passes while the old callback has not run, as with a busy event loop.
  vi.setSystemTime(1200); client.presence('stage');
  expect(socket.sent.filter(message => message.type === 'presence')).toHaveLength(2);
  expect(socket.sent.at(-1)).toEqual({ type: 'presence', payload: { stage_id: 'stage' } });
  client.presence('stage', undefined, undefined, { pointer: { x: 30, y: 30 } });
  vi.advanceTimersByTime(99);
  expect(socket.sent.filter(message => message.type === 'presence')).toHaveLength(2);
  vi.advanceTimersByTime(1);
  expect(socket.sent.filter(message => message.type === 'presence')).toHaveLength(3);
  expect(socket.sent.at(-1)).toEqual({ type: 'presence', payload: { stage_id: 'stage', pointer: { x: 30, y: 30 } } });
  expect(socket.sent.some(message => (message.payload as { pointer?: { x: number } })?.pointer?.x === 20)).toBe(false);
});
it('drops old transient queues and presence across reconnect, disconnect and disposal', async () => {
  vi.useFakeTimers();
  const { client, socket, doc } = await connected();
  client.presence('stage', undefined, undefined, { pointer: { x: 10, y: 10 } });
  client.presence('stage', undefined, undefined, { pointer: { x: 20, y: 20 } });
  await client.connect(); const next = Socket.instances.at(-1)!;
  next.dispatchEvent(new Event('open'));
  next.message(auth(false, 'window-new'));
  next.message({ type: 'sync', ...authority(doc) });
  vi.advanceTimersByTime(100);
  expect(next.sent.some(message => message.type === 'presence')).toBe(false);
  expect(socket.sent.filter(message => message.type === 'presence')).toHaveLength(1);
  client.presence('stage', undefined, undefined, { pointer: { x: 30, y: 30 } });
  client.presence('stage', undefined, undefined, { pointer: { x: 40, y: 40 } });
  next.close(); vi.advanceTimersByTime(100);
  expect(next.sent.filter(message => message.type === 'presence')).toHaveLength(1);
  await client.connect(); const final = Socket.instances.at(-1)!;
  final.message(auth(false, 'window-final')); final.message({ type: 'sync', ...authority(doc) });
  client.presence('stage'); client.presence('stage', undefined, undefined, { pointer: { x: 50, y: 50 } });
  client.dispose(); const count = final.sent.length;
  vi.advanceTimersByTime(100); client.presence('stage'); vi.advanceTimersByTime(100);
  expect(final.sent).toHaveLength(count);
});
function Field({ client, field = 'tactic.purpose' }: { client: TacticCollaborationClient; field?: string }) { useSyncExternalStore(client.subscribe, client.snapshot); return <TacticTextField client={client} field={field} label="目的" stageId="stage" disabled={false} />; }
it('keeps local caret anchored across a remote insertion before it and visibly renders a valid peer caret', async () => {
  const { client, socket, doc } = await connected();
  const { container } = render(<Field client={client} />);
  const input = screen.getByLabelText('目的') as HTMLTextAreaElement;
  input.focus(); input.setSelectionRange(2, 2); fireEvent.select(input);
  doc.getMap<Y.Text>('fields').get('tactic.purpose')!.insert(0, 'ZZ');
  const relative = encodeBytes(Y.encodeRelativePosition(Y.createRelativePositionFromTypeIndex(doc.getMap<Y.Text>('fields').get('tactic.purpose')!, 3)));
  act(() => socket.message({ type: 'operation.applied', ...authority(doc, 2), operation_id: 'remote', connection_id: 'window-b', actor_id: 'same-account', operation_type: 'text.update', participants: [{ id: 'same-account', connection_id: 'window-b', name: '同账号另一窗口', color: '#315d57', field: 'tactic.purpose', relative_cursor: relative }] }));
  expect(input.value).toBe('ZZabcd'); expect(input.selectionStart).toBe(4);
  expect(container.querySelector('.tactic-peer-caret')).toBeInTheDocument();
  expect(screen.getByText('同账号另一窗口')).toBeVisible();
});
it('preserves IME edits across concurrent remote prefix insertion and keeps undo local', async () => {
  const { client, socket, doc } = await connected();
  render(<Field client={client} />);
  const input = screen.getByLabelText('目的') as HTMLTextAreaElement;
  input.focus(); input.setSelectionRange(4, 4);
  fireEvent.compositionStart(input);
  fireEvent.change(input, { target: { value: 'abcd中文' } });
  doc.getMap<Y.Text>('fields').get('tactic.purpose')!.insert(0, 'remote');
  act(() => socket.message({ type: 'operation.applied', ...authority(doc, 2), operation_id: 'remote', connection_id: 'window-b', actor_id: 'same-account', operation_type: 'text.update' }));
  expect(input.value).toBe('abcd中文');
  fireEvent.compositionEnd(input, { data: '中文' });
  expect(client.state.projection?.fields['tactic.purpose']).toBe('remoteabcd中文');
  act(() => client.undo?.undo());
  expect(client.state.projection?.fields['tactic.purpose']).toBe('remoteabcd');
  expect(client.state.confirmed?.fields['tactic.purpose']).toBe('remoteabcd');
});
it('buffers a required name while empty, retains it on blur, and merges the later replacement with remote text', async () => {
  const { client, socket, doc } = await connected();
  render(<Field client={client} field="tactic.title" />);
  const input = screen.getByLabelText('目的') as HTMLTextAreaElement;
  input.focus(); fireEvent.change(input, { target: { value: '' } }); fireEvent.blur(input);
  expect(client.state.pending).toBe(0); expect(client.hasUnconfirmedWork()).toBe(true);
  expect(screen.getByText(/此项不能为空/)).toBeVisible();
  doc.getMap<Y.Text>('fields').get('tactic.title')!.insert(0, 'remote');
  act(() => socket.message({ type: 'operation.applied', ...authority(doc, 2), operation_id: 'remote', connection_id: 'window-b', actor_id: 'same-account', operation_type: 'text.update' }));
  expect(input.value).toBe('');
  fireEvent.change(input, { target: { value: '新名字' } });
  expect(client.state.projection?.fields['tactic.title']).toBe('remote新名字');
  expect(client.state.pending).toBe(1);
  expect(client.state.recovery).toBe(false);
  expect(screen.queryByText(/此项不能为空/)).not.toBeInTheDocument();
});
it('retains pending text on disconnect and marks a lost acknowledgement after ten seconds', async () => {
  vi.useFakeTimers();
  const { client, socket } = await connected();
  client.doc.transact(() => client.doc.getMap<Y.Text>('fields').get('tactic.purpose')!.insert(0, 'draft'), 'local-ui');
  vi.advanceTimersByTime(11000);
  expect(client.state.recovery).toBe(true);
  expect(client.state.pending).toBe(1);
  expect(client.state.error).toContain('尚未收到保存确认');
  socket.close();
  expect(client.state.connected).toBe(false);
  expect(client.recoveryExport().pending_operations).toHaveLength(1);
});
it('mounts the actual React editor, sends a real stage command, and uses authoritative generated stage IDs', async () => {
  vi.stubGlobal('matchMedia', vi.fn(() => ({ matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn() })));
  render(<MemoryRouter><TacticEditor tacticId="tactic" session={{ user: { id: 'u', username: 'user', display_name: '玩家' }, csrf_token: 'csrf' }} /></MemoryRouter>);
  await waitFor(() => expect(Socket.instances).toHaveLength(1));
  const socket = Socket.instances[0]; const doc = seed();
  act(() => { socket.message(auth()); socket.message({ type: 'sync', ...authority(doc) }); });
  fireEvent.click(await screen.findByRole('button', { name: '新增阶段' }));
  const sent = socket.sent.find(value => value.type === 'operation')!.operation as { operation_id: string; payload: { command: unknown; expected_revision: number } };
  expect(sent.payload).toEqual({ expected_revision: 1, command: { type: 'stage.add', after_stage_id: 'stage' } });
  const structure = structuredClone(doc.getMap('document').get('structure')) as { stages: object[] };
  structure.stages.push({ id: 'server-generated-stage', version: 1, initial_positions: { role: { x: 50, y: 50, version: 1 } }, actions: [], lineup_ids: [] });
  doc.getMap('document').set('structure', structure);
  for (const suffix of ['name', 'description', 'condition']) { const text = new Y.Text(); if (suffix === 'name') text.insert(0, '服务端新阶段'); doc.getMap('fields').set(`stage.server-generated-stage.${suffix}`, text); }
  await act(async () => { socket.message({ type: 'operation.applied', ...authority(doc, 2), operation_id: sent.operation_id, connection_id: 'window-a', actor_id: 'same-account', operation_type: 'stage.command' }); });
  expect(screen.getByRole('button', { name: '服务端新阶段' })).toBeVisible();
  expect(screen.getByText(/版本 2/, { selector: '.editor-save-slot' })).toBeVisible();
});
it('retains the original stage identity of a form when that stage is remotely deleted', async () => {
  vi.stubGlobal('matchMedia', vi.fn(() => ({ matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn() })));
  render(<MemoryRouter><TacticEditor tacticId="tactic" session={{ user: { id: 'u', username: 'user', display_name: '玩家' }, csrf_token: 'csrf' }} /></MemoryRouter>);
  await waitFor(() => expect(Socket.instances).toHaveLength(1));
  const socket = Socket.instances[0]; const doc = seed();
  const structure = structuredClone(doc.getMap('document').get('structure')) as { stages: { id: string }[] };
  structure.stages.push({ ...structure.stages[0], id: 'surviving' });
  doc.getMap('document').set('structure', structure);
  for (const suffix of ['name', 'description', 'condition']) { const text = new Y.Text(); if (suffix === 'name') text.insert(0, '保留阶段'); doc.getMap('fields').set(`stage.surviving.${suffix}`, text); }
  act(() => { socket.message(auth()); socket.message({ type: 'sync', ...authority(doc) }); });
  fireEvent.click(await screen.findByRole('button', { name: '新增动作' }));
  fireEvent.change(await screen.findByLabelText('动作类型'), { target: { value: 'wait' } });
  fireEvent.change(screen.getByLabelText('新动作说明'), { target: { value: '原阶段待提交动作' } });
  structure.stages.splice(0, 1); doc.getMap('document').set('structure', structure);
  act(() => socket.message({ type: 'operation.applied', ...authority(doc, 2), operation_id: 'remote-delete', connection_id: 'window-b', actor_id: 'same-account', operation_type: 'stage.command' }));
  await waitFor(() => expect(screen.getByText(/草稿仍属于原阶段/)).toBeVisible());
  fireEvent.submit(screen.getByRole('button', { name: '添加到阶段' }).closest('form')!);
  expect(socket.sent.some(message => message.type === 'operation')).toBe(false);
  expect(screen.getByLabelText('新动作说明')).toHaveValue('原阶段待提交动作');
});
it('clears only a resolved acquisition error and never clears a persistent operation failure on lease cleanup', async () => {
  const { client, socket } = await connected();
  const failed = client.acquire('action:a', 1);
  socket.message({ type: 'protocol.error', request_id: socket.sent.at(-1)!.request_id, code: 'COLLAB_LOCKED' });
  await expect(failed).rejects.toThrow('其他窗口编辑');
  expect(client.state.error).toContain('其他窗口编辑');
  const success = client.acquire('action:a', 1);
  socket.message({ type: 'lease.granted', request_id: socket.sent.at(-1)!.request_id, object_id: 'action:a', token: 'token', expires_at: Date.now() + 10000, renew_every_ms: 3000 });
  await success; expect(client.state.error).toBe('');
  socket.message({ type: 'operation.rejected', operation_id: 'failed-write', code: 'TACTIC_CONFLICT' });
  const persistentError = client.state.error;
  client.release('action:a');
  socket.message({ type: 'protocol.error', request_id: socket.sent.at(-1)!.request_id, code: 'VALIDATION_ERROR' });
  expect(client.state.error).toBe(persistentError);
  expect(client.state.auxiliaryError).toContain('释放未确认');
  client.clearLeaseError(); expect(client.state.error).toBe(persistentError);
});
it('does not send an optimistic relative cursor or a queued field deleted before presence is delivered', async () => {
  vi.useFakeTimers();
  const { client, socket, doc } = await connected();
  client.doc.transact(() => client.doc.getMap<Y.Text>('fields').get('tactic.purpose')!.insert(0, 'new'), 'local-ui');
  client.presence('stage', 'tactic.purpose', 1);
  const presence = socket.sent.at(-1) as { payload: Record<string, unknown> };
  expect(presence.payload.relative_cursor).toBeUndefined();
  client.presence('stage', 'role.role.name', 1);
  doc.getMap('fields').delete('role.role.name');
  socket.message({ type: 'sync', ...authority(doc, 2) });
  vi.advanceTimersByTime(100);
  const queued = socket.sent.at(-1) as { payload: Record<string, unknown> };
  expect(queued.payload).toEqual({ stage_id: 'stage' });
});
it('preserves a failed initial authority error through socket close rather than hiding it as auxiliary state', async () => {
  const client = new TacticCollaborationClient('tactic', 'csrf'); clients.push(client); await client.connect();
  const socket = Socket.instances.at(-1)!;
  socket.message({ type: 'protocol.error', code: 'COLLAB_UNAVAILABLE' });
  expect(client.state.error).toContain('无法加载战术');
  expect(client.state.auxiliaryError).toBe('');
  socket.close();
  expect(client.state.error).toContain('无法加载战术');
  expect(client.state.connected).toBe(false);
});
it.each([[401, '登录已失效'], [403, '没有访问此战术的权限'], [404, '找不到此战术'], [503, '请稍后重试']] as const)('safely distinguishes ticket HTTP %i from login failure', async (status, text) => {
  vi.stubGlobal('fetch', vi.fn(async () => ({ ok: false, status, headers: new Headers(), json: async () => ({ error: { code: 'safe-error', message: 'ignored internal context' } }) })));
  const client = new TacticCollaborationClient('unknown-tactic', 'csrf'); clients.push(client); await client.connect();
  expect(client.state.error).toContain(text);
  expect(Socket.instances).toHaveLength(0);
  expect(client.state.error).not.toContain('ignored internal');
});
it('keeps both sidebars mounted and preserves an unsubmitted action across collapse and expand', async () => {
  const { socket } = await editorWithStages();
  const description = screen.getByLabelText('新动作说明');
  const name = screen.getByLabelText('角色 1 名称');
  fireEvent.change(screen.getByLabelText('动作类型'), { target: { value: 'wait' } });
  fireEvent.change(description, { target: { value: '尚未提交的动作' } });
  fireEvent.click(screen.getByRole('button', { name: '收起阶段与角色' }));
  fireEvent.click(screen.getByRole('button', { name: '收起动作与说明' }));
  expect(name).not.toBeVisible(); expect(description).not.toBeVisible();
  expect(screen.getByRole('button', { name: '展开动作与说明' })).toHaveAttribute('aria-expanded', 'false');
  expect(description).toBeInTheDocument(); expect(description).toHaveValue('尚未提交的动作');
  fireEvent.click(screen.getByRole('button', { name: '展开动作与说明' }));
  fireEvent.click(screen.getByRole('button', { name: '展开阶段与角色' }));
  expect(screen.getByLabelText('新动作说明')).toBe(description); expect(description).toBeVisible();
  expect(screen.getByLabelText('角色 1 名称')).toBe(name);
  expect(socket.sent.filter(message => message.type === 'operation')).toHaveLength(0);
});
it('confirms inheritance for the selected stage UUID and refuses a remotely removed target', async () => {
  const { socket, doc } = await editorWithStages(2);
  fireEvent.click(screen.getByRole('button', { name: '第二阶段' }));
  fireEvent.click(screen.getByRole('button', { name: '重新继承前阶段站位' }));
  expect(screen.getByRole('dialog', { name: '确认重新继承站位' })).toHaveTextContent('其他阶段的独立站位不会被改写');
  expect(socket.sent.some(message => message.type === 'operation')).toBe(false);
  fireEvent.click(screen.getByRole('button', { name: '取消继承' }));
  expect(socket.sent.some(message => message.type === 'operation')).toBe(false);
  fireEvent.click(screen.getByRole('button', { name: '重新继承前阶段站位' }));
  fireEvent.click(screen.getByRole('button', { name: '确认重新继承' }));
  const sent = socket.sent.find(message => message.type === 'operation')!.operation as { operation_id: string; payload: object };
  expect(sent.payload).toEqual({ expected_revision: 1, command: { type: 'stage.reinherit', stage_id: 'second' } });
  await act(async () => socket.message({ type: 'operation.applied', ...authority(doc, 2), operation_id: sent.operation_id, connection_id: 'window-a', actor_id: 'same-account', operation_type: 'stage.command' }));
  fireEvent.click(screen.getByRole('button', { name: '重新继承前阶段站位' }));
  const structure = structuredClone(doc.getMap('document').get('structure')) as { stages: { id: string }[] };
  structure.stages.pop(); doc.getMap('document').set('structure', structure);
  act(() => socket.message({ type: 'sync', ...authority(doc, 3) }));
  expect(screen.getByRole('button', { name: '确认重新继承' })).toBeDisabled();
  expect(screen.getByText('原阶段已变化，请取消后重新选择阶段。')).toBeVisible();
  expect(socket.sent.filter(message => message.type === 'operation')).toHaveLength(1);
});
it('shares only valid map selection UUIDs and pointers, retaining map presence during pending text and clearing queued state', async () => {
  vi.useFakeTimers();
  const { client, socket } = await connected();
  client.presence('stage', undefined, undefined, { selectedObjectId: 'role', pointer: { x: 30, y: 80 } });
  expect(socket.sent.at(-1)).toEqual({ type: 'presence', payload: { stage_id: 'stage', selected_object_id: 'role', pointer: { x: 30, y: 80 } } });
  client.doc.transact(() => client.doc.getMap<Y.Text>('fields').get('tactic.purpose')!.insert(0, 'draft'), 'local-ui');
  client.presence('stage', undefined, undefined, { selectedObjectId: 'role', pointer: { x: 40, y: 70 } });
  vi.advanceTimersByTime(100);
  expect(socket.sent.at(-1)).toEqual({ type: 'presence', payload: { stage_id: 'stage', selected_object_id: 'role', pointer: { x: 40, y: 70 } } });
  client.presence('stage', undefined, undefined, { selectedObjectId: 'position:stage:role', pointer: { x: NaN, y: 999 } });
  vi.advanceTimersByTime(100);
  expect(socket.sent.at(-1)).toEqual({ type: 'presence', payload: { stage_id: 'stage' } });
  for (let i = 0; i < 100; i++) client.presence('stage', undefined, undefined, { selectedObjectId: 'role', pointer: { x: i, y: 50 } });
  client.presence('stage'); vi.advanceTimersByTime(100);
  expect(socket.sent.at(-1)).toEqual({ type: 'presence', payload: { stage_id: 'stage' } });
  expect(socket.sent.filter(message => message.type === 'presence')).toHaveLength(4);
});
it('wires actual editor role selection and clears its map presence when the window loses focus', async () => {
  const { socket } = await editorWithStages();
  vi.useFakeTimers();
  fireEvent.click(screen.getByRole('button', { name: '选择角色 1：角色' }));
  act(() => { vi.advanceTimersByTime(100); });
  expect(socket.sent.filter(message => message.type === 'presence').at(-1)).toEqual({ type: 'presence', payload: { stage_id: 'stage', selected_object_id: 'role' } });
  fireEvent(window, new Event('blur'));
  act(() => { vi.advanceTimersByTime(100); });
  expect(socket.sent.filter(message => message.type === 'presence').at(-1)).toEqual({ type: 'presence', payload: { stage_id: 'stage' } });
  expect(socket.sent.some(message => message.type === 'operation')).toBe(false);
});
const objectOpportunity = { source_operation_id: '10000000-0000-4000-8000-000000000001', recovery_id: null, is_delete: false };
it('uses server-only object undo references while preserving the separate text UndoManager and latest confirmed revision', async () => {
  const { client, socket, doc } = await connected();
  const textUndo = client.undo;
  socket.message({ type: 'object.undo.state', undo: objectOpportunity });
  doc.getMap<Y.Text>('fields').get('tactic.purpose')!.insert(0, '远端文字');
  socket.message({ type: 'operation.applied', ...authority(doc, 2), operation_type: 'text.update', operation_id: 'remote-text', connection_id: 'window-b', actor_id: 'same-account' });
  expect(client.state.objectUndo).toEqual(objectOpportunity); expect(client.undo).toBe(textUndo);
  const completion = client.undoObject();
  const packet = socket.sent.filter(message => message.type === 'operation').at(-1)!;
  const operation = packet.operation as { operation_id: string; epoch: string; type: string; payload: object };
  expect(operation.type).toBe('stage.command'); expect(operation.epoch).toBe('epoch');
  expect(operation.payload).toEqual({ expected_revision: 2, command: { type: 'object.undo', source_operation_id: objectOpportunity.source_operation_id, recovery_id: null } });
  socket.message({ type: 'operation.applied', ...authority(doc, 3), operation_id: operation.operation_id, connection_id: 'window-a', actor_id: 'same-account', operation_type: 'stage.command' });
  await completion;
  expect(client.state.objectUndo).toBeNull(); expect(client.undo).toBe(textUndo);
  expect(client.state.confirmed?.fields['tactic.purpose']).toBe('远端文字abcd');
  await expect(client.undoObject()).rejects.toThrow('没有可用');
});
it('clears object opportunities on sync/epoch/disconnect/reconnect but preserves an already sent undo for exact retry', async () => {
  const { client, socket, doc } = await connected();
  socket.message({ type: 'object.undo.state', undo: objectOpportunity });
  socket.message({ type: 'sync', ...authority(doc, 2) }); expect(client.state.objectUndo).toBeNull();
  socket.message({ type: 'object.undo.state', undo: objectOpportunity });
  socket.message({ type: 'sync', ...authority(doc, 3, 'new-epoch') }); expect(client.state.objectUndo).toBeNull();
  socket.message({ type: 'object.undo.state', undo: objectOpportunity });
  const completion = client.undoObject(); const rejected = expect(completion).rejects.toThrow('断开');
  const original = socket.sent.filter(message => message.type === 'operation').at(-1)!;
  socket.close(); await rejected;
  expect(client.state.objectUndo).toBeNull(); expect(client.state.pending).toBe(1);
  expect(client.recoveryExport().pending_operations).toEqual([original.operation]);
  await client.connect(); expect(client.state.objectUndo).toBeNull();
  const next = Socket.instances.at(-1)!;
  next.message(auth(false, 'window-new')); next.message({ type: 'sync', ...authority(doc, 3, 'new-epoch') });
  socket.message({ type: 'object.undo.state', undo: objectOpportunity }); expect(client.state.objectUndo).toBeNull();
  client.retry(); expect(next.sent.filter(message => message.type === 'operation').at(-1)).toEqual(original);
  next.message({ type: 'operation.ack', ...authority(doc, 4, 'new-epoch'), operation_id: (original.operation as { operation_id: string }).operation_id, current_revision: 4, duplicate: true });
  expect(client.state.pending).toBe(0); expect(client.state.objectUndo).toBeNull();
});
it('reports object conflict without automatically changing revision or operation ID and accepts a retained server opportunity after sync', async () => {
  const { client, socket, doc } = await connected();
  socket.message({ type: 'object.undo.state', undo: objectOpportunity });
  const completion = client.undoObject(); const rejected = expect(completion).rejects.toThrow('后续内容已变化，无法撤销');
  const original = socket.sent.filter(message => message.type === 'operation').at(-1)!;
  const id = (original.operation as { operation_id: string }).operation_id;
  socket.message({ type: 'operation.rejected', operation_id: id, code: 'TACTIC_CONFLICT' }); await rejected;
  socket.message({ type: 'sync', ...authority(doc, 5) });
  socket.message({ type: 'object.undo.state', undo: objectOpportunity });
  expect(client.state.objectUndo).toEqual(objectOpportunity); expect(client.state.pending).toBe(1);
  expect(client.state.error).toContain('不会自动改用新版本重试');
  expect(socket.sent.filter(message => message.type === 'operation')).toHaveLength(1);
  await expect(client.undoObject()).rejects.toThrow('先处理未确认');
  client.retry(); expect(socket.sent.filter(message => message.type === 'operation').at(-1)).toEqual(original);
});
it('fails closed on malformed summaries and blocks undo for local drafts and read-only connections', async () => {
  const { client, socket } = await connected();
  for (const undo of [{ ...objectOpportunity, source_operation_id: 'not-uuid' }, { ...objectOpportunity, recovery_id: 'not-uuid' }, { ...objectOpportunity, old_structure: {} }]) {
    socket.message({ type: 'object.undo.state', undo }); expect(client.state.objectUndo).toBeNull();
  }
  socket.message({ type: 'object.undo.state', undo: objectOpportunity });
  client.setLocalDraft({ drawing: true }); await expect(client.undoObject()).rejects.toThrow('先处理未确认');
  client.setLocalDraft(null);
  socket.message(auth(true)); await expect(client.undoObject()).rejects.toThrow('没有可用');
  expect(socket.sent.some(message => message.type === 'operation')).toBe(false);
  client.dispose(); expect(client.state.objectUndo).toBeNull();
});
it('retains an unconfirmed undo after a service rejection and accepts the server opportunity reissued after sync', async () => {
  const { client, socket, doc } = await connected();
  socket.message({ type: 'object.undo.state', undo: objectOpportunity });
  const completion = client.undoObject(); const rejected = expect(completion).rejects.toThrow('无法加载战术');
  const original = socket.sent.filter(message => message.type === 'operation').at(-1)!;
  socket.message({ type: 'operation.rejected', operation_id: (original.operation as { operation_id: string }).operation_id, code: 'COLLAB_UNAVAILABLE' });
  await rejected;
  socket.message({ type: 'sync', ...authority(doc) }); expect(client.state.objectUndo).toBeNull();
  socket.message({ type: 'object.undo.state', undo: objectOpportunity }); expect(client.state.objectUndo).toEqual(objectOpportunity);
  expect(client.state.pending).toBe(1); expect(client.state.recovery).toBe(true);
  await expect(client.undoObject()).rejects.toThrow('先处理未确认');
  client.retry(); expect(socket.sent.filter(message => message.type === 'operation').at(-1)).toEqual(original);
});
it('shows an independent delete-undo button, disables it for drafts/pending and sends no old content', async () => {
  const { socket, doc } = await editorWithStages();
  fireEvent.click(screen.getByText('撤销与重做'));
  expect(screen.getByRole('button', { name: '撤销对象' })).toBeDisabled();
  const summary = { ...objectOpportunity, is_delete: true, recovery_id: '20000000-0000-4000-8000-000000000002' };
  act(() => socket.message({ type: 'object.undo.state', undo: summary }));
  const undo = screen.getByRole('button', { name: '撤销删除并恢复为副本' }); expect(undo).toBeEnabled();
  fireEvent.change(screen.getByLabelText('新动作说明'), { target: { value: '未提交' } }); expect(undo).toBeDisabled();
  fireEvent.click(screen.getByRole('button', { name: '取消动作绘制' })); expect(undo).toBeEnabled();
  expect(screen.getByRole('button', { name: '撤销文字' })).toBeVisible(); expect(screen.getByRole('button', { name: '重做文字' })).toBeVisible();
  fireEvent.click(undo); expect(undo).toBeDisabled();
  const operation = socket.sent.filter(message => message.type === 'operation').at(-1)!.operation as { operation_id: string; payload: object };
  expect(operation.payload).toEqual({ expected_revision: 1, command: { type: 'object.undo', source_operation_id: summary.source_operation_id, recovery_id: summary.recovery_id } });
  await act(async () => socket.message({ type: 'operation.applied', ...authority(doc, 2), operation_id: operation.operation_id, connection_id: 'window-a', actor_id: 'same-account', operation_type: 'stage.command' }));
  expect(screen.getByRole('button', { name: '撤销对象' })).toBeDisabled();
});
it.each([false, true])('disables object undo when mobile=%s and the connection is read-only or mobile', async mobile => {
  const { socket } = await editorWithStages(1, mobile);
  act(() => {
    if (!mobile) socket.message(auth(true));
    socket.message({ type: 'object.undo.state', undo: objectOpportunity });
  });
  expect(screen.getByRole('button', { name: '撤销对象' })).toBeDisabled();
});

it('keeps the same map and mounted fields when switching inspector context and receiving saved text', async () => {
  const { socket, doc } = await editorWithStages();
  const map = screen.getByRole('region', { name: '阶段地图' });
  const roleName = screen.getByLabelText('角色 1 名称');
  fireEvent.click(screen.getByRole('button', { name: /^选择角色 1/ }));
  expect(roleName).toBeVisible();
  expect(screen.getByText('点击地图“设置站位”后在地图选择位置。')).toBeVisible();
  const before = socket.sent.filter(value => value.type === 'operation').length;
  expect(screen.getByLabelText('新动作说明')).not.toBeVisible();
  fireEvent.click(screen.getByRole('button', { name: '阶段' }));
  expect(roleName).not.toBeVisible();
  expect(screen.getByLabelText('阶段说明')).toBeVisible();
  doc.getMap<Y.Text>('fields').get('stage.stage.description')!.insert(0, '远端长说明'.repeat(50));
  act(() => socket.message({ type: 'sync', ...authority(doc, 2) }));
  expect(screen.getByRole('region', { name: '阶段地图' })).toBe(map);
  expect(screen.getByLabelText('角色 1 名称')).toBe(roleName);
  expect(socket.sent.filter(value => value.type === 'operation')).toHaveLength(before);
});

it('keeps narrow fine-pointer desktop editing available and opens action tools without a command', async () => {
  const { socket } = await editorWithStages(1, false, true);
  expect(screen.queryByText(/手机端仅查看/)).not.toBeInTheDocument();
  expect(screen.getByLabelText('新动作说明')).toBeVisible();
  expect(screen.getByLabelText('新动作说明')).toBeEnabled();
  fireEvent.click(screen.getByRole('button', { name: /^等待$/ }));
  expect(screen.getByLabelText('动作类型')).toHaveValue('wait');
  expect(socket.sent.filter(value => value.type === 'operation')).toHaveLength(0);
});

it('retains editable draft nodes and only one open drawer when an expanded desktop shrinks', async () => {
  const { socket } = await editorWithStages();
  const stageButton = screen.getByRole('button', { name: '阶段' });
  const description = screen.getByLabelText('新动作说明');
  const map = screen.getByRole('region', { name: '阶段地图' });
  fireEvent.change(description, { target: { value: '缩窄仍保留' } });
  vi.stubGlobal('matchMedia', vi.fn((query: string) => ({
    matches: query === '(any-pointer: fine)' || query === '(max-width: 820px)',
    addEventListener: vi.fn(), removeEventListener: vi.fn(),
  })));
  stageButton.focus();
  fireEvent(window, new Event('resize'));
  expect(stageButton).not.toBeVisible();
  expect(document.activeElement).toBe(screen.getByText('面板'));
  expect(description).toBeVisible(); expect(description).toBeEnabled();
  expect(screen.getByLabelText('新动作说明')).toBe(description);
  expect(description).toHaveValue('缩窄仍保留');
  expect(screen.getByRole('region', { name: '阶段地图' })).toBe(map);
  expect(socket.sent.filter(value => value.type === 'operation')).toHaveLength(0);
});

async function editorWithActions() {
  const value = await editorWithStages();
  const structure = structuredClone(value.doc.getMap('document').get('structure')) as { stages: { actions: object[] }[] };
  structure.stages[0].actions = ['action-a', 'action-b'].map((id, index) => ({ id, kind: 'wait', role_id: 'role', version: 1, group_id: `group-${index}` }));
  value.doc.getMap('document').set('structure', structure);
  for (const id of ['action-a', 'action-b']) {
    const text = new Y.Text(); text.insert(0, id); value.doc.getMap('fields').set(`action.${id}.description`, text);
  }
  act(() => value.socket.message({ type: 'sync', ...authority(value.doc, 2) }));
  return value;
}

it('keeps a new draft submit/cancel reachable when another action or role is clicked', async () => {
  const { socket } = await editorWithActions();
  const input = screen.getByLabelText('新动作说明');
  fireEvent.change(input, { target: { value: '原新增草稿' } });
  fireEvent.click(screen.getByRole('button', { name: /2 · 等待/ }));
  expect(input).toBeVisible(); expect(input).toHaveValue('原新增草稿');
  expect(screen.getByRole('button', { name: '取消动作绘制' })).toBeVisible();
  expect(screen.getByRole('button', { name: '添加到阶段' })).toBeVisible();
  fireEvent.click(screen.getByRole('button', { name: /^选择角色 1/ }));
  expect(input).toBeVisible();
  await waitFor(() => expect(screen.getByText('请先提交或取消当前动作绘制，再切换选择。')).toBeVisible());
  fireEvent.click(screen.getByRole('button', { name: '取消动作绘制' }));
  fireEvent.click(screen.getByRole('button', { name: /2 · 等待/ }));
  expect(screen.getByRole('textbox', { name: '动作说明' })).toHaveValue('action-b');
  expect(socket.sent.filter(value => value.type === 'operation')).toHaveLength(0);
});

it('keeps a leased edit bound to A when B is clicked until that edit is cancelled', async () => {
  const { socket } = await editorWithActions();
  fireEvent.click(screen.getByRole('button', { name: /1 · 等待/ }));
  fireEvent.click(screen.getByRole('button', { name: '修改动作' }));
  const request = socket.sent.at(-1)!;
  await act(async () => socket.message({ type: 'lease.granted', request_id: request.request_id, object_id: 'action:action-a', token: 'fixture', expires_at: Date.now() + 10000, renew_every_ms: 3000 }));
  fireEvent.click(screen.getByRole('button', { name: /2 · 等待/ }));
  expect(screen.getByRole('textbox', { name: '动作说明' })).toHaveValue('action-a');
  expect(screen.getByRole('button', { name: '提交动作修改' })).toBeVisible();
  fireEvent.click(screen.getByRole('button', { name: '取消动作绘制' }));
  fireEvent.click(screen.getByRole('button', { name: /2 · 等待/ }));
  expect(screen.getByRole('textbox', { name: '动作说明' })).toHaveValue('action-b');
  expect(socket.sent.filter(value => value.type === 'operation')).toHaveLength(0);
});

it('opens tactic information outside a collapsed inspector and returns focus to its title trigger', async () => {
  await editorWithStages();
  const field = screen.getByLabelText('战术名称');
  fireEvent.click(screen.getByRole('button', { name: '收起动作与说明' }));
  const title = screen.getByRole('button', { name: 'title' }); title.focus(); fireEvent.click(title);
  const dialog = screen.getByRole('dialog', { name: '战术信息' });
  expect(dialog.closest('aside')).toBeNull(); expect(field).toBeVisible();
  expect(screen.getByLabelText('战术名称')).toBe(field);
  fireEvent.click(screen.getByRole('button', { name: '关闭战术信息' }));
  expect(document.activeElement).toBe(title);
});

it('clears effective action selection on remote removal while retaining an actionable original draft', async () => {
  const { socket, doc } = await editorWithActions();
  fireEvent.click(screen.getByRole('button', { name: /1 · 等待/ }));
  const structure = structuredClone(doc.getMap('document').get('structure')) as { stages: { actions: { id: string }[] }[] };
  structure.stages[0].actions = structure.stages[0].actions.filter(action => action.id !== 'action-a');
  doc.getMap('document').set('structure', structure);
  act(() => socket.message({ type: 'sync', ...authority(doc, 3) }));
  expect(screen.getByText(/原动作已删除，请重新选择动作/)).toBeVisible();
  expect(screen.queryByRole('button', { name: '修改动作' })).not.toBeInTheDocument();
  expect(socket.sent.filter(value => value.type === 'operation')).toHaveLength(0);
});

it('uses a modal delete confirmation, returns focus and refuses a remotely removed target', async () => {
  const { socket, doc } = await editorWithActions();
  fireEvent.click(screen.getByRole('button', { name: /1 · 等待/ }));
  const trigger = screen.getByRole('button', { name: '删除动作' }); trigger.focus(); fireEvent.click(trigger);
  expect(screen.getByRole('dialog', { name: '确认删除' })).toBeVisible();
  fireEvent.click(screen.getByRole('button', { name: '关闭确认删除' }));
  expect(document.activeElement).toBe(trigger);
  fireEvent.click(trigger);
  const structure = structuredClone(doc.getMap('document').get('structure')) as { stages: { actions: { id: string }[] }[] };
  structure.stages[0].actions = structure.stages[0].actions.filter(action => action.id !== 'action-a');
  doc.getMap('document').set('structure', structure);
  act(() => socket.message({ type: 'sync', ...authority(doc, 3) }));
  expect(screen.getByRole('button', { name: '确认删除' })).toBeDisabled();
  expect(screen.getByText('原删除目标已消失，请关闭后重新选择。')).toBeVisible();
  expect(socket.sent.filter(value => value.type === 'operation')).toHaveLength(0);
});

it('does not bind an invalid selected role to the remaining role or write an invalid position', async () => {
  const { socket, doc } = await editorWithStages();
  const structure = structuredClone(doc.getMap('document').get('structure')) as { roles: { id: string; number: number; color: string }[]; stages: { initial_positions: Record<string, { x: number; y: number; version: number }> }[] };
  structure.roles.push({ id: 'other-role', number: 2, color: '#cb5a42' });
  structure.stages[0].initial_positions['other-role'] = { x: 70, y: 70, version: 1 };
  const name = new Y.Text(); name.insert(0, '另一角色'); doc.getMap('fields').set('role.other-role.name', name);
  doc.getMap('document').set('structure', structure);
  act(() => socket.message({ type: 'sync', ...authority(doc, 2) }));
  fireEvent.click(screen.getByRole('button', { name: /^选择角色 1/ }));
  structure.roles.shift(); delete structure.stages[0].initial_positions.role;
  doc.getMap('document').set('structure', structure);
  act(() => socket.message({ type: 'sync', ...authority(doc, 3) }));
  expect(screen.getByText('原角色已删除，请重新选择角色。')).toBeVisible();
  expect(screen.getByLabelText('角色 2 名称')).not.toBeVisible();
  fireEvent.click(screen.getByRole('button', { name: '设置站位' }));
  const image = screen.getByAltText('Mirage 战术地图底图');
  vi.spyOn(image.parentElement!, 'getBoundingClientRect').mockReturnValue({ left: 0, top: 0, width: 100, height: 100, right: 100, bottom: 100, x: 0, y: 0, toJSON: () => ({}) });
  fireEvent.click(image.parentElement!, { clientX: 20, clientY: 20 });
  await waitFor(() => expect(screen.getByText('原角色已删除，请重新选择角色后设置站位。')).toBeVisible());
  expect(socket.sent.filter(value => value.type === 'operation')).toHaveLength(0);
});

it('always shows actual collaboration status and keeps pending content unconfirmed after disconnect', async () => {
  const { socket } = await editorWithStages();
  fireEvent.click(screen.getByText('连接、权限与草稿详情'));
  const summary = screen.getByRole('region', { name: '当前协作状态' });
  expect(within(summary).getByText('已连接')).toBeVisible();
  expect(within(summary).getByText('可编辑')).toBeVisible();
  expect(within(summary).getByText('全部修改已获服务器确认')).toBeVisible();
  const map = screen.getByRole('region', { name: '阶段地图' });
  fireEvent.change(screen.getByLabelText('战术目的'), { target: { value: '仍在等待确认' } });
  expect(within(summary).getByText('1 项待服务器确认')).toBeVisible();
  expect(within(summary).queryByText('全部修改已获服务器确认')).not.toBeInTheDocument();
  act(() => socket.close());
  expect(within(summary).getByText('连接中断')).toBeVisible();
  expect(within(summary).getByText('重连后核对权限')).toBeVisible();
  expect(within(summary).getByText('有待处理的未确认草稿')).toBeVisible();
  expect(within(summary).queryByText('全部修改已获服务器确认')).not.toBeInTheDocument();
  expect(screen.getByRole('region', { name: '阶段地图' })).toBe(map);
});

it('marks retained participants as historical and removes live editing claims after disconnect', async () => {
  const { socket, doc } = await editorWithStages();
  act(() => {
    socket.message({ type: 'participants', participants: [{ connection_id: 'peer', actor_id: 'peer-user', name: '队友甲', stage_id: 'stage', field: 'stage.stage.name' }] });
    socket.message({ type: 'locks', locks: [{ object_id: 'action:one', connection_id: 'peer' }] });
  });
  expect(screen.getByText('1 在线')).toBeVisible();
  expect(screen.getByRole('heading', { name: '在线成员' })).toBeVisible();
  expect(screen.getByText('队友甲 正在编辑')).toBeInTheDocument();
  expect(screen.getByText('动作正在被编辑')).toBeVisible();
  const map = screen.getByRole('region', { name: '阶段地图' });
  const slot = screen.getByText('1 在线');
  fireEvent.change(screen.getByLabelText('新动作说明'), { target: { value: '断线前尚未提交' } });
  act(() => socket.close());
  expect(slot).toHaveTextContent('未连接');
  expect(screen.queryByText('1 在线')).not.toBeInTheDocument();
  expect(screen.getByRole('heading', { name: '最近连接成员（非实时）' })).toBeVisible();
  expect(screen.getByText(/队友甲 · 阶段/)).toBeVisible();
  expect(screen.queryByText('队友甲 正在编辑')).not.toBeInTheDocument();
  expect(screen.queryByText('动作正在被编辑')).not.toBeInTheDocument();
  expect(screen.getByText('未连接，暂时无法确认对象占用。')).toBeVisible();
  expect(screen.getByLabelText('新动作说明')).toHaveValue('断线前尚未提交');
  expect(screen.getByRole('region', { name: '阶段地图' })).toBe(map);
  fireEvent.click(screen.getByRole('button', { name: '重新连接' }));
  await waitFor(() => expect(Socket.instances).toHaveLength(2));
  expect(slot).toHaveTextContent('未连接');
  act(() => {
    const next = Socket.instances[1];
    next.message(auth(false, 'window-reconnected'));
    next.message({ type: 'sync', ...authority(doc), participants: [{ connection_id: 'fresh-peer', actor_id: 'other-user', name: '队友乙', stage_id: 'stage' }] });
  });
  expect(slot).toHaveTextContent('1 在线');
  expect(screen.getByRole('heading', { name: '在线成员' })).toBeVisible();
  expect(screen.getByText(/队友乙 · 阶段/)).toBeVisible();
  expect(screen.queryByText(/队友甲 · 阶段/)).not.toBeInTheDocument();
  expect(screen.getByLabelText('新动作说明')).toHaveValue('断线前尚未提交');
});

it('summarizes a server-readonly connection without claiming editing is permitted', async () => {
  const { socket } = await editorWithStages(1, false, false, true);
  fireEvent.click(screen.getByText('连接、权限与草稿详情'));
  const summary = screen.getByRole('region', { name: '当前协作状态' });
  expect(within(summary).getByText('已连接')).toBeVisible();
  expect(within(summary).getByText('仅可查看')).toBeVisible();
  expect(within(summary).queryByText('可编辑')).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole('button', { name: /^选择角色 1/ }));
  expect(screen.getByLabelText('角色 1 名称')).toBeDisabled();
  expect(screen.getByText('站位：50.0 / 50.0')).toBeVisible();
  expect(screen.queryByText('点击地图“设置站位”后在地图选择位置。')).not.toBeInTheDocument();
  expect(screen.queryByRole('button', { name: '设置站位' })).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole('button', { name: '筛选此角色' }));
  expect(screen.getByLabelText('按角色查看')).toHaveValue('role');
  fireEvent.click(screen.getByRole('button', { name: '查看全部角色' }));
  expect(screen.getByLabelText('按角色查看')).toHaveValue('');
  expect(socket.sent.some(message => message.type === 'operation')).toBe(false);
});

it('keeps automatic error details dismissible and mutually exclusive with other toolbar menus', async () => {
  await editorWithActions();
  fireEvent.change(screen.getByLabelText('新动作说明'), { target: { value: '草稿' } });
  fireEvent.click(screen.getByRole('button', { name: /2 · 等待/ }));
  const status = screen.getByText('连接、权限与草稿详情').closest('details')!;
  const panels = screen.getByText('面板').closest('details')!;
  const documentMenu = screen.getByText('文档、历史与分享').closest('details')!;
  await waitFor(() => expect(status).toHaveAttribute('open'));
  expect(status.querySelector('.editor-menu-body')).toBeVisible();
  expect(documentMenu.querySelector('.editor-menu-body')).not.toBeVisible();
  fireEvent.click(screen.getByText('面板'));
  expect(status).not.toHaveAttribute('open'); expect(panels).toHaveAttribute('open');
  fireEvent.click(screen.getByText('文档、历史与分享'));
  expect(panels).not.toHaveAttribute('open'); expect(documentMenu).toHaveAttribute('open');
  expect(documentMenu.querySelector('.editor-menu-body')).toBeVisible();
  expect(status.querySelector('.editor-menu-body')).not.toBeVisible();
  fireEvent.click(screen.getByRole('button', { name: '关闭文档菜单' }));
  expect(documentMenu).not.toHaveAttribute('open');
  expect(documentMenu.querySelector('.editor-menu-body')).not.toBeVisible();
  expect(document.activeElement).toBe(screen.getByText('文档、历史与分享'));
  await act(async () => {});
  expect(status).not.toHaveAttribute('open');
});

it('moves focus into an opened inspector and returns it to the reachable panel summary on close', async () => {
  await editorWithStages();
  fireEvent.click(screen.getByRole('button', { name: '收起属性' }));
  await waitFor(() => expect(document.activeElement).toBe(screen.getByText('面板')));
  fireEvent.click(screen.getByText('面板'));
  fireEvent.click(screen.getByRole('button', { name: '展开动作与说明' }));
  await waitFor(() => expect(document.activeElement).toBe(screen.getByRole('button', { name: '收起属性' })));
  expect(screen.getByText('面板').closest('details')).not.toHaveAttribute('open');
});

it('uses the original stage action index after filtering and exposes saved metadata without taking a lease', async () => {
  const { socket, doc } = await editorWithActions();
  const structure = structuredClone(doc.getMap('document').get('structure')) as { stages: { actions: { role_id: string }[] }[] };
  structure.stages[0].actions[0].role_id = 'not-selected';
  doc.getMap('document').set('structure', structure);
  act(() => socket.message({ type: 'sync', ...authority(doc, 3) }));
  fireEvent.change(screen.getByLabelText('按角色查看'), { target: { value: 'role' } });
  fireEvent.click(screen.getByRole('button', { name: /2 · 等待/ }));
  expect(screen.getByRole('button', { name: '动作上移' })).toBeEnabled();
  const card = screen.getByRole('textbox', { name: '动作说明' }).closest('article')!;
  expect(card).toHaveTextContent('执行角色角色'); expect(card).toHaveTextContent('执行关系接着执行');
  expect(socket.sent.some(value => value.type === 'lease.acquire' || value.type === 'operation')).toBe(false);
  act(() => socket.message(auth(true)));
  expect(card).toHaveTextContent('执行角色角色');
  expect(screen.getByRole('button', { name: '修改动作' })).toBeDisabled();
});

function setFullscreenSurface(surface: HTMLElement | null) {
  Object.defineProperty(document, 'fullscreenElement', { configurable: true, value: surface });
  document.dispatchEvent(new Event('fullscreenchange'));
}

it('submits the same prepared action from fullscreen without duplicating its description input', async () => {
  const { socket } = await editorWithStages();
  const description = screen.getByLabelText('新动作说明');
  fireEvent.change(screen.getByLabelText('动作类型'), { target: { value: 'wait' } });
  fireEvent.change(description, { target: { value: '全屏等待说明' } });
  const surface = screen.getByRole('region', { name: '阶段地图' });
  try {
    act(() => setFullscreenSurface(surface));
    expect(screen.getByRole('region', { name: '全屏动作绘制' })).toBeVisible();
    expect(screen.getAllByLabelText('新动作说明')).toHaveLength(1);
    expect(screen.getByLabelText('新动作说明')).toBe(description);
    fireEvent.click(screen.getByRole('button', { name: '提交当前动作' }));
    const sent = socket.sent.find(value => value.type === 'operation')!.operation as { payload: { command: Record<string, unknown> } };
    expect(sent.payload.command).toMatchObject({ type: 'action.add', kind: 'wait', stage_id: 'stage', role_id: 'role', description: '全屏等待说明' });
    expect(description).toHaveValue('全屏等待说明');
    expect(screen.getByRole('button', { name: '提交当前动作' })).toBeDisabled();
  } finally { act(() => setFullscreenSurface(null)); }
});

it('cancels only the fullscreen drawing while keeping the original text fields mounted', async () => {
  const { socket } = await editorWithStages();
  const description = screen.getByLabelText('新动作说明');
  fireEvent.change(description, { target: { value: '只取消本次绘制' } });
  const title = screen.getByLabelText('战术名称');
  try {
    act(() => setFullscreenSurface(screen.getByRole('region', { name: '阶段地图' })));
    fireEvent.click(screen.getByRole('button', { name: '取消当前绘制' }));
    expect(description).toHaveValue(''); expect(title).toHaveValue('title');
    expect(screen.getByLabelText('战术名称')).toBe(title);
    expect(socket.sent.some(value => value.type === 'operation')).toBe(false);
  } finally { act(() => setFullscreenSurface(null)); }
});

it('keeps the map and camera layer on failed image retry without changing drawing or sending a write', async () => {
  const { socket } = await editorWithStages();
  const image = screen.getByAltText('Mirage 战术地图底图');
  const surface = screen.getByRole('region', { name: '阶段地图' });
  const layer = image.parentElement!;
  const description = screen.getByLabelText('新动作说明');
  fireEvent.change(description, { target: { value: '地图失败仍保留' } });
  fireEvent.change(screen.getByRole('slider'), { target: { value: '2' } });
  const transform = layer.style.transform;
  fireEvent.error(image);
  expect(screen.getByText('地图加载失败，当前位置与绘制仍保留。')).toBeVisible();
  fireEvent.click(screen.getByRole('button', { name: '重试地图' }));
  expect(screen.getByAltText('Mirage 战术地图底图')).toBe(image);
  expect(image).toHaveAttribute('src', '/assets/maps/mirage.svg?retry=1');
  fireEvent.load(image);
  expect(screen.queryByText('正在加载地图…')).not.toBeInTheDocument();
  expect(screen.getByRole('region', { name: '阶段地图' })).toBe(surface);
  expect(layer.style.transform).toBe(transform); expect(description).toHaveValue('地图失败仍保留');
  expect(socket.sent.some(value => value.type === 'operation')).toBe(false);
});

it('reserves the editor loading workspace before authoritative projection arrives', async () => {
  vi.stubGlobal('matchMedia', vi.fn(() => ({ matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn() })));
  render(<MemoryRouter><TacticEditor tacticId="tactic" session={{ user: { id: 'u', username: 'user', display_name: '玩家' }, csrf_token: 'csrf' }} /></MemoryRouter>);
  await screen.findByText('正在准备地图与编辑区域…');
  await waitFor(() => expect(Socket.instances).toHaveLength(1));
  expect(screen.getByLabelText('地图加载占位').closest('section')).toHaveAttribute('aria-busy', 'true');
  expect(screen.queryByRole('button', { name: '新增动作' })).not.toBeInTheDocument();
});

it('keeps a known same-role parallel addition editable without sending it or entering recovery', async () => {
  const { socket } = await editorWithActions();
  fireEvent.change(screen.getByLabelText('动作类型'), { target: { value: 'wait' } });
  fireEvent.change(screen.getByLabelText('新动作说明'), { target: { value: '保留并行草稿' } });
  fireEvent.change(screen.getByLabelText('与上一动作'), { target: { value: 'parallel' } });
  fireEvent.click(screen.getByRole('button', { name: '添加到阶段' }));
  await waitFor(() => expect(screen.getByText(/“角色”在同一并行编组已有动作/)).toBeVisible());
  expect(socket.sent.some(value => value.type === 'operation')).toBe(false);
  expect(screen.getByLabelText('新动作说明')).toHaveValue('保留并行草稿');
  expect(screen.getByLabelText('新动作说明')).toBeEnabled();
  expect(screen.queryByRole('button', { name: '按原操作重试' })).not.toBeInTheDocument();
  fireEvent.change(screen.getByLabelText('与上一动作'), { target: { value: 'after' } });
  fireEvent.click(screen.getByRole('button', { name: '添加到阶段' }));
  expect(socket.sent.filter(value => value.type === 'operation')).toHaveLength(1);
});

it('rejects a known conflicting regroup before lease/operation while leaving other commands available', async () => {
  const { socket } = await editorWithActions();
  fireEvent.click(screen.getByRole('button', { name: /2 · 等待/ }));
  fireEvent.change(screen.getByRole('combobox', { name: '动作编组' }), { target: { value: '0' } });
  await waitFor(() => expect(screen.getByText(/“角色”在同一并行编组已有动作/)).toBeVisible());
  expect(socket.sent.some(value => value.type === 'operation' || value.type === 'lease.acquire')).toBe(false);
  expect(screen.getByRole('button', { name: '修改动作' })).toBeEnabled();
});

it('retains a leased edit when changing its actor would conflict and allows correcting that actor', async () => {
  const { socket, doc } = await editorWithActions();
  const structure = structuredClone(doc.getMap('document').get('structure')) as { roles: { id: string; number: number; color: string }[]; stages: { actions: { group_id: string; role_id: string }[]; initial_positions: Record<string, { x: number; y: number; version: number }> }[] };
  structure.roles.push({ id: 'r2', number: 2, color: '#315d57' });
  structure.stages[0].initial_positions.r2 = { x: 70, y: 70, version: 1 };
  structure.stages[0].actions[1].group_id = structure.stages[0].actions[0].group_id;
  structure.stages[0].actions[1].role_id = 'r2';
  const name = new Y.Text(); name.insert(0, '支援'); doc.getMap('fields').set('role.r2.name', name);
  doc.getMap('document').set('structure', structure);
  act(() => socket.message({ type: 'sync', ...authority(doc, 3) }));
  fireEvent.click(screen.getByRole('button', { name: /2 · 等待/ }));
  fireEvent.click(screen.getByRole('button', { name: '修改动作' }));
  const request = socket.sent.at(-1)!;
  await act(async () => socket.message({ type: 'lease.granted', request_id: request.request_id, object_id: 'action:action-b', token: 'fixture', expires_at: Date.now() + 10000, renew_every_ms: 3000 }));
  fireEvent.change(screen.getByLabelText('执行角色'), { target: { value: 'role' } });
  fireEvent.click(screen.getByRole('button', { name: '提交动作修改' }));
  await waitFor(() => expect(screen.getByText(/“角色”在同一并行编组已有动作/)).toBeVisible());
  expect(screen.getByLabelText('执行角色')).toBeEnabled();
  expect(screen.getByRole('button', { name: '取消动作绘制' })).toBeVisible();
  expect(socket.sent.some(value => value.type === 'operation')).toBe(false);
  fireEvent.change(screen.getByLabelText('执行角色'), { target: { value: 'r2' } });
  fireEvent.click(screen.getByRole('button', { name: '提交动作修改' }));
  await waitFor(() => expect(socket.sent.filter(value => value.type === 'operation')).toHaveLength(1));
});

it('uses verified team origin in the live editor toolbar while retaining unconfirmed navigation protection', async () => {
  const fetcher = vi.fn(async (raw: RequestInfo | URL) => {
    const path = String(raw);
    const value = path.endsWith('/collaboration-ticket') ? { ticket: 'business-ticket' }
      : path.endsWith('/notebooks/personal') ? { notebook: { id: 'personal', kind: 'personal' } }
        : path.includes('/teams?') ? { items: [{ id: 'actual-team', name: '当前队伍', notebook_id: 'shared' }], next_cursor: null }
          : { tactic: { id: 'tactic', notebook_id: 'shared', source: null }, permissions: { manage: false, publish: false } };
    return { ok: true, status: 200, headers: new Headers(), json: async () => value };
  }); vi.stubGlobal('fetch', fetcher);
  const { socket } = await editorWithStages();
  const back = await screen.findByRole('link', { name: '返回队伍：当前队伍' });
  expect(back).toHaveAttribute('href', '/teams/actual-team');
  const map = screen.getByRole('region', { name: '阶段地图' });
  fireEvent.change(screen.getByLabelText('新动作说明'), { target: { value: '未确认草稿' } });
  fireEvent.click(back);
  await waitFor(() => expect(screen.getByText('有未确认内容，请先保存、导出或明确放弃后再离开。')).toBeVisible());
  expect(screen.getByRole('region', { name: '阶段地图' })).toBe(map);
  expect(Socket.instances).toHaveLength(1);
  expect(socket.sent.some(value => value.type === 'operation')).toBe(false);
  // The two preexisting detail reads are attribution + publication, never a third origin read.
  expect(fetcher.mock.calls.filter(call => String(call[0]).endsWith('/tactics/tactic'))).toHaveLength(2);
});

it.each(['rejected', 'timeout'] as const)('retains the first recovery projection and local copy through retry without ACK and a later %s', async failure => {
  vi.useFakeTimers();
  const { client, socket, doc } = await connected();
  const structure = structuredClone(doc.getMap('document').get('structure')) as { stages: { id: string }[] };
  structure.stages.push({ ...structuredClone(structure.stages[0]), id: 'surviving-stage' });
  for (const suffix of ['name', 'description', 'condition']) {
    const text = new Y.Text(); if (suffix === 'name') text.insert(0, '保留阶段');
    doc.getMap('fields').set(`stage.surviving-stage.${suffix}`, text);
  }
  doc.getMap('document').set('structure', structure);
  socket.message({ type: 'sync', ...authority(doc, 2) });
  const field = 'stage.stage.description';
  client.doc.transact(() => client.doc.getMap<Y.Text>('fields').get(field)!.insert(0, 'UNCONFIRMED_ORIGINAL_TEXT'), 'local-ui');
  client.setLocalDraft({ field, value: 'LOCAL_COMPOSITION_COPY', composing: true }, field);
  const original = socket.sent.find(message => message.type === 'operation')!;
  const operationId = (original.operation as { operation_id: string }).operation_id;
  socket.message({ type: 'operation.rejected', operation_id: operationId, code: 'TACTIC_CONFLICT' });
  const first = structuredClone(client.recoveryExport().recovery_copy) as { projection: { fields: Record<string, string> }; local: unknown };
  const pending = structuredClone(client.recoveryExport().pending_operations);
  expect(first.projection.fields[field]).toBe('UNCONFIRMED_ORIGINAL_TEXT');
  expect(first.local).toEqual({ [field]: { field, value: 'LOCAL_COMPOSITION_COPY', composing: true } });
  structure.stages = structure.stages.filter(stage => stage.id !== 'stage');
  doc.getMap('document').set('structure', structure);
  for (const suffix of ['name', 'description', 'condition']) doc.getMap('fields').delete(`stage.stage.${suffix}`);
  socket.message({ type: 'sync', ...authority(doc, 3) });
  socket.message({ type: 'sync', ...authority(doc, 3) });
  expect(client.state.projection!.fields[field]).toBeUndefined();
  expect(client.recoveryExport().recovery_copy).toEqual(first);
  client.retry();
  const retryExport = client.recoveryExport();
  expect(retryExport.pending_operations).toEqual(pending);
  expect(socket.sent.filter(message => message.type === 'operation').at(-1)).toEqual(original);
  expect((retryExport.recovery_copy as typeof first).local).toEqual(first.local);
  expect.soft((retryExport.recovery_copy as typeof first).projection.fields[field], 'before a successful ACK the original readable recovery text must remain').toBe('UNCONFIRMED_ORIGINAL_TEXT');
  if (failure === 'rejected') socket.message({ type: 'operation.rejected', operation_id: operationId, code: 'COLLAB_UNAVAILABLE' });
  else vi.advanceTimersByTime(11001);
  const failedExport = client.recoveryExport();
  expect(client.state.recovery).toBe(true);
  expect(failedExport.pending_operations).toEqual(pending);
  expect((failedExport.recovery_copy as typeof first).local).toEqual(first.local);
  expect.soft((failedExport.recovery_copy as typeof first).projection.fields[field], 'retry failure must not replace the first recovery text with the remotely deleted projection').toBe('UNCONFIRMED_ORIGINAL_TEXT');
  expect.soft(failedExport.recovery_copy, 'the first recovery copy remains immutable until confirmed success or explicit discard').toEqual(first);
});

function applyOwnTextReceipt(socket: Socket, doc: Y.Doc, operation: { operation_id: string; payload: Record<string, unknown> }, revision: number) {
  Y.applyUpdate(doc, decodeBytes(String(operation.payload.update)));
  socket.message({ type: 'operation.applied', ...authority(doc, revision), operation_id: operation.operation_id, connection_id: 'window-a', actor_id: 'same-account', operation_type: 'text.update' });
}

it('keeps recovery through a partial ACK, clears after all pending are confirmed and captures fresh data on the next failure', async () => {
  vi.useFakeTimers();
  const { client, socket, doc } = await connected();
  const text = client.doc.getMap<Y.Text>('fields').get('tactic.purpose')!;
  client.doc.transact(() => text.insert(text.length, '_first'), 'local-ui');
  client.doc.transact(() => text.insert(text.length, '_second'), 'local-ui');
  const operations = structuredClone(client.recoveryExport().pending_operations);
  expect(operations).toHaveLength(2);
  socket.message({ type: 'operation.rejected', operation_id: operations[0].operation_id, code: 'TACTIC_CONFLICT' });
  const first = structuredClone(client.recoveryExport().recovery_copy);
  client.retry();
  applyOwnTextReceipt(socket, doc, operations[0], 2);
  expect(client.state.pending).toBe(1);
  expect(client.recoveryExport().recovery_copy).toEqual(first);
  expect(client.hasUnconfirmedWork()).toBe(true);
  applyOwnTextReceipt(socket, doc, operations[1], 3);
  expect(client.state.pending).toBe(0); expect(client.hasUnconfirmedWork()).toBe(false);
  expect(client.recoveryExport().recovery_copy).not.toHaveProperty('operations');
  client.doc.transact(() => text.insert(text.length, '_NEW_FAILURE'), 'local-ui');
  const next = client.recoveryExport().pending_operations[0];
  socket.message({ type: 'operation.rejected', operation_id: next.operation_id, code: 'COLLAB_UNAVAILABLE' });
  const fresh = client.recoveryExport().recovery_copy as { confirmed_revision: number; projection: { fields: Record<string, string> } };
  expect(fresh.confirmed_revision).toBe(3);
  expect(fresh.projection.fields['tactic.purpose']).toContain('_NEW_FAILURE');
  expect(fresh).not.toEqual(first);
});

it('retains recovery until local work finishes after ACK and does not let a newer local draft reuse that ACK', async () => {
  const { client, socket, doc } = await connected();
  client.doc.transact(() => client.doc.getMap<Y.Text>('fields').get('tactic.purpose')!.insert(0, 'own'), 'local-ui');
  client.setLocalDraft({ form: 'ORIGINAL_LOCAL' });
  const operation = client.recoveryExport().pending_operations[0];
  socket.message({ type: 'operation.rejected', operation_id: operation.operation_id, code: 'TACTIC_CONFLICT' });
  const first = structuredClone(client.recoveryExport().recovery_copy);
  client.retry(); applyOwnTextReceipt(socket, doc, operation, 2);
  expect(client.recoveryExport().recovery_copy).toEqual(first);
  client.setLocalDraft({ form: 'NEWER_LOCAL' });
  client.setLocalDraft(null);
  expect(client.recoveryExport().recovery_copy).toEqual(first);
  expect(client.hasUnconfirmedWork()).toBe(true);
  client.doc.transact(() => client.doc.getMap<Y.Text>('fields').get('tactic.purpose')!.insert(0, 'confirmed-next'), 'local-ui');
  applyOwnTextReceipt(socket, doc, client.recoveryExport().pending_operations[0], 3);
  expect(client.hasUnconfirmedWork()).toBe(false);
  expect(client.recoveryExport().recovery_copy).not.toHaveProperty('operations');
});

it('does not treat clearing a local draft as an ACK and exports newer local work separately from the first snapshot', async () => {
  const { client, socket } = await connected();
  client.setLocalDraft({ form: 'ORIGINAL_LOCAL' }); socket.close();
  const first = structuredClone(client.recoveryExport().recovery_copy);
  client.setLocalDraft(null);
  expect(client.recoveryExport().recovery_copy).toEqual(first);
  expect(client.hasUnconfirmedWork()).toBe(true);
  client.setLocalDraft({ form: 'LATEST_LOCAL_AFTER_PRESERVE' });
  const exported = client.recoveryExport();
  expect(exported.recovery_copy).toEqual(first);
  expect(exported.current_local_draft).toEqual({ form: { form: 'LATEST_LOCAL_AFTER_PRESERVE' } });
  expect(JSON.stringify(exported)).toContain('LATEST_LOCAL_AFTER_PRESERVE');
});

it('clears a retained recovery after an ACK followed by completion of the unchanged local draft', async () => {
  const { client, socket, doc } = await connected();
  client.doc.transact(() => client.doc.getMap<Y.Text>('fields').get('tactic.purpose')!.insert(0, 'own'), 'local-ui');
  client.setLocalDraft({ form: 'WAITING_TO_FINISH' });
  const operation = client.recoveryExport().pending_operations[0];
  socket.message({ type: 'operation.rejected', operation_id: operation.operation_id, code: 'TACTIC_CONFLICT' });
  client.retry(); applyOwnTextReceipt(socket, doc, operation, 2);
  expect(client.hasUnconfirmedWork()).toBe(true);
  client.setLocalDraft(null);
  expect(client.hasUnconfirmedWork()).toBe(false);
  expect(client.recoveryExport().recovery_copy).not.toHaveProperty('operations');
});

it('does not treat a later unrelated ACK as confirmation of an original local-only recovery and requires explicit discard', async () => {
  const { client, socket, doc } = await connected();
  client.setLocalDraft({ form: 'NEVER_SUBMITTED' }); socket.close();
  const first = structuredClone(client.recoveryExport().recovery_copy);
  client.setLocalDraft(null);
  await client.connect();
  const next = Socket.instances.at(-1)!;
  next.dispatchEvent(new Event('open')); next.message(auth()); next.message({ type: 'sync', ...authority(doc) });
  client.retry();
  client.doc.transact(() => client.doc.getMap<Y.Text>('fields').get('tactic.purpose')!.insert(0, 'unrelated'), 'local-ui');
  applyOwnTextReceipt(next, doc, client.recoveryExport().pending_operations[0], 2);
  expect(client.recoveryExport().recovery_copy).toEqual(first);
  expect(client.hasUnconfirmedWork()).toBe(true);
  await client.discard();
  expect(client.hasUnconfirmedWork()).toBe(false);
  expect(client.recoveryExport().recovery_copy).not.toHaveProperty('operations');
});

it('adds current local form and IME data without replacing first local data or exporting client credentials', async () => {
  const { client, socket } = await connected();
  expect(client.recoveryExport().current_local_draft).toBeNull();
  const field = 'stage.stage.description';
  client.setLocalDraft({ field, value: '首份组合文字', composing: true }, field);
  client.setLocalDraft({ stage_id: 'stage', action_form: { description: '动作草稿', points: [{ x: 1, y: 2 }] } });
  socket.close();
  const first = structuredClone(client.recoveryExport().recovery_copy);
  const originalPending = structuredClone(client.recoveryExport().pending_operations);
  client.setLocalDraft({ field, value: '最新组合尾字', composing: true }, field);
  const exported = client.recoveryExport();
  expect(exported.kind).toBe('smokeline-formal-recovery'); expect(exported.document_id).toBe('tactic');
  expect(exported.confirmed_revision).toBe(1); expect(exported.pending_operations).toEqual(originalPending);
  expect(exported.recovery_copy).toEqual(first);
  expect(exported.current_local_draft).toEqual({
    [field]: { field, value: '最新组合尾字', composing: true },
    form: { stage_id: 'stage', action_form: { description: '动作草稿', points: [{ x: 1, y: 2 }] } },
  });
  expect(JSON.stringify(exported.current_local_draft)).not.toMatch(/business-ticket|"csrf"|"session"|"token"|memory-only-fixture/);
  client.setLocalDraft(null, field); client.setLocalDraft(null);
  expect(client.recoveryExport().current_local_draft).toBeNull();
  expect(client.recoveryExport().recovery_copy).toEqual(first);
});
