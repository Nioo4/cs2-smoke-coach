import { randomUUID } from 'node:crypto';
import {
  actionDescriptionField, roleNameField, stageConditionField, stageDescriptionField, stageEndPositions,
  stageNameField, validateStageTextValue,
} from './stage-board-schema.mjs';

const PALETTE = ['#cb5a42', '#315d57', '#b58324', '#586b9b', '#8a4b78'];

export function buildStageCommand(sourceStructure, command, doc) {
  if (!command || typeof command !== 'object' || Array.isArray(command) || typeof command.type !== 'string') throw new Error('invalid_stage_command');
  const structure = structuredClone(sourceStructure);
  const fieldSources = {};
  switch (command.type) {
    case 'role.add': {
      exact(command, ['type']);
      if (structure.roles.length >= 5) throw new Error('stage_role_limit');
      const number = firstFreeNumber(structure.roles.map((role) => role.number));
      const role = { id: randomUUID(), number, color: PALETTE[number - 1] };
      structure.roles.push(role);
      for (const stage of structure.stages) stage.initial_positions[role.id] = { x: 50, y: 50 };
      fieldSources[roleNameField(role.id)] = literal(`角色 ${number}`);
      break;
    }
    case 'stage.add': {
      exact(command, ['type', 'after_stage_id']);
      if (structure.stages.length >= 20) throw new Error('stage_count_limit');
      const index = insertAfterIndex(structure.stages, command.after_stage_id);
      const previous = index > 0 ? structure.stages[index - 1] : null;
      const stage = { id: randomUUID(), initial_positions: previous ? stageEndPositions(previous, structure.roles) : Object.fromEntries(structure.roles.map((role) => [role.id, { x: 50, y: 50 }])), actions: [] };
      structure.stages.splice(index, 0, stage);
      fieldSources[stageNameField(stage.id)] = literal(`阶段 ${index + 1}`);
      fieldSources[stageDescriptionField(stage.id)] = literal('');
      fieldSources[stageConditionField(stage.id)] = literal('');
      break;
    }
    case 'stage.copy': {
      exact(command, ['type', 'stage_id']);
      if (structure.stages.length >= 20) throw new Error('stage_count_limit');
      const source = findStage(structure, command.stage_id);
      const index = structure.stages.indexOf(source) + 1;
      const copy = copyStage(source, structure.roles, fieldSources, 'active');
      normalizeStage(copy);
      structure.stages.splice(index, 0, copy);
      break;
    }
    case 'stage.delete': {
      exact(command, ['type', 'stage_id']);
      if (structure.stages.length <= 1) throw new Error('last_stage_required');
      structure.stages.splice(structure.stages.indexOf(findStage(structure, command.stage_id)), 1);
      break;
    }
    case 'stage.reorder': {
      exact(command, ['type', 'stage_id', 'to_index']);
      const index = structure.stages.indexOf(findStage(structure, command.stage_id));
      reorder(structure.stages, index, command.to_index);
      break;
    }
    case 'stage.reinherit': {
      exact(command, ['type', 'stage_id']);
      const index = structure.stages.findIndex((stage) => stage.id === command.stage_id);
      if (index < 1) throw new Error('stage_has_no_previous');
      structure.stages[index].initial_positions = stageEndPositions(structure.stages[index - 1], structure.roles);
      normalizeStage(structure.stages[index]);
      break;
    }
    case 'position.set': {
      exact(command, ['type', 'stage_id', 'role_id', 'position']);
      const stage = findStage(structure, command.stage_id);
      if (!structure.roles.some((role) => role.id === command.role_id)) throw new Error('role_not_found');
      structure.stages[structure.stages.indexOf(stage)].initial_positions[command.role_id] = point(command.position);
      normalizeStage(stage);
      break;
    }
    case 'action.add': {
      const stage = findStage(structure, command.stage_id);
      const action = makeAction(command, stage, structure.roles, doc);
      if (stage.actions.length >= 100) throw new Error('stage_action_limit');
      structure.stages[structure.stages.indexOf(stage)].actions.push(action);
      normalizeStage(stage);
      fieldSources[actionDescriptionField(action.id)] = literal(command.description);
      break;
    }
    case 'action.update': {
      exact(command, ['type', 'stage_id', 'action_id', 'changes']);
      const stage = findStage(structure, command.stage_id);
      const action = findAction(stage, command.action_id);
      const changes = command.changes;
      if (!changes || typeof changes !== 'object' || Array.isArray(changes) || !Object.keys(changes).length) throw new Error('invalid_action_changes');
      const allowed = action.kind === 'move' ? ['role_id', 'path'] : action.kind === 'observe' ? ['role_id', 'target'] : action.kind === 'utility' ? ['thrower_role_id', 'target', 'utility_type'] : action.kind === 'text' ? ['role_id', 'position'] : ['role_id'];
      if (Object.keys(changes).some((key) => !allowed.includes(key))) throw new Error('invalid_action_changes');
      const replacement = { ...action, ...structuredClone(changes) };
      const index = stage.actions.indexOf(action);
      stage.actions[index] = replacement;
      normalizeStage(stage);
      break;
    }
    case 'action.group': {
      exact(command, ['type', 'stage_id', 'action_id', 'group_index']);
      const stage = findStage(structure, command.stage_id);
      const action = findAction(stage, command.action_id);
      const groups = groupIds(stage.actions);
      if (!Number.isInteger(command.group_index) || command.group_index < 0 || command.group_index > groups.length) throw new Error('invalid_action_group');
      const groupId = command.group_index === groups.length ? randomUUID() : groups[command.group_index];
      stage.actions.splice(stage.actions.indexOf(action), 1);
      action.group_id = groupId;
      const targetGroupLast = stage.actions.reduce((last, item, index) => item.group_id === groupId ? index : last, -1);
      const insertionIndex = targetGroupLast < 0 ? stage.actions.length : targetGroupLast + 1;
      stage.actions.splice(insertionIndex, 0, action);
      normalizeStage(stage);
      break;
    }
    case 'action.reorder': {
      exact(command, ['type', 'stage_id', 'action_id', 'to_index', 'group_mode']);
      const stage = findStage(structure, command.stage_id);
      const action = findAction(stage, command.action_id);
      const index = stage.actions.indexOf(action);
      reorder(stage.actions, index, command.to_index);
      const nextIndex = stage.actions.indexOf(action);
      action.group_id = chooseGroup(stage.actions, nextIndex, command.group_mode);
      normalizeStage(stage);
      break;
    }
    case 'action.delete': {
      exact(command, ['type', 'stage_id', 'action_id']);
      const stage = findStage(structure, command.stage_id);
      stage.actions.splice(stage.actions.indexOf(findAction(stage, command.action_id)), 1);
      normalizeStage(stage);
      break;
    }
    case 'recovery.restore': {
      if (!Array.isArray(sourceStructure.recovery)) throw new Error('invalid_stage_recovery');
      const recovery = sourceStructure.recovery.find((item) => item.id === command.recovery_id);
      if (!recovery || !['stage', 'action'].includes(recovery.kind)) throw new Error('recovery_item_not_found');
      if (recovery.kind === 'stage') {
        exact(command, ['type', 'recovery_id', 'after_stage_id']);
        if (structure.stages.length >= 20) throw new Error('stage_count_limit');
        const source = recovery.snapshot.stage;
        const index = insertAfterIndex(structure.stages, command.after_stage_id);
        const copy = copyStage(source, structure.roles, fieldSources, 'recovery', recovery);
        normalizeStage(copy);
        structure.stages.splice(index, 0, copy);
      } else {
        exact(command, ['type', 'recovery_id', 'stage_id']);
        const stage = findStage(structure, command.stage_id);
        if (stage.actions.length >= 100) throw new Error('stage_action_limit');
        const oldAction = recovery.snapshot.action;
        const action = structuredClone(oldAction);
        action.id = randomUUID();
        action.group_id = randomUUID();
        reassignMissingRole(action, structure.roles);
        normalizeStage(stage);
        if (action.kind === 'move') action.path[0] = currentPosition(stage, action.role_id);
        fieldSources[actionDescriptionField(action.id)] = fromRecovery(recovery.id, actionDescriptionField(oldAction.id));
        stage.actions.push(action);
        normalizeStage(stage);
      }
      break;
    }
    default: throw new Error('unsupported_stage_command');
  }
  return { structure, fieldSources };
}

