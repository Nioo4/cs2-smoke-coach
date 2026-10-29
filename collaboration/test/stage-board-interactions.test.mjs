import { test } from 'node:test';
import assert from 'node:assert/strict';
import { mapClickIntent, mapPointFromRect, moveCommandFromDrawing } from '../src/stage-board-interactions.mjs';

test('move drawing binds the selected role and route into the UI-generated action command', () => {
  const drawing = {
    mode: 'new-move', roleId: 'role-selected', points: [{ x: 50, y: 50 }, { x: 45, y: 60 }],
    command: { type: 'action.add', stage_id: 'stage-current', kind: 'move', group_mode: 'after', description: '沿折线路径移动。' },
  };
  const command = moveCommandFromDrawing(drawing);
  assert.equal(command.role_id, 'role-selected');
  assert.deepEqual(command.path, drawing.points);
  assert.equal(command.stage_id, 'stage-current');
  assert.equal(command.description, '沿折线路径移动。');
  assert.notEqual(command.path, drawing.points);
});

test('an active route or target drawing takes priority over a stale position tool', () => {
  assert.equal(mapClickIntent('position', { mode: 'new-move' }), 'drawing');
  assert.equal(mapClickIntent('position', { mode: 'edit-target' }), 'drawing');
  assert.equal(mapClickIntent('position', null), 'position');
});

test('fullscreen map points use the square drawing layer inside a letterboxed viewport', () => {
  const viewport = { left: 0, top: 0, width: 1600, height: 900 };
  const squareMapLayer = { left: 350, top: 0, width: 900, height: 900 };
  const point = mapPointFromRect(575, 675, squareMapLayer);
  assert.deepEqual(point, { x: 25, y: 75 });
  assert.notDeepEqual(point, mapPointFromRect(575, 675, viewport));
  assert.deepEqual(mapPointFromRect(500, 1425, { left: 50, top: 75, width: 1800, height: 1800 }), { x: 25, y: 75 });
  assert.throws(() => mapPointFromRect(0, 0, { left: 0, top: 0, width: 0, height: 0 }), /invalid_map_bounds/);
});
