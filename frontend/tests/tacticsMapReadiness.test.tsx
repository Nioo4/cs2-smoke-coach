import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import TacticMap from '../src/tactics/TacticMap';
import type { TacticProjection } from '../src/tactics/editorTypes';
import { MemoryRouter } from 'react-router-dom';
import * as Y from 'yjs';
import TacticEditor from '../src/tactics/TacticEditor';
import { encodeBytes, projectDocument } from '../src/tactics/collaborationClient';

const projection: TacticProjection = {
  structure: { schema_kind: 'stage-board', schema_version: 2, document_id: 'fixture', map: 'mirage', side: 'T', roles: [{ id: 'role', number: 1, color: '#cb5a42' }], stages: [{ id: 'stage', version: 1, initial_positions: { role: { x: 10, y: 20, version: 1 } }, actions: [], lineup_ids: [] }] },
  fields: { 'stage.stage.name': '加载边界', 'role.role.name': '角色' },
};
let fullscreenDescriptor: PropertyDescriptor | undefined;
beforeEach(() => {
  fullscreenDescriptor = Object.getOwnPropertyDescriptor(document, 'fullscreenElement');
  vi.stubGlobal('matchMedia', vi.fn(() => ({ matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn() })));
  vi.stubGlobal('PointerEvent', MouseEvent);
});
afterEach(() => {
  vi.restoreAllMocks(); vi.unstubAllGlobals();
  if (fullscreenDescriptor) Object.defineProperty(document, 'fullscreenElement', fullscreenDescriptor);
  else Reflect.deleteProperty(document, 'fullscreenElement');
});
function mount() {
  const point = vi.fn(), tool = vi.fn(), start = vi.fn(async () => true), move = vi.fn(), end = vi.fn();
  const rendered = render(<TacticMap projection={projection} stageId="stage" readOnly={false} onMapPoint={point} onActionTool={tool} onRoleDragStart={start} onRoleDragMove={move} onRoleDragEnd={end} />);
  const layer = rendered.container.querySelector<HTMLElement>('.tactic-map-drawing')!;
  vi.spyOn(layer, 'getBoundingClientRect').mockReturnValue({ left: 0, top: 0, width: 100, height: 100 } as DOMRect);
  const role = screen.getByRole('button', { name: /^选择角色/ });
  Object.defineProperty(role, 'setPointerCapture', { value: vi.fn() });
  return { ...rendered, point, tool, start, move, end, layer, role, image: screen.getByAltText('Mirage 战术地图底图') };
}

it('does not draw or activate an action tool before the map image has loaded', () => {
  const value = mount();
  expect(screen.getByText('正在加载地图…')).toBeVisible();
  fireEvent.click(value.layer, { clientX: 25, clientY: 75 });
  const tool = screen.queryByRole('button', { name: '移动' }); if (tool) fireEvent.click(tool);
  expect(value.point).not.toHaveBeenCalled(); expect(value.tool).not.toHaveBeenCalled();
});

it('does not acquire a position edit from an unloaded image', async () => {
  const value = mount();
  await act(async () => fireEvent.pointerDown(value.role, { clientX: 10, clientY: 20, button: 0 }));
  expect(value.start).not.toHaveBeenCalled();
});

it('waits for the retry load event rather than reopening writes as soon as retry starts', () => {
  const value = mount();
  fireEvent.load(value.image); fireEvent.click(value.layer, { clientX: 25, clientY: 75 });
  expect(value.point).toHaveBeenCalledTimes(1);
  fireEvent.error(value.image); fireEvent.click(screen.getByRole('button', { name: '重试地图' }));
  fireEvent.click(value.layer, { clientX: 60, clientY: 40 });
  expect(value.point).toHaveBeenCalledTimes(1);
  fireEvent.load(value.image); fireEvent.click(value.layer, { clientX: 60, clientY: 40 });
  expect(value.point).toHaveBeenLastCalledWith({ x: 60, y: 40 }, 'select');
});

it('does not commit a captured role drag if the map image fails before pointerup', async () => {
  const value = mount(); fireEvent.load(value.image);
  await act(async () => fireEvent.pointerDown(value.role, { clientX: 10, clientY: 20, button: 0 }));
  fireEvent.pointerMove(value.role, { clientX: 35, clientY: 65 });
  expect(value.move).toHaveBeenLastCalledWith('role', { x: 35, y: 65 });
  fireEvent.error(value.image); fireEvent.pointerUp(value.role, { clientX: 35, clientY: 65 });
  expect(value.end).toHaveBeenLastCalledWith('role', null);
});

