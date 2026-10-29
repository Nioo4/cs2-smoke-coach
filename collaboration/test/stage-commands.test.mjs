import { test } from 'node:test';
import assert from 'node:assert/strict';
import { randomUUID } from 'node:crypto';
import { buildStageCommand } from '../src/stage-commands.mjs';
import { actionDescriptionField, createStageInitialData, validateStageStructure } from '../src/stage-board-schema.mjs';

function fixture() {
  const data = createStageInitialData();
  return { structure: data.structure, role: data.structure.roles[0], stage: data.structure.stages[0] };
}

function apply(structure, command) {
  return buildStageCommand(structure, command, null).structure;
}

function addMove(structure, stage, role, end, groupMode = 'after') {
  return apply(structure, {
    type: 'action.add', stage_id: stage.id, kind: 'move', role_id: role.id,
    group_mode: groupMode, description: '沿路径移动到指定位置。',
    path: [{ x: 0, y: 0 }, end],
  });
}

test('position changes, reinheritance, and removing an earlier move rebase only derived movement starts', () => {
  let { structure, role, stage } = fixture();
  structure = addMove(structure, stage, role, { x: 20, y: 20 });
  structure = addMove(structure, stage, role, { x: 30, y: 30 });
  structure = addMove(structure, stage, role, { x: 40, y: 40 });
  stage = structure.stages[0];
  const [first, second, third] = stage.actions;

  structure = apply(structure, { type: 'position.set', stage_id: stage.id, role_id: role.id, position: { x: 60, y: 60 } });
  assert.deepEqual(structure.stages[0].actions.map((action) => action.path[0]), [
    { x: 60, y: 60 }, { x: 20, y: 20 }, { x: 30, y: 30 },
  ]);

  const nextStageId = randomUUID();
  structure.stages.push({ id: nextStageId, initial_positions: { [role.id]: { x: 1, y: 1 } }, actions: [
    { id: randomUUID(), group_id: randomUUID(), kind: 'move', role_id: role.id, path: [{ x: 1, y: 1 }, { x: 70, y: 70 }] },
  ] });
  structure = apply(structure, { type: 'stage.reinherit', stage_id: nextStageId });
  const inherited = structure.stages[1];
  assert.deepEqual(inherited.initial_positions[role.id], { x: 40, y: 40 });
  assert.deepEqual(inherited.actions[0].path, [{ x: 40, y: 40 }, { x: 70, y: 70 }]);

  structure.stages = [structure.stages[0]];
  structure = apply(structure, { type: 'action.delete', stage_id: stage.id, action_id: first.id });
  const rebased = structure.stages[0].actions;
  assert.deepEqual(rebased.map((action) => action.path[0]), [{ x: 60, y: 60 }, { x: 30, y: 30 }]);
  assert.deepEqual(rebased.map((action) => action.path.at(-1)), [{ x: 30, y: 30 }, { x: 40, y: 40 }]);
  validateStageStructure(structure);
  assert.ok(second.id && third.id);
});

test('moving an action to an existing group keeps that group contiguous', () => {
  const { structure, role, stage } = fixture();
  const firstGroup = randomUUID();
  const middleGroup = randomUUID();
  const lastGroup = randomUUID();
  stage.actions = [
    { id: randomUUID(), group_id: firstGroup, kind: 'wait', role_id: role.id },
    { id: randomUUID(), group_id: firstGroup, kind: 'text', role_id: null, position: { x: 30, y: 30 } },
    { id: randomUUID(), group_id: middleGroup, kind: 'text', role_id: null, position: { x: 50, y: 50 } },
    { id: randomUUID(), group_id: lastGroup, kind: 'text', role_id: null, position: { x: 70, y: 70 } },
  ];
  const moved = stage.actions[1];
  const result = apply(structure, { type: 'action.group', stage_id: stage.id, action_id: moved.id, group_index: 1 });
  const actions = result.stages[0].actions;
  assert.deepEqual(actions.map((action) => action.group_id), [firstGroup, middleGroup, middleGroup, lastGroup]);
  assert.equal(actions.filter((action) => action.id === moved.id)[0].group_id, middleGroup);
  validateStageStructure(result);
});

test('recovery restores an action as a new sequential group and derives its start from current positions', () => {
  const { structure, role, stage } = fixture();
  const currentGroup = randomUUID();
  stage.actions = [{ id: randomUUID(), group_id: currentGroup, kind: 'move', role_id: role.id, path: [{ x: 50, y: 50 }, { x: 20, y: 20 }] }];
  const recoveryId = randomUUID();
  const oldActionId = randomUUID();
  structure.recovery.push({
    id: recoveryId, kind: 'action',
    snapshot: { stage_id: stage.id, action: { id: oldActionId, group_id: randomUUID(), kind: 'move', role_id: role.id, path: [{ x: 10, y: 10 }, { x: 80, y: 80 }] } },
    texts: { [actionDescriptionField(oldActionId)]: '原动作说明' },
  });
  const result = apply(structure, { type: 'recovery.restore', recovery_id: recoveryId, stage_id: stage.id });
  const actions = result.stages[0].actions;
  assert.equal(actions.length, 2);
  assert.notEqual(actions[1].group_id, currentGroup);
  assert.deepEqual(actions[1].path, [{ x: 20, y: 20 }, { x: 80, y: 80 }]);
  validateStageStructure(result);
});

test('malformed action paths are rejected as input errors before mapping', () => {
  const { structure, role, stage } = fixture();
  assert.throws(() => apply(structure, {
    type: 'action.add', stage_id: stage.id, kind: 'move', role_id: role.id,
    group_mode: 'after', description: '路线说明。', path: null,
  }), { message: 'invalid_move_path' });
});
