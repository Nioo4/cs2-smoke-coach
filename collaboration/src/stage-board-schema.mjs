import * as Y from 'yjs';

export const STAGE_DOCUMENT_ID = 'mirage-stage-prototype';
export const STAGE_SCHEMA_VERSION = 1;
export const MAX_STAGE_ACTIONS = 100;
export const MAX_STAGE_COUNT = 20;
export const MAX_STAGE_ROLES = 5;
export const MAX_STAGE_TEXT_CHARS = 2000;
export const MAX_STAGE_STRUCTURE_BYTES = 1024 * 1024;
export const MAX_STAGE_DOCUMENT_BYTES = 4 * 1024 * 1024;
const TEST_DOCUMENT_ID = /^t0-stage-test-[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;

export function isStageDocumentId(documentId) {
  return documentId === STAGE_DOCUMENT_ID || TEST_DOCUMENT_ID.test(documentId ?? '');
}

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
const UTILITY_TYPES = new Set(['smoke', 'flash', 'molotov', 'he', 'decoy']);

export function createStageInitialData(documentId = STAGE_DOCUMENT_ID) {
  if (!isStageDocumentId(documentId)) throw new Error('unknown_document');
  const roleId = crypto.randomUUID();
  const stageId = crypto.randomUUID();
  const role = { id: roleId, number: 1, color: '#cb5a42' };
  const stage = { id: stageId, initial_positions: { [roleId]: { x: 50, y: 50 } }, actions: [] };
  const structure = {
    schema_kind: 'stage-board', schema_version: STAGE_SCHEMA_VERSION,
    document_id: documentId, map: 'mirage', roles: [role], stages: [stage], recovery: [],
  };
  return {
    epoch: crypto.randomUUID(), revision: 0, structure,
    fields: {
      [roleNameField(roleId)]: '角色 1',
      [stageNameField(stageId)]: '阶段 1',
      [stageDescriptionField(stageId)]: '',
      [stageConditionField(stageId)]: '',
    },
  };
}

export function roleNameField(roleId) { return `role.${roleId}.name`; }
export function stageNameField(stageId) { return `stage.${stageId}.name`; }
export function stageDescriptionField(stageId) { return `stage.${stageId}.description`; }
export function stageConditionField(stageId) { return `stage.${stageId}.condition`; }
export function actionDescriptionField(actionId) { return `action.${actionId}.description`; }

export function expectedStageTextFields(structure) {
  return [
    ...structure.roles.map((role) => roleNameField(role.id)),
    ...structure.stages.flatMap((stage) => [
      stageNameField(stage.id), stageDescriptionField(stage.id), stageConditionField(stage.id),
      ...stage.actions.map((action) => actionDescriptionField(action.id)),
    ]),
  ];
}

export function createStageYDoc(fields) {
  const doc = new Y.Doc();
  const root = doc.getMap('fields');
  doc.transact(() => {
    for (const [key, value] of Object.entries(fields)) {
      const text = new Y.Text();
      if (value) text.insert(0, value);
      root.set(key, text);
    }
  }, 'server-seed');
  return doc;
}

export function validateStageStructure(structure) {
  if (!structure || typeof structure !== 'object' || Array.isArray(structure) ||
      structure.schema_kind !== 'stage-board' || structure.schema_version !== STAGE_SCHEMA_VERSION ||
      !isStageDocumentId(structure.document_id) || structure.map !== 'mirage') throw new Error('invalid_stage_structure');
  if (!exactKeys(structure, ['schema_kind', 'schema_version', 'document_id', 'map', 'roles', 'stages', 'recovery'])) throw new Error('invalid_stage_structure');
  if (!Array.isArray(structure.roles) || structure.roles.length < 1 || structure.roles.length > MAX_STAGE_ROLES) throw new Error('invalid_stage_roles');
  if (!Array.isArray(structure.stages) || structure.stages.length < 1 || structure.stages.length > MAX_STAGE_COUNT) throw new Error('invalid_stage_count');
  if (!Array.isArray(structure.recovery) || structure.recovery.length > 500) throw new Error('invalid_stage_recovery');

  const roleIds = new Set();
  const roleNumbers = new Set();
  const entityIds = new Set();
  for (const role of structure.roles) {
    if (!role || !exactKeys(role, ['id', 'number', 'color']) || !UUID.test(role.id) || roleIds.has(role.id) || entityIds.has(role.id) || !Number.isInteger(role.number) || role.number < 1 || role.number > MAX_STAGE_ROLES || roleNumbers.has(role.number) || !/^#[0-9a-f]{6}$/i.test(role.color)) throw new Error('invalid_stage_role');
    roleIds.add(role.id);
    entityIds.add(role.id);
    roleNumbers.add(role.number);
  }

  const stageIds = new Set();
  const actionIds = new Set();
  for (const stage of structure.stages) {
    if (!stage || !exactKeys(stage, ['id', 'initial_positions', 'actions']) || !UUID.test(stage.id) || stageIds.has(stage.id) || entityIds.has(stage.id) || !Array.isArray(stage.actions) || stage.actions.length > MAX_STAGE_ACTIONS) throw new Error('invalid_stage');
    stageIds.add(stage.id);
    entityIds.add(stage.id);
    if (!stage.initial_positions || typeof stage.initial_positions !== 'object' || Array.isArray(stage.initial_positions) ||
        Object.keys(stage.initial_positions).length !== roleIds.size || Object.keys(stage.initial_positions).some((id) => !roleIds.has(id))) throw new Error('invalid_stage_positions');
    for (const roleId of roleIds) validatePoint(stage.initial_positions[roleId], 'invalid_stage_position');
    validateActions(stage, roleIds, actionIds, entityIds);
  }

  const recoveryIds = new Set();
  for (const item of structure.recovery) {
    if (!item || !exactKeys(item, ['id', 'kind', 'snapshot', 'texts']) || !UUID.test(item.id) || recoveryIds.has(item.id) || entityIds.has(item.id) || !['stage', 'action', 'role'].includes(item.kind) ||
        !item.snapshot || typeof item.snapshot !== 'object' || Array.isArray(item.snapshot) ||
        !item.texts || typeof item.texts !== 'object' || Array.isArray(item.texts)) throw new Error('invalid_stage_recovery_item');
    recoveryIds.add(item.id);
    entityIds.add(item.id);
    for (const value of Object.values(item.texts)) validateTextValue(value, false);
    const reserved = item.kind === 'stage' ? [item.snapshot.stage?.id, ...(item.snapshot.stage?.actions ?? []).map((action) => action.id)] : item.kind === 'action' ? [item.snapshot.action?.id] : [item.snapshot.role?.id];
    for (const id of reserved) {
      if (!UUID.test(id ?? '') || entityIds.has(id)) throw new Error('stage_identity_reused');
      entityIds.add(id);
    }
  }
  if (new TextEncoder().encode(JSON.stringify(structure)).byteLength > MAX_STAGE_STRUCTURE_BYTES) throw new Error('stage_structure_too_large');
}

export function validateStageTextDoc(doc, structure, baselineDoc = null) {
  if (doc.store?.pendingStructs || doc.store?.pendingDs) throw new Error('unresolved_yjs_dependencies');
  const sharedNames = [...doc.share.keys()];
  if (sharedNames.length !== 1 || sharedNames[0] !== 'fields') throw new Error('unexpected_shared_type');
  const root = doc.getMap('fields');
  if (!(root instanceof Y.Map)) throw new Error('invalid_text_root');
  const expected = expectedStageTextFields(structure).sort();
  const actual = [...root.keys()].sort();
  if (expected.length !== actual.length || expected.some((key, index) => key !== actual[index])) throw new Error('text_structure_changed');
  let totalBytes = 0;
  for (const key of expected) {
    const text = root.get(key);
    if (!(text instanceof Y.Text)) throw new Error('invalid_text_field');
    const previousText = baselineDoc?.share.get('fields')?.get(key);
    if (previousText && typeIdentity(previousText) !== typeIdentity(text)) throw new Error('text_field_identity_changed');
    if (text.toDelta().some((part) => typeof part.insert !== 'string' || part.attributes)) throw new Error('text_must_be_plain');
    const value = text.toString();
    validateTextValue(value, false);
    if (key.endsWith('.name') && !value.trim()) throw new Error('empty_stage_name');
    const actionId = key.startsWith('action.') ? key.split('.')[1] : null;
    if (actionId && !value.trim()) throw new Error('empty_action_description');
    totalBytes += new TextEncoder().encode(value).byteLength;
  }
  if (totalBytes > 1024 * 1024 || Y.encodeStateAsUpdate(doc).byteLength > MAX_STAGE_DOCUMENT_BYTES) throw new Error('stage_document_too_large');
}

export function projectStageFields(doc, structure) {
  validateStageTextDoc(doc, structure);
  const root = doc.getMap('fields');
  return Object.fromEntries(expectedStageTextFields(structure).map((key) => [key, root.get(key).toString()]));
}

export function validateStageTextValue(value) {
  validateTextValue(value, false);
  return value;
}

export function stageEndPositions(stage, roles = []) {
  const positions = structuredClone(stage.initial_positions);
  for (const action of stage.actions) {
    if (action.kind === 'move') positions[action.role_id] = structuredClone(action.path.at(-1));
  }
  for (const role of roles) positions[role.id] ??= { x: 50, y: 50 };
  return positions;
}

function validateActions(stage, roleIds, actionIds, entityIds) {
  const seenGroups = new Set();
  let previousGroup = null;
  const groupRoles = new Map();
  const positions = structuredClone(stage.initial_positions);
  let groupFinalPositions = null;
  for (const action of stage.actions) {
    if (!action || !UUID.test(action.id) || actionIds.has(action.id) || entityIds.has(action.id) || !UUID.test(action.group_id) || !['move', 'wait', 'observe', 'utility', 'text'].includes(action.kind)) throw new Error('invalid_stage_action');
    const keysByKind = {
      move: ['id', 'group_id', 'kind', 'role_id', 'path'],
      wait: ['id', 'group_id', 'kind', 'role_id'],
      observe: ['id', 'group_id', 'kind', 'role_id', 'target'],
      utility: ['id', 'group_id', 'kind', 'thrower_role_id', 'utility_type', 'target'],
      text: ['id', 'group_id', 'kind', 'role_id', 'position'],
    };
    if (!exactKeys(action, keysByKind[action.kind])) throw new Error('invalid_stage_action');
    actionIds.add(action.id);
    entityIds.add(action.id);
    const newGroup = action.group_id !== previousGroup;
    if (newGroup) {
      if (seenGroups.has(action.group_id)) throw new Error('invalid_action_group_order');
      if (groupFinalPositions) Object.assign(positions, groupFinalPositions);
      groupFinalPositions = structuredClone(positions);
      seenGroups.add(action.group_id);
      previousGroup = action.group_id;
      groupRoles.set(action.group_id, new Set());
    }
    const usesRole = action.kind !== 'text';
    const roleId = action.kind === 'utility' ? action.thrower_role_id : action.role_id;
    if (usesRole && (!roleIds.has(roleId) || groupRoles.get(action.group_id).has(roleId))) throw new Error('same_role_group_conflict');
    if (usesRole) groupRoles.get(action.group_id).add(roleId);
    if (action.kind === 'move') {
      if (!Array.isArray(action.path) || action.path.length < 2 || action.path.length > 50 || action.path.some((point) => !validPoint(point))) throw new Error('invalid_move_path');
      if (!samePoint(action.path[0], positions[roleId])) throw new Error('move_start_mismatch');
      groupFinalPositions[roleId] = structuredClone(action.path.at(-1));
    } else if (action.kind === 'wait') {
      if (!roleIds.has(roleId)) throw new Error('invalid_wait_action');
    } else if (action.kind === 'observe') {
      if (!validPoint(action.target)) throw new Error('invalid_observe_target');
    } else if (action.kind === 'utility') {
      if (!UTILITY_TYPES.has(action.utility_type) || !validPoint(action.target)) throw new Error('invalid_utility_action');
    } else if (action.kind === 'text') {
      if (!validPoint(action.position) || (action.role_id !== null && action.role_id !== undefined && !roleIds.has(action.role_id))) throw new Error('invalid_text_action');
    }
  }
}

function validatePoint(point, code) {
  if (!validPoint(point)) throw new Error(code);
}

function validPoint(point) {
  return Boolean(point && typeof point === 'object' && !Array.isArray(point) &&
    exactKeys(point, ['x', 'y']) &&
    typeof point.x === 'number' && Number.isFinite(point.x) && point.x >= 0 && point.x <= 100 &&
    typeof point.y === 'number' && Number.isFinite(point.y) && point.y >= 0 && point.y <= 100);
}

function exactKeys(value, keys) {
  return Boolean(value && typeof value === 'object' && !Array.isArray(value) && Object.keys(value).sort().join(',') === [...keys].sort().join(','));
}

function samePoint(left, right) {
  return Math.abs(left.x - right.x) < 1e-6 && Math.abs(left.y - right.y) < 1e-6;
}

function validateTextValue(value) {
  if (typeof value !== 'string' || [...value].length > MAX_STAGE_TEXT_CHARS || /[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f]/.test(value)) throw new Error('invalid_text');
}

function typeIdentity(type) {
  const id = type?._item?.id;
  if (!id || !Number.isSafeInteger(id.client) || !Number.isSafeInteger(id.clock)) return null;
  return `${id.client}:${id.clock}`;
}
