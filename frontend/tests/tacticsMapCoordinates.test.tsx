import { act, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import TacticMap from '../src/tactics/TacticMap';
import type { TacticProjection } from '../src/tactics/editorTypes';
import { mapPointFromRect } from '../../collaboration/src/stage-board-interactions.mjs';

const projection: TacticProjection = {
  structure: { schema_kind: 'stage-board', schema_version: 2, document_id: 'fixture', map: 'mirage', side: 'T',
    roles: [{ id: 'role', number: 1, color: '#cb5a42' }],
    stages: [{ id: 'stage', version: 1, initial_positions: { role: { x: 10, y: 20, version: 1 } }, actions: [], lineup_ids: [] }] },
  fields: { 'stage.stage.name': '坐标测试', 'role.role.name': '角色' },
};
const target = { x: 37.5, y: 62.5 };
function rect(left: number, top: number, width: number, height = width): DOMRect {
  return { left, top, width, height, right: left + width, bottom: top + height, x: left, y: top, toJSON: () => ({}) };
}
function location(box: DOMRect, point = target) { return { clientX: box.left + box.width * point.x / 100, clientY: box.top + box.height * point.y / 100 }; }
let fullscreenDescriptor: PropertyDescriptor | undefined;
beforeEach(() => {
  fullscreenDescriptor = Object.getOwnPropertyDescriptor(document, 'fullscreenElement');
  vi.stubGlobal('matchMedia', vi.fn(() => ({ matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn() })));
  vi.stubGlobal('PointerEvent', class extends MouseEvent {
    readonly pointerId: number;
    constructor(type: string, init: PointerEventInit = {}) { super(type, init); this.pointerId = init.pointerId ?? 1; }
  });
});
afterEach(() => {
  vi.restoreAllMocks(); vi.unstubAllGlobals();
  if (fullscreenDescriptor) Object.defineProperty(document, 'fullscreenElement', fullscreenDescriptor);
  else Reflect.deleteProperty(document, 'fullscreenElement');
});
function fullscreen(surface: Element | null) {
  Object.defineProperty(document, 'fullscreenElement', { configurable: true, value: surface });
  document.dispatchEvent(new Event('fullscreenchange'));
}

it('uses fresh drawing bounds for the same target after actual zoom/pan/fullscreen controls and return', async () => {
  const point = vi.fn(), pointer = vi.fn();
  const { container } = render(<TacticMap projection={projection} stageId="stage" readOnly={false} onMapPoint={point} onMapPointer={pointer} />);
  fireEvent.load(screen.getByAltText('Mirage 战术地图底图'));
  const layer = container.querySelector<HTMLElement>('.tactic-map-drawing')!;
  const viewport = container.querySelector<HTMLElement>('.tactic-map-viewport')!;
  const surface = screen.getByRole('region', { name: '阶段地图' });
  let box = rect(100, 150, 400);
  vi.spyOn(layer, 'getBoundingClientRect').mockImplementation(() => box);
  vi.spyOn(viewport, 'getBoundingClientRect').mockReturnValue(rect(1, 2, 999, 777));
  Object.defineProperty(viewport, 'setPointerCapture', { value: vi.fn() });
  const check = () => {
    fireEvent.click(screen.getByRole('button', { name: '设置站位' }));
    fireEvent.click(layer, location(box));
    expect(point).toHaveBeenLastCalledWith(target, 'position');
    fireEvent.pointerMove(viewport, location(box));
    expect(pointer).toHaveBeenLastCalledWith(target);
  };
  check();
  fireEvent.change(screen.getByRole('slider'), { target: { value: '4' } });
  expect(layer.style.transform).toBe('scale(4)'); box = rect(-500, -450, 1600); check();
  fireEvent.click(screen.getByRole('button', { name: '平移地图' }));
  fireEvent.pointerDown(viewport, { clientX: 100, clientY: 100, pointerId: 1, button: 0 });
  fireEvent.pointerMove(viewport, { clientX: 220, clientY: 20, pointerId: 1 });
  fireEvent.pointerUp(viewport, { pointerId: 1 });
  expect(container.querySelector<HTMLElement>('.tactic-map-world')!.style.transform).toBe('translate(120px, -80px)');
  box = rect(-380, -530, 1600); check();
  Object.defineProperty(surface, 'requestFullscreen', { value: vi.fn(async () => fullscreen(surface)) });
  fireEvent.click(screen.getByRole('button', { name: '全屏' }));
  await screen.findByRole('button', { name: '退出全屏' });
  box = rect(-600, -1100, 3200); check();
  fireEvent.change(screen.getByRole('slider'), { target: { value: '1' } });
  box = rect(600, 100, 800); check();
  act(() => fullscreen(null)); box = rect(100, 150, 400); check();
  expect(container.querySelector('.tactic-map-drawing')).toBe(layer);
  expect(point).toHaveBeenCalledTimes(6);
});

it.each([
  { name: 'normal', box: rect(100, 150, 400), full: false, zoom: '1' },
  { name: 'zoomed and panned', box: rect(-380, -530, 1600), full: false, zoom: '4' },
  { name: 'fullscreen', box: rect(600, 100, 800), full: true, zoom: '1' },
])('uses transformed drawing bounds for a leased role drag in $name mode', async ({ box, full, zoom }) => {
  const start = vi.fn(async () => true), move = vi.fn(), end = vi.fn();
  const { container } = render(<TacticMap projection={projection} stageId="stage" readOnly={false} onRoleDragStart={start} onRoleDragMove={move} onRoleDragEnd={end} />);
  fireEvent.load(screen.getByAltText('Mirage 战术地图底图'));
  const layer = container.querySelector<HTMLElement>('.tactic-map-drawing')!;
  vi.spyOn(layer, 'getBoundingClientRect').mockReturnValue(box);
  fireEvent.change(screen.getByRole('slider'), { target: { value: zoom } });
  if (full) act(() => fullscreen(screen.getByRole('region', { name: '阶段地图' })));
  const role = screen.getByRole('button', { name: /^选择角色/ });
  Object.defineProperty(role, 'setPointerCapture', { value: vi.fn() });
  await act(async () => fireEvent.pointerDown(role, { ...location(box, { x: 10, y: 20 }), pointerId: 2, button: 0 }));
  fireEvent.pointerMove(role, { ...location(box), pointerId: 2 });
  fireEvent.pointerUp(role, { ...location(box), pointerId: 2 });
  expect(start).toHaveBeenCalledWith('role');
  expect(move).toHaveBeenLastCalledWith('role', target);
  expect(end).toHaveBeenLastCalledWith('role', target);
  expect(projection.structure.stages[0].initial_positions.role).toEqual({ x: 10, y: 20, version: 1 });
});

it('reports inclusive pointer boundaries but sends null outside or on a zero-size layer', () => {
  const pointer = vi.fn();
  const { container } = render(<TacticMap projection={projection} stageId="stage" readOnly onMapPointer={pointer} />);
  let box = rect(100, 200, 400);
  vi.spyOn(container.querySelector('.tactic-map-drawing')!, 'getBoundingClientRect').mockImplementation(() => box);
  const viewport = container.querySelector('.tactic-map-viewport')!;
  for (const edge of [{ x: 0, y: 0 }, { x: 100, y: 100 }]) {
    fireEvent.pointerMove(viewport, location(box, edge)); expect(pointer).toHaveBeenLastCalledWith(edge);
  }
  fireEvent.pointerMove(viewport, { clientX: box.left - 1, clientY: box.top }); expect(pointer).toHaveBeenLastCalledWith(null);
  box = rect(100, 200, 0);
  fireEvent.pointerMove(viewport, { clientX: 100, clientY: 200 }); expect(pointer).toHaveBeenLastCalledWith(null);
});

it.each([
  { at: { x: 100, y: 200 }, expected: { x: 0, y: 0 } },
  { at: { x: 500, y: 600 }, expected: { x: 100, y: 100 } },
  { at: { x: -900, y: 400 }, expected: { x: 0, y: 50 } },
  { at: { x: 300, y: 900 }, expected: { x: 50, y: 100 } },
])('clamps finite edit coordinates $at to legal map boundaries', ({ at, expected }) => {
  expect(mapPointFromRect(at.x, at.y, rect(100, 200, 400))).toEqual(expected);
});

it.each([[0, 400], [400, 0], [0, 0]])('rejects a zero-size conversion layer %sx%s', (width, height) => {
  expect(() => mapPointFromRect(0, 0, rect(0, 0, width, height))).toThrow('invalid_map_bounds');
});