function makeAction(command, stage, roles, doc) {
  const base = { type: command.type, stage_id: command.stage_id, kind: command.kind, description: command.description, group_mode: command.group_mode };
  if (!['move', 'wait', 'observe', 'utility', 'text'].includes(command.kind)) throw new Error('invalid_action_kind');
  const isUtility = command.kind === 'utility';
  const expected = command.kind === 'move' ? [...Object.keys(base), 'role_id', 'path'] :
    command.kind === 'wait' ? [...Object.keys(base), 'role_id'] :
      command.kind === 'observe' ? [...Object.keys(base), 'role_id', 'target'] :
        command.kind === 'utility' ? [...Object.keys(base), 'thrower_role_id', 'utility_type', 'target'] :
          [...Object.keys(base), 'role_id', 'position'];
  exact(command, expected);
  validateStageTextValue(command.description);
  if (!command.description.trim()) throw new Error('empty_action_description');
  const roleId = isUtility ? command.thrower_role_id : command.role_id;
  if (command.kind !== 'text' && !roles.some((role) => role.id === roleId)) throw new Error('role_not_found');
  const action = { id: randomUUID(), group_id: chooseGroup(stage.actions, stage.actions.length, command.group_mode), kind: command.kind };
  if (command.kind === 'move') {
    if (!Array.isArray(command.path) || command.path.length < 2 || command.path.length > 50) throw new Error('invalid_move_path');
    action.role_id = roleId;
    action.path = command.path.map(point);
    action.path[0] = actionStartPosition(stage, roleId, command.group_mode);
  } else if (command.kind === 'wait') action.role_id = roleId;
  else if (command.kind === 'observe') { action.role_id = roleId; action.target = point(command.target); }
  else if (command.kind === 'utility') {
    if (!['smoke', 'flash', 'molotov', 'he', 'decoy'].includes(command.utility_type)) throw new Error('invalid_utility_type');
    action.thrower_role_id = roleId; action.utility_type = command.utility_type; action.target = point(command.target);
  } else {
    if (command.role_id !== null && !roles.some((role) => role.id === command.role_id)) throw new Error('role_not_found');
    action.role_id = command.role_id;
    action.position = point(command.position);
  }
  return action;
}

