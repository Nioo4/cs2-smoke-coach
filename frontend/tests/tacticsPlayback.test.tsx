import { act, fireEvent, render, renderHook, screen } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import TacticMap from '../src/tactics/TacticMap';
import TacticPlaybackPanel from '../src/tactics/TacticPlaybackPanel';
import { useTacticPlayback } from '../src/tactics/useTacticPlayback';
import type { TacticProjection } from '../src/tactics/editorTypes';
import type { Participant } from '../src/tactics/collaborationClient';

const projection: TacticProjection = {
  structure: { schema_kind: 'stage-board', schema_version: 2, document_id: 'document', map: 'mirage', side: 'T', roles: [{ id: 'r', number: 1, color: '#cb5a42' }], stages: [
    { id: 'first', version: 1, initial_positions: { r: { x: 10, y: 20, version: 1 } }, lineup_ids: [], actions: [{ id: 'move', version: 1, group_id: 'g', kind: 'move', role_id: 'r', path: [{ x: 10, y: 20 }, { x: 90, y: 20 }] }] },
    { id: 'second', version: 1, initial_positions: { r: { x: 90, y: 20, version: 1 } }, lineup_ids: [], actions: [] },
  ] }, fields: { 'role.r.name': '玩家一', 'stage.first.name': '第一阶段', 'stage.second.name': '第二阶段', 'stage.first.description': '进攻说明', 'action.move.description': '沿路线移动' },
};
afterEach(() => { vi.restoreAllMocks(); vi.unstubAllGlobals(); });
function media(mobile = false) { vi.stubGlobal('matchMedia', vi.fn((query: string) => ({ matches: query.includes('any-pointer: fine') || query.includes('any-pointer:fine') ? !mobile : mobile, addEventListener: vi.fn(), removeEventListener: vi.fn() }))); }
it('normalizes live pointers on the transformed layer and clears them on leave without creating an edit', () => {
  media(); vi.stubGlobal('PointerEvent', MouseEvent);
  const pointer = vi.fn(); const edit = vi.fn();
  const { container } = render(<TacticMap projection={projection} stageId="first" readOnly={false} onMapPointer={pointer} onMapPoint={edit} />);
  const layer = container.querySelector('.tactic-map-drawing')!; const viewport = container.querySelector('.tactic-map-viewport')!;
  vi.spyOn(layer, 'getBoundingClientRect').mockReturnValue({ left: -100, top: -50, width: 800, height: 800, right: 700, bottom: 750 } as DOMRect);
  fireEvent.pointerMove(viewport, { clientX: 300, clientY: 350 });
  expect(pointer).toHaveBeenLastCalledWith({ x: 50, y: 50 }); expect(edit).not.toHaveBeenCalled();
  fireEvent.pointerMove(viewport, { clientX: 800, clientY: 350 });
  expect(pointer).toHaveBeenLastCalledWith(null);
  fireEvent.pointerLeave(viewport); expect(pointer).toHaveBeenLastCalledWith(null);
});
it('renders same-stage remote pointers and selected objects by connection, including another window of the same account', () => {
  media();
  const peer: Participant = { id: 'same-account', connection_id: 'window-b', name: '队友', color: '#315d57', read_only: false, stage_id: 'first', pointer: { x: 25, y: 70 }, selected_object_id: 'move' };
  const { rerender } = render(<TacticMap projection={projection} stageId="first" readOnly connectionId="window-a" participants={[peer, { ...peer, connection_id: 'window-a', name: '本窗口' }, { ...peer, connection_id: 'window-c', name: '其他阶段', stage_id: 'second' }]} />);
  expect(screen.getByRole('img', { name: '队友的地图指针' })).toHaveStyle({ left: '25%', top: '70%' });
  expect(screen.getByRole('img', { name: '队友选中的动作' })).toHaveStyle({ left: '90%', top: '20%' });
  expect(screen.queryByRole('img', { name: '本窗口的地图指针' })).not.toBeInTheDocument();
  expect(screen.queryByRole('img', { name: '其他阶段的地图指针' })).not.toBeInTheDocument();
  rerender(<TacticMap projection={projection} stageId="first" readOnly connectionId="window-a" participants={[{ ...peer, pointer: undefined, selected_object_id: 'r' }]} />);
  expect(screen.queryByRole('img', { name: '队友的地图指针' })).not.toBeInTheDocument();
  expect(screen.getByRole('img', { name: '队友选中的角色' })).toHaveStyle({ left: '10%', top: '20%' });
  rerender(<TacticMap projection={projection} stageId="second" readOnly connectionId="window-a" participants={[peer]} />);
  expect(screen.queryByRole('img', { name: '队友选中的动作' })).not.toBeInTheDocument();
});
it('shows a visible pending target at a single draft point and removes it when the draft is cleared', () => {
  media();
  const { rerender } = render(<TacticMap projection={projection} stageId="first" readOnly={false} drawing={[{ x: 35, y: 62 }]} />);
  expect(screen.getByRole('img', { name: '待提交目标位置' })).toHaveStyle({ left: '35%', top: '62%' });
  expect(screen.getByRole('img', { name: '待提交目标位置' })).toHaveTextContent('◎');
  rerender(<TacticMap projection={projection} stageId="first" readOnly={false} drawing={[]} />);
  expect(screen.queryByRole('img', { name: '待提交目标位置' })).not.toBeInTheDocument();
});
it('uses the shared playback algorithm, freezes confirmed snapshot, and pauses the interstage wait', () => {
  let callback: FrameRequestCallback | null = null;
  let now = 0;
  vi.spyOn(performance, 'now').mockImplementation(() => now);
  vi.stubGlobal('requestAnimationFrame', vi.fn((next: FrameRequestCallback) => { callback = next; return 1; }));
  vi.stubGlobal('cancelAnimationFrame', vi.fn());
  const { result, rerender } = renderHook(({ revision }) => useTacticPlayback(projection, revision, 'epoch', false), { initialProps: { revision: 1 } });
  act(() => { expect(result.current.start('auto', 'first')).toBe(true); });
  act(() => { now = 1500; callback!(now); });
  expect(result.current.play?.frame.positions.r.x).toBe(50);
  rerender({ revision: 2 });
  expect(result.current.changed).toBe(true);
  expect(result.current.play?.revision).toBe(1);
  act(() => { now = 3200; callback!(now); });
  expect(result.current.play?.waiting).toBe(true);
  act(() => result.current.togglePause());
  expect(result.current.play?.paused).toBe(true);
  now = 15000;
  act(() => result.current.togglePause());
  act(() => { now = 15801; callback!(now); });
  expect(result.current.play?.stageIndex).toBe(1);
  act(() => result.current.exit());
  expect(result.current.play).toBeNull();
});
it('keeps map edits out of mobile view while allowing zoom and real role selection', () => {
  media(true);
  const select = vi.fn();
  render(<TacticMap projection={projection} stageId="first" readOnly={false} onSelectRole={select} />);
  expect(screen.queryByRole('button', { name: '设置站位' })).not.toBeInTheDocument();
  expect(screen.getByText(/手机端仅供查看/)).toBeVisible();
  fireEvent.change(screen.getByRole('slider'), { target: { value: '4' } });
  expect(screen.getByText('4.0×')).toBeVisible();
  fireEvent.click(screen.getByRole('button', { name: '选择角色 1：玩家一' }));
  expect(select).toHaveBeenCalledWith('r');
  fireEvent.click(screen.getByRole('button', { name: '复位' }));
  expect(screen.getByText('1.0×')).toBeVisible();
});
it('normalizes edit coordinates against transformed drawing bounds and renders five utility types', () => {
  media();
  const point = vi.fn();
  const copy = structuredClone(projection);
  for (const utility of ['smoke', 'flash', 'molotov', 'he', 'decoy'] as const) copy.structure.stages[0].actions.push({ id: utility, version: 1, group_id: utility, kind: 'utility', thrower_role_id: 'r', utility_type: utility, target: { x: 50, y: 50 } });
  const { container } = render(<TacticMap projection={copy} stageId="first" readOnly={false} onMapPoint={point} />);
  fireEvent.load(screen.getByAltText('Mirage 战术地图底图'));
  const layer = container.querySelector('.tactic-map-drawing')!;
  vi.spyOn(layer, 'getBoundingClientRect').mockReturnValue({ left: -100, top: -50, width: 800, height: 800 } as DOMRect);
  fireEvent.click(screen.getByRole('button', { name: '设置站位' }));
  fireEvent.click(layer, { clientX: 300, clientY: 350 });
  expect(point).toHaveBeenCalledWith({ x: 50, y: 50 }, 'position');
  expect(container.querySelectorAll('.tactic-map-pin.utility')).toHaveLength(5);
});
it('provides actual frozen-playback consumer controls and blocks preview of unconfirmed edits', () => {
  media();
  render(<TacticPlaybackPanel confirmed={projection} revision={1} epoch="epoch" hasUnconfirmedWork />);
  expect(screen.getByRole('button', { name: '自动演示' })).toBeDisabled();
  expect(screen.getByText('沿路线移动')).toBeVisible();
  fireEvent.click(screen.getByRole('button', { name: '第二阶段' }));
  expect(screen.getByRole('button', { name: '第二阶段' })).toHaveAttribute('aria-pressed', 'true');
});
it('keeps playback controls inside the fullscreen surface and exposes an explicit exit', async () => {
  media();
  const { container } = render(<TacticPlaybackPanel confirmed={projection} revision={1} epoch="epoch" hasUnconfirmedWork={false} />);
  const surface = container.querySelector('.tactic-map-panel')!;
  Object.defineProperty(surface, 'requestFullscreen', { configurable: true, value: vi.fn(async () => {
    Object.defineProperty(document, 'fullscreenElement', { configurable: true, value: surface });
    document.dispatchEvent(new Event('fullscreenchange'));
  }) });
  fireEvent.click(screen.getByRole('button', { name: '全屏' }));
  expect(await screen.findByRole('button', { name: '退出全屏' })).toBeVisible();
  expect(surface.contains(screen.getByRole('button', { name: '自动演示' }))).toBe(true);
  Object.defineProperty(document, 'fullscreenElement', { configurable: true, value: null });
});
it('filters only displayed roles/actions and keeps the document unchanged', () => {
  media();
  const source = structuredClone(projection);
  source.structure.roles.push({ id: 'other', number: 2, color: '#315d57' });
  const before = JSON.stringify(source);
  render(<TacticPlaybackPanel confirmed={source} revision={1} epoch="epoch" hasUnconfirmedWork={false} />);
  fireEvent.change(screen.getByLabelText('按角色查看'), { target: { value: 'other' } });
  expect(screen.queryByText('沿路线移动')).not.toBeInTheDocument();
  expect(screen.queryByRole('button', { name: '选择角色 1：玩家一' })).not.toBeInTheDocument();
  expect(JSON.stringify(source)).toBe(before);
});
it('retains deleted stage in frozen playback and explicitly falls back after exit', () => {
  media();
  vi.stubGlobal('requestAnimationFrame', vi.fn(() => 1));
  vi.stubGlobal('cancelAnimationFrame', vi.fn());
  const { rerender } = render(<TacticPlaybackPanel confirmed={projection} revision={1} epoch="epoch" hasUnconfirmedWork={false} />);
  fireEvent.click(screen.getByRole('button', { name: '第二阶段' }));
  fireEvent.click(screen.getByRole('button', { name: '分阶段演示' }));
  const changed = structuredClone(projection);
  changed.structure.stages.splice(1, 1);
  rerender(<TacticPlaybackPanel confirmed={changed} revision={2} epoch="epoch" hasUnconfirmedWork={false} />);
  expect(screen.getByRole('heading', { name: '第二阶段' })).toBeVisible();
  expect(screen.getByText(/预览期间收到新修改/)).toBeVisible();
  fireEvent.click(screen.getByRole('button', { name: '退出预览' }));
  expect(screen.getByText(/此前选择的阶段已被删除/)).toBeVisible();
  expect(screen.getByRole('heading', { name: '第一阶段' })).toBeVisible();
});
