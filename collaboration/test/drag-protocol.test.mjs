import { test } from 'node:test';
import assert from 'node:assert/strict';
import { dragGrantAction, mapObjectCenter, updateDragPosition } from '../src/drag-protocol.mjs';

function drag() { return { startX: 12, startY: 30, x: 12, y: 30, moved: false, ended: false, cancelled: false }; }

test('fast pointer release keeps the final position until a delayed lease grant can commit it', () => {
  const pending = drag();
  updateDragPosition(pending, 48, 62);
  pending.ended = true;
  assert.equal(dragGrantAction(pending), 'commit');
  assert.deepEqual([pending.x, pending.y], [48, 62]);
});

test('delayed lease grant releases a click or cancelled gesture without a map operation', () => {
  const click = drag();
  click.ended = true;
  assert.equal(dragGrantAction(click), 'release');
  const cancelled = drag();
  updateDragPosition(cancelled, 50, 60);
  cancelled.ended = true;
  cancelled.cancelled = true;
  assert.equal(dragGrantAction(cancelled), 'release');
});

test('active drag waits for pointerup after an early lease grant', () => {
  const active = drag();
  assert.equal(dragGrantAction(active), 'continue');
  updateDragPosition(active, 44, 53);
  active.ended = true;
  assert.equal(dragGrantAction(active), 'commit');
});

test('three-point asymmetric arrow uses the rendered bounding-box center for dragging', () => {
  const arrow = { kind: 'arrow', points: [{ x: 20, y: 25 }, { x: 30, y: 40 }, { x: 80, y: 70 }] };
  assert.deepEqual(mapObjectCenter(arrow), { x: 50, y: 47.5 });
});