function copyStage(source, roles, fieldSources, sourceKind, recovery = null) {
  const copy = structuredClone(source);
  copy.id = randomUUID();
  copy.initial_positions = Object.fromEntries(roles.map((role) => [role.id, source.initial_positions[role.id] ? structuredClone(source.initial_positions[role.id]) : { x: 50, y: 50 }]));
  const groupMap = new Map();
  copy.actions = source.actions.map((oldAction) => {
    const action = structuredClone(oldAction);
    action.id = randomUUID();
    if (!groupMap.has(action.group_id)) groupMap.set(action.group_id, randomUUID());
    action.group_id = groupMap.get(action.group_id);
    reassignMissingRole(action, roles);
    fieldSources[actionDescriptionField(action.id)] = sourceKind === 'active'
      ? { kind: 'active', key: actionDescriptionField(oldAction.id) }
      : fromRecovery(recovery.id, actionDescriptionField(oldAction.id));
    return action;
  });
  fieldSources[stageNameField(copy.id)] = sourceKind === 'active' ? { kind: 'active', key: stageNameField(source.id) } : fromRecovery(recovery.id, stageNameField(source.id));
  fieldSources[stageDescriptionField(copy.id)] = sourceKind === 'active' ? { kind: 'active', key: stageDescriptionField(source.id) } : fromRecovery(recovery.id, stageDescriptionField(source.id));
  fieldSources[stageConditionField(copy.id)] = sourceKind === 'active' ? { kind: 'active', key: stageConditionField(source.id) } : fromRecovery(recovery.id, stageConditionField(source.id));
  return copy;
}

