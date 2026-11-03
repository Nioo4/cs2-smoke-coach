import { createHash, randomUUID } from 'node:crypto';
import * as Y from 'yjs';
import { STAGE_DOCUMENT_ID, validateStageStructure } from './stage-board-schema.mjs';

export const FORMAL_UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;
export const sha256 = (bytes) => createHash('sha256').update(bytes).digest('hex');
export function canonicalJson(value) {
  const ordered = (item) => Array.isArray(item) ? item.map(ordered) : item && typeof item === 'object' ? Object.fromEntries(Object.keys(item).sort().map((key) => [key, ordered(item[key])])) : item;
  return JSON.stringify(ordered(value));
}
const exact = (value, keys) => value && !Array.isArray(value) && typeof value === 'object' && Object.keys(value).sort().join(',') === [...keys].sort().join(',');
const version = (value) => Number.isSafeInteger(value) && value >= 1;

export function validateFormalStructure(structure) {
  if (!exact(structure, ['schema_kind', 'schema_version', 'document_id', 'map', 'side', 'roles', 'stages']) || structure.schema_kind !== 'stage-board' || structure.schema_version !== 2 || !FORMAL_UUID.test(structure.document_id) || !['T', 'CT'].includes(structure.side) || !Array.isArray(structure.stages)) throw new Error('invalid_formal_structure');
  const legacy = structuredClone(structure);
  if (!Array.isArray(structure.roles) || structure.roles.some((role) => !FORMAL_UUID.test(role?.id))) throw new Error('invalid_formal_role');
  delete legacy.side;
  legacy.schema_version = 1; legacy.document_id = STAGE_DOCUMENT_ID; legacy.recovery = [];
  for (const stage of legacy.stages) {
    if (!exact(stage, ['id', 'version', 'initial_positions', 'actions', 'lineup_ids']) || !FORMAL_UUID.test(stage.id) || !version(stage.version) || !Array.isArray(stage.lineup_ids) || stage.lineup_ids.length > 10 || new Set(stage.lineup_ids).size !== stage.lineup_ids.length || stage.lineup_ids.some((id) => !FORMAL_UUID.test(id)) || !stage.initial_positions || !Array.isArray(stage.actions)) throw new Error('invalid_formal_stage');
    delete stage.version; delete stage.lineup_ids;
    for (const point of Object.values(stage.initial_positions)) {
      if (!exact(point, ['x', 'y', 'version']) || !version(point.version)) throw new Error('invalid_formal_position');
      delete point.version;
    }
    for (const action of stage.actions) {
      if (!version(action.version) || !FORMAL_UUID.test(action.id) || !FORMAL_UUID.test(action.group_id)) throw new Error('invalid_formal_action_version');
      delete action.version;
    }
  }
  validateStageStructure(legacy);
  if (Buffer.byteLength(JSON.stringify(structure)) > 1024 * 1024) throw new Error('formal_structure_too_large');
}

export function formalTextFields(structure) {
  return ['tactic.title', 'tactic.purpose', 'tactic.applicability',
    ...structure.roles.map((role) => `role.${role.id}.name`),
    ...structure.stages.flatMap((stage) => [`stage.${stage.id}.name`, `stage.${stage.id}.description`, `stage.${stage.id}.condition`, ...stage.actions.map((action) => `action.${action.id}.description`)])];
}

export function projectFormalDocument(doc) {
  if (doc.store.pendingStructs || doc.store.pendingDs || [...doc.share.keys()].sort().join(',') !== 'document,fields') throw new Error('invalid_formal_roots');
  const root = doc.getMap('document');
  if ([...root.keys()].join(',') !== 'structure') throw new Error('invalid_formal_document_root');
  const structure = root.get('structure');
  validateFormalStructure(structure);
  const texts = doc.getMap('fields');
  const expected = formalTextFields(structure).sort();
  if ([...texts.keys()].sort().join(',') !== expected.join(',')) throw new Error('formal_text_fields_changed');
  const fields = {};
  for (const key of expected) {
    const text = texts.get(key);
    if (!(text instanceof Y.Text) || text.toDelta().some((part) => typeof part.insert !== 'string' || part.attributes)) throw new Error('formal_text_must_be_plain');
    const value = text.toString();
    if ([...value].length > (key === 'tactic.title' ? 100 : 2000) || /[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f]/.test(value)) throw new Error('invalid_formal_text');
    if ((key === 'tactic.title' || key.endsWith('.name') || key.startsWith('action.')) && !value.trim()) throw new Error('empty_formal_text');
    fields[key] = value;
  }
  if (Object.values(fields).reduce((sum, value) => sum + Buffer.byteLength(value), 0) > 1024 * 1024 || Y.encodeStateAsUpdate(doc).length > 4 * 1024 * 1024) throw new Error('formal_document_too_large');
  return { structure: structuredClone(structure), fields };
}

