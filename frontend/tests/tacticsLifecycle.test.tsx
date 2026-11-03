import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { StrictMode } from 'react';
import { createMemoryRouter, MemoryRouter, RouterProvider } from 'react-router-dom';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import * as Y from 'yjs';
import TacticEditor from '../src/tactics/TacticEditor';
import { TacticCollaborationClient, encodeBytes, projectDocument } from '../src/tactics/collaborationClient';

class Socket extends EventTarget {
  static OPEN = 1;
  static instances: Socket[] = [];
  readyState = 1;
  sent: object[] = [];
  constructor() { super(); Socket.instances.push(this); }
  send(data: string) { this.sent.push(JSON.parse(data)); }
  close() { this.readyState = 3; this.dispatchEvent(new Event('close')); }
  message(data: object) { this.dispatchEvent(new MessageEvent('message', { data: JSON.stringify(data) })); }
}
const session = { user: { id: 'user', username: 'user', display_name: '玩家' }, csrf_token: 'csrf' };
const clients: TacticCollaborationClient[] = [];
const docs: Y.Doc[] = [];
function response() { return { ok: true, status: 201, headers: new Headers(), json: async () => ({ ticket: 'fixture' }) }; }
function sync(socket: Socket, id: string) {
  const doc = new Y.Doc(); docs.push(doc);
  doc.getMap('document').set('structure', { schema_kind: 'stage-board', schema_version: 2, document_id: id, map: 'mirage', side: 'T', roles: [{ id: 'role', number: 1, color: '#cb5a42' }], stages: [{ id: 'stage', version: 1, initial_positions: { role: { x: 50, y: 50, version: 1 } }, actions: [], lineup_ids: [] }] });
  for (const [key, value] of Object.entries({ 'tactic.title': `文档${id}`, 'tactic.purpose': '', 'tactic.applicability': '', 'stage.stage.name': '阶段', 'stage.stage.description': '', 'stage.stage.condition': '', 'role.role.name': '角色' })) {
    const text = new Y.Text(); if (value) text.insert(0, value); doc.getMap('fields').set(key, text);
  }
  socket.message({ type: 'auth.ok', actor_id: 'actor', connection_id: id, role: 'editor', read_only: false });
  socket.message({ type: 'sync', document_id: id, epoch: 'epoch', revision: 1, state: encodeBytes(Y.encodeStateAsUpdate(doc)), projection: projectDocument(doc), recoveries: [], participants: [], locks: [] });
}
beforeEach(() => {
  Socket.instances = [];
  vi.stubGlobal('WebSocket', Socket);
  vi.stubGlobal('fetch', vi.fn(async () => response()));
  vi.stubGlobal('matchMedia', vi.fn(() => ({ matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn() })));
});
afterEach(() => { clients.splice(0).forEach(client => client.dispose()); docs.splice(0).forEach(doc => doc.destroy()); vi.restoreAllMocks(); vi.unstubAllGlobals(); });

it('never mounts the previous document fields during a direct document or credential-scope change', async () => {
  const view = render(<MemoryRouter><TacticEditor tacticId="first" session={session} /></MemoryRouter>);
  await waitFor(() => expect(Socket.instances).toHaveLength(1));
  const old = Socket.instances[0]; act(() => sync(old, 'first'));
  expect(await screen.findByRole('button', { name: '文档first' })).toBeVisible();
  view.rerender(<MemoryRouter><TacticEditor tacticId="second" session={session} /></MemoryRouter>);
  expect(screen.queryByRole('button', { name: '文档first' })).not.toBeInTheDocument();
  expect(screen.getByText('正在准备战术编辑器…')).toBeVisible();
  expect(old.readyState).toBe(3);
  await waitFor(() => expect(Socket.instances).toHaveLength(2));
  act(() => sync(Socket.instances[1], 'second'));
  expect(await screen.findByRole('button', { name: '文档second' })).toBeVisible();
  act(() => sync(old, 'first'));
  expect(screen.queryByRole('button', { name: '文档first' })).not.toBeInTheDocument();
  expect(screen.getByRole('button', { name: '文档second' })).toBeVisible();
  view.rerender(<MemoryRouter><TacticEditor tacticId="second" session={{ ...session, csrf_token: 'rotated' }} /></MemoryRouter>);
  expect(screen.queryByRole('button', { name: '文档second' })).not.toBeInTheDocument();
  expect(screen.getByText('正在准备战术编辑器…')).toBeVisible();
  view.unmount();
});