function chooseGroup(actions, index, mode) {
  if (!['parallel', 'after'].includes(mode)) throw new Error('invalid_group_mode');
  return mode === 'parallel' && index > 0 ? actions[index - 1].group_id : randomUUID();
}

function normalizeStage(stage) {
  const seenGroups = new Set();
  let previousGroup = null;
  for (const action of stage.actions) {
    if (action.group_id !== previousGroup) {
      if (seenGroups.has(action.group_id)) action.group_id = randomUUID();
      seenGroups.add(action.group_id);
      previousGroup = action.group_id;
    }
  }
  let positions = structuredClone(stage.initial_positions);
  for (let start = 0; start < stage.actions.length;) {
    let end = start + 1;
    while (end < stage.actions.length && stage.actions[end].group_id === stage.actions[start].group_id) end += 1;
    const groupStart = structuredClone(positions);
    const groupEnd = structuredClone(positions);
    for (let index = start; index < end; index += 1) {
      const action = stage.actions[index];
      if (action.kind !== 'move') continue;
      action.path[0] = structuredClone(groupStart[action.role_id]);
      groupEnd[action.role_id] = structuredClone(action.path.at(-1));
    }
    positions = groupEnd;
    start = end;
  }
}

function actionStartPosition(stage, roleId, groupMode) {
  let positions = structuredClone(stage.initial_positions);
  let lastGroupStart = structuredClone(positions);
  for (let start = 0; start < stage.actions.length;) {
    let end = start + 1;
    while (end < stage.actions.length && stage.actions[end].group_id === stage.actions[start].group_id) end += 1;
    lastGroupStart = structuredClone(positions);
    for (let index = start; index < end; index += 1) {
      const action = stage.actions[index];
      if (action.kind === 'move') positions[action.role_id] = structuredClone(action.path.at(-1));
    }
    start = end;
  }
  return structuredClone(groupMode === 'parallel' && stage.actions.length ? lastGroupStart[roleId] : positions[roleId]);
}

function groupIds(actions) { return [...new Set(actions.map((action) => action.group_id))]; }
function findStage(structure, id) { const stage = structure.stages.find((item) => item.id === id); if (!stage) throw new Error('stage_not_found'); return stage; }
function findAction(stage, id) { const action = stage.actions.find((item) => item.id === id); if (!action) throw new Error('action_not_found'); return action; }
function insertAfterIndex(stages, id) { if (id === null) return stages.length; const index = stages.findIndex((stage) => stage.id === id); if (index < 0) throw new Error('stage_not_found'); return index + 1; }
function reorder(items, from, to) { if (!Number.isInteger(to) || to < 0 || to >= items.length) throw new Error('invalid_order'); items.splice(to, 0, items.splice(from, 1)[0]); }
function firstFreeNumber(used) { for (let number = 1; number <= 5; number += 1) if (!used.includes(number)) return number; throw new Error('stage_role_limit'); }
function literal(value) { return { kind: 'literal', value }; }
function fromRecovery(id, key) { return { kind: 'recovery', recovery_id: id, key }; }
function exact(value, keys) { if (!value || typeof value !== 'object' || Array.isArray(value) || Object.keys(value).sort().join(',') !== [...keys].sort().join(',')) throw new Error('invalid_stage_command'); }
function point(value) { if (!value || typeof value !== 'object' || Array.isArray(value) || Object.keys(value).sort().join(',') !== 'x,y' || !Number.isFinite(value.x) || !Number.isFinite(value.y) || value.x < 0 || value.x > 100 || value.y < 0 || value.y > 100) throw new Error('invalid_stage_position'); return { x: value.x, y: value.y }; }
function currentPosition(stage, roleId) { let position = structuredClone(stage.initial_positions[roleId]); for (const action of stage.actions) if (action.kind === 'move' && action.role_id === roleId) position = structuredClone(action.path.at(-1)); return position; }
function reassignMissingRole(action, roles) { const id = action.kind === 'utility' ? action.thrower_role_id : action.role_id; if (id && !roles.some((role) => role.id === id)) { if (action.kind === 'utility') action.thrower_role_id = roles[0].id; else action.role_id = roles[0].id; } }