it('cannot bypass loading guards by entering fullscreen', () => {
  const value = mount();
  act(() => {
    Object.defineProperty(document, 'fullscreenElement', { configurable: true, value: screen.getByRole('region', { name: '阶段地图' }) });
    document.dispatchEvent(new Event('fullscreenchange'));
  });
  fireEvent.click(value.layer, { clientX: 25, clientY: 75 });
  expect(value.point).not.toHaveBeenCalled();
});

it('releases a late drag permission as cancellation even if the image has retried before it arrives', async () => {
  const value = mount(); let grant: (ready: boolean) => void = () => {};
  value.start.mockImplementationOnce(() => new Promise<boolean>(resolve => { grant = resolve; }));
  fireEvent.load(value.image);
  fireEvent.pointerDown(value.role, { clientX: 10, clientY: 20, button: 0 });
  fireEvent.error(value.image);
  fireEvent.click(screen.getByRole('button', { name: '重试地图' })); fireEvent.load(value.image);
  await act(async () => grant(true));
  expect(value.end).toHaveBeenLastCalledWith('role', null);
  expect(value.move).not.toHaveBeenCalled();
});

it('keeps fullscreen cancellation available but does not submit geometry while its image is unavailable', async () => {
  class Socket extends EventTarget {
    static OPEN = 1;
    static instances: Socket[] = [];
    readyState = 1; sent: Record<string, unknown>[] = [];
    constructor() { super(); Socket.instances.push(this); }
    send(data: string) { this.sent.push(JSON.parse(data)); }
    close() { this.readyState = 3; this.dispatchEvent(new Event('close')); }
    message(data: object) { this.dispatchEvent(new MessageEvent('message', { data: JSON.stringify(data) })); }
  }
  vi.stubGlobal('WebSocket', Socket);
  vi.stubGlobal('fetch', vi.fn(async () => ({ ok: true, status: 200, headers: new Headers(), json: async () => ({ ticket: 'fixture' }) })));
  const server = new Y.Doc();
  try {
    server.getMap('document').set('structure', structuredClone(projection.structure));
    for (const [field, value] of Object.entries({ ...projection.fields, 'tactic.title': '地图等待', 'tactic.purpose': '', 'tactic.applicability': '', 'stage.stage.description': '', 'stage.stage.condition': '' })) {
      const text = new Y.Text(); if (value) text.insert(0, value); server.getMap('fields').set(field, text);
    }
    render(<MemoryRouter><TacticEditor tacticId="fixture" session={{ user: { id: 'user', username: 'user', display_name: '玩家' }, csrf_token: 'fixture-csrf' }} /></MemoryRouter>);
    await waitFor(() => expect(Socket.instances).toHaveLength(1));
    const socket = Socket.instances[0];
    act(() => {
      socket.message({ type: 'auth.ok', actor_id: 'actor', connection_id: 'connection', role: 'editor', read_only: false });
      socket.message({ type: 'sync', document_id: 'fixture', epoch: 'epoch', revision: 1, state: encodeBytes(Y.encodeStateAsUpdate(server)), projection: projectDocument(server), recoveries: [], participants: [], locks: [] });
    });
    const image = await screen.findByAltText('Mirage 战术地图底图'); fireEvent.load(image);
    fireEvent.click(screen.getByRole('button', { name: '新增动作' }));
    fireEvent.change(screen.getByLabelText('新动作说明'), { target: { value: '现有绘制不丢' } });
    const layer = image.parentElement!;
    vi.spyOn(layer, 'getBoundingClientRect').mockReturnValue({ left: 0, top: 0, width: 100, height: 100 } as DOMRect);
    fireEvent.click(layer, { clientX: 35, clientY: 65 });
    fireEvent.click(layer, { clientX: 45, clientY: 55 });
    act(() => {
      Object.defineProperty(document, 'fullscreenElement', { configurable: true, value: screen.getByRole('region', { name: '阶段地图' }) });
      document.dispatchEvent(new Event('fullscreenchange'));
    });
    expect(within(screen.getByRole('region', { name: '全屏动作绘制' })).getByText(/已选3点/)).toBeVisible();
    expect(screen.getByRole('button', { name: '提交当前动作' })).toBeEnabled();
    fireEvent.error(image);
    expect(screen.getByRole('button', { name: '提交当前动作' })).toBeDisabled();
    fireEvent.click(screen.getByRole('button', { name: '提交当前动作' }));
    expect(socket.sent.some(message => message.type === 'operation')).toBe(false);
    expect(screen.getByRole('button', { name: '取消当前绘制' })).toBeEnabled();
    expect(screen.getByLabelText('新动作说明')).toHaveValue('现有绘制不丢');
  } finally { server.destroy(); }
});