export function encodeFormalDocument(doc) {
  const projection = projectFormalDocument(doc);
  const state = Buffer.from(Y.encodeStateAsUpdate(doc));
  // The digest covers these exact UTF-8 bytes, avoiding cross-language float formatting ambiguity.
  const projectionJson = canonicalJson(projection);
  return { state_base64: state.toString('base64'), projection, projection_json: projectionJson, state_sha256: sha256(state), projection_sha256: sha256(projectionJson) };
}

export function createFormalSeed(input) {
  if (!exact(input, ['document_id', 'title', 'side']) || !FORMAL_UUID.test(input.document_id) || typeof input.title !== 'string' || !input.title.trim() || [...input.title].length > 100 || !['T', 'CT'].includes(input.side)) throw new Error('invalid_seed_request');
  const roleId = randomUUID(); const stageId = randomUUID();
  const structure = { schema_kind: 'stage-board', schema_version: 2, document_id: input.document_id, map: 'mirage', side: input.side,
    roles: [{ id: roleId, number: 1, color: '#cb5a42' }],
    stages: [{ id: stageId, version: 1, initial_positions: { [roleId]: { x: 50, y: 50, version: 1 } }, actions: [], lineup_ids: [] }] };
  const fields = { 'tactic.title': input.title, 'tactic.purpose': '', 'tactic.applicability': '', [`role.${roleId}.name`]: '角色 1', [`stage.${stageId}.name`]: '阶段 1', [`stage.${stageId}.description`]: '', [`stage.${stageId}.condition`]: '' };
  const doc = new Y.Doc();
  try {
    doc.getMap('document').set('structure', structure);
    for (const [key, value] of Object.entries(fields)) {
      const text = new Y.Text(); if (value) text.insert(0, value); doc.getMap('fields').set(key, text);
    }
    return encodeFormalDocument(doc);
  } finally { doc.destroy(); }
}

export function copyFormalProjection(input) {
  if (!exact(input, ['document_id', 'projection']) || !FORMAL_UUID.test(input.document_id) || !exact(input.projection, ['structure', 'fields'])) throw new Error('invalid_copy_request');
  validateFormalStructure(input.projection.structure);
  const structure = structuredClone(input.projection.structure); const ids = new Map();
  const fresh = (id) => { if (!ids.has(id)) ids.set(id, randomUUID()); return ids.get(id); };
  structure.document_id = input.document_id;
  for (const role of structure.roles) role.id = fresh(role.id);
  for (const stage of structure.stages) {
    stage.id = fresh(stage.id); stage.version = 1;
    stage.initial_positions = Object.fromEntries(Object.entries(stage.initial_positions).map(([id, point]) => [fresh(id), { ...point, version: 1 }]));
    for (const action of stage.actions) { action.id = fresh(action.id); if (action.role_id != null) action.role_id = fresh(action.role_id); action.group_id = fresh(action.group_id); action.version = 1; }
  }
  const doc = new Y.Doc();
  try {
    doc.getMap('document').set('structure', structure);
    for (const [key, value] of Object.entries(input.projection.fields)) {
      const parts = key.split('.'); if (parts.length === 3) parts[1] = ids.get(parts[1]) ?? parts[1];
      if (typeof value !== 'string') throw new Error('invalid_copy_text');
      const text = new Y.Text(); if (value) text.insert(0, value); doc.getMap('fields').set(parts.join('.'), text);
    }
    return encodeFormalDocument(doc);
  } finally { doc.destroy(); }
}