it('creates only the live StrictMode client and ignores a ticket arriving after unmount', async () => {
  let resolve: (value: ReturnType<typeof response>) => void = () => {};
  const fetcher = vi.fn(() => new Promise<ReturnType<typeof response>>(done => { resolve = done; }));
  vi.stubGlobal('fetch', fetcher);
  const view = render(<StrictMode><MemoryRouter><TacticEditor tacticId="first" session={session} /></MemoryRouter></StrictMode>);
  await waitFor(() => expect(fetcher).toHaveBeenCalledTimes(1));
  view.unmount();
  await act(async () => resolve(response()));
  expect(Socket.instances).toHaveLength(0);
});

it('ignores an older reconnect ticket and all events from replaced or disposed sockets', async () => {
  const resolvers: ((value: ReturnType<typeof response>) => void)[] = [];
  vi.stubGlobal('fetch', vi.fn(() => new Promise<ReturnType<typeof response>>(done => resolvers.push(done))));
  const client = new TacticCollaborationClient('first', 'csrf'); clients.push(client);
  const early = client.connect(), latest = client.connect();
  resolvers[1](response()); await latest;
  resolvers[0](response()); await early;
  expect(Socket.instances).toHaveLength(1);
  const old = Socket.instances[0]; sync(old, 'first');
  const reconnect = client.connect(); resolvers[2](response()); await reconnect;
  const current = Socket.instances[1]; sync(current, 'first');
  const notify = vi.fn(); const unsubscribe = client.subscribe(notify);
  const currentSnapshot = client.snapshot();
  old.dispatchEvent(new Event('open')); old.dispatchEvent(new Event('error')); old.message({ type: 'bad' });
  sync(old, 'first');
  expect(client.snapshot()).toBe(currentSnapshot);
  expect(notify).not.toHaveBeenCalled(); expect(old.sent).toHaveLength(0);
  unsubscribe(); client.dispose();
  const snapshot = client.snapshot();
  current.dispatchEvent(new Event('open')); current.dispatchEvent(new Event('error')); current.message({ type: 'bad' });
  expect(client.snapshot()).toBe(snapshot); expect(notify).not.toHaveBeenCalled();
});

it('blocks a real document route change while an action form is unconfirmed and keeps its original connection and text', async () => {
  const router = createMemoryRouter([
    { path: '/first', element: <TacticEditor tacticId="first" session={session} /> },
    { path: '/second', element: <TacticEditor tacticId="second" session={session} /> },
  ], { initialEntries: ['/first'] });
  const view = render(<RouterProvider router={router} />);
  try {
    await waitFor(() => expect(Socket.instances).toHaveLength(1));
    const socket = Socket.instances[0]; act(() => sync(socket, 'first'));
    fireEvent.click(await screen.findByRole('button', { name: '新增动作' }));
    fireEvent.change(screen.getByLabelText('新动作说明'), { target: { value: '尚未确认的本地动作' } });
    await act(async () => { await router.navigate('/second'); });
    expect(screen.getByRole('alertdialog', { name: '未确认内容离页保护' })).toBeVisible();
    expect(router.state.location.pathname).toBe('/first');
    expect(Socket.instances).toHaveLength(1); expect(socket.readyState).toBe(Socket.OPEN);
    fireEvent.click(screen.getByRole('button', { name: '返回继续编辑' }));
    expect(screen.getByLabelText('新动作说明')).toHaveValue('尚未确认的本地动作');
    expect(screen.queryByRole('alertdialog', { name: '未确认内容离页保护' })).not.toBeInTheDocument();
  } finally { view.unmount(); router.dispose(); }
});
