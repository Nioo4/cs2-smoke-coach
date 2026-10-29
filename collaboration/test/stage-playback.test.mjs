import { test } from 'node:test';
import assert from 'node:assert/strict';
import { interpolatePath, stagePlaybackFrame } from '../src/stage-playback.mjs';

test('route interpolation follows cumulative path distance rather than segment count', () => {
  const point = interpolatePath([{ x: 0, y: 0 }, { x: 80, y: 0 }, { x: 80, y: 20 }], 0.8);
  assert.deepEqual(point, { x: 80, y: 0 });
});

test('parallel action groups animate together and sequential groups hold the final state', () => {
  const stage = {
    initial_positions: { a: { x: 0, y: 0 }, b: { x: 10, y: 10 } },
    actions: [
      { id: 'a1', group_id: 'g1', kind: 'move', role_id: 'a', path: [{ x: 0, y: 0 }, { x: 60, y: 0 }] },
      { id: 'b1', group_id: 'g1', kind: 'move', role_id: 'b', path: [{ x: 10, y: 10 }, { x: 70, y: 10 }] },
      { id: 'a2', group_id: 'g2', kind: 'move', role_id: 'a', path: [{ x: 60, y: 0 }, { x: 60, y: 60 }] },
    ],
  };
  const halfway = stagePlaybackFrame(stage, 1_500);
  assert.deepEqual(halfway.positions, { a: { x: 30, y: 0 }, b: { x: 40, y: 10 } });
  assert.equal(halfway.activeGroupId, 'g1');
  assert.deepEqual(halfway.activeActionIds, ['a1', 'b1']);
  const next = stagePlaybackFrame(stage, 4_500);
  assert.deepEqual(next.positions, { a: { x: 60, y: 30 }, b: { x: 70, y: 10 } });
  assert.equal(next.activeGroupId, 'g2');
  const end = stagePlaybackFrame(stage, 9_000);
  assert.deepEqual(end.positions, { a: { x: 60, y: 60 }, b: { x: 70, y: 10 } });
  assert.equal(end.complete, true);
  assert.deepEqual(end.activeActionIds, []);
});
