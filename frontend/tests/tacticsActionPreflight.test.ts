import { expect, it } from 'vitest';
import { parallelRoleConflict } from '../src/tactics/actionPreflight';
import type { TacticAction, TacticStage } from '../src/tactics/editorTypes';

function stage(actions: TacticAction[]): TacticStage {
  return { id: 'stage', version: 1, initial_positions: {}, lineup_ids: [], actions };
}
const move: TacticAction = { id: 'move', version: 1, kind: 'move', role_id: 'r1', group_id: 'g1', path: [{ x: 0, y: 0 }, { x: 10, y: 10 }] };
const utility: TacticAction = { id: 'utility', version: 1, kind: 'utility', thrower_role_id: 'r2', group_id: 'g1', utility_type: 'smoke', target: { x: 20, y: 20 } };
const wait: TacticAction = { id: 'wait', version: 1, kind: 'wait', role_id: 'r3', group_id: 'g2' };

it('checks the entire last group for move and utility throwers, allowing a new group or another role', () => {
  const value = stage([move, utility]);
  expect(parallelRoleConflict(value, { type: 'action.add', kind: 'wait', role_id: 'r1', group_mode: 'parallel' })).toBe('r1');
  expect(parallelRoleConflict(value, { type: 'action.add', kind: 'observe', role_id: 'r2', group_mode: 'parallel' })).toBe('r2');
  expect(parallelRoleConflict(value, { type: 'action.add', kind: 'utility', thrower_role_id: 'r1', role_id: 'irrelevant', group_mode: 'parallel' })).toBe('r1');
  expect(parallelRoleConflict(value, { type: 'action.add', kind: 'move', role_id: 'r3', group_mode: 'parallel' })).toBeNull();
  expect(parallelRoleConflict(value, { type: 'action.add', kind: 'wait', role_id: 'r1', group_mode: 'after' })).toBeNull();
  expect(parallelRoleConflict(stage([]), { type: 'action.add', kind: 'wait', role_id: 'r1', group_mode: 'parallel' })).toBeNull();
});

it('excludes the edited action itself and checks changed utility thrower instead of role_id', () => {
  const value = stage([move, utility]);
  expect(parallelRoleConflict(value, { type: 'action.update', action_id: 'move', changes: { role_id: 'r1' } })).toBeNull();
  expect(parallelRoleConflict(value, { type: 'action.update', action_id: 'move', changes: { role_id: 'r2' } })).toBe('r2');
  expect(parallelRoleConflict(value, { type: 'action.update', action_id: 'utility', changes: { thrower_role_id: 'r1' } })).toBe('r1');
  expect(parallelRoleConflict(value, { type: 'action.update', action_id: 'utility', changes: { role_id: 'r1' } })).toBeNull();
});

it('checks regroup targets while allowing the existing group and a new group', () => {
  const value = stage([move, utility, { ...wait, role_id: 'r1' }]);
  expect(parallelRoleConflict(value, { type: 'action.group', action_id: 'wait', group_index: 0 })).toBe('r1');
  expect(parallelRoleConflict(value, { type: 'action.group', action_id: 'move', group_index: 0 })).toBeNull();
  expect(parallelRoleConflict(value, { type: 'action.group', action_id: 'wait', group_index: 2 })).toBeNull();
});

it.each([null, 'r1'])('does not reserve roles for text hints with role=%s, as the schema usesRole excludes all text', role => {
  const text: TacticAction = { id: 'text', version: 1, kind: 'text', role_id: role, group_id: 'g1', position: { x: 0, y: 0 } };
  expect(parallelRoleConflict(stage([move]), { type: 'action.add', kind: 'text', role_id: role, group_mode: 'parallel' })).toBeNull();
  expect(parallelRoleConflict(stage([text]), { type: 'action.add', kind: 'wait', role_id: 'r1', group_mode: 'parallel' })).toBeNull();
  expect(parallelRoleConflict(stage([move, text]), { type: 'action.update', action_id: 'text', changes: { role_id: 'r1' } })).toBeNull();
  expect(parallelRoleConflict(stage([move, { ...text, group_id: 'g2' }]), { type: 'action.group', action_id: 'text', group_index: 0 })).toBeNull();
});

it('does not translate unknown command/schema failures into this narrow known-conflict result', () => {
  const value = stage([move]);
  for (const command of [
    { type: 'unknown' }, { type: 'action.update', action_id: 'move', changes: null },
    { type: 'action.group', action_id: 'move', group_index: -1 },
    { type: 'action.add', kind: 'wait', role_id: null, group_mode: 'parallel' },
  ]) expect(parallelRoleConflict(value, command)).toBeNull();
});
