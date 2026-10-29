import * as Y from 'yjs';

export const DEFAULT_DOCUMENT_ID = 'mirage-prototype';
export const MAX_DOCUMENT_BYTES = 4 * 1024 * 1024;
export const MAX_STRUCTURE_BYTES = 1024 * 1024;
export const MAX_TEXT_BYTES = 1024 * 1024;
export const MAX_TEXT_CHARS = 2000;
const PG_FIXTURE_ID = /^pg-(?:restart|timeout)-[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;

export function isPrototypeDocumentId(documentId) {
  return documentId === DEFAULT_DOCUMENT_ID || PG_FIXTURE_ID.test(documentId ?? '');
}

const initialFields = {
  'tactic.title': '协作机制原型',
  'tactic.purpose': '非游戏验证内容：此页面只验证文字协作、地图对象租约与保存恢复。',
  'step-1.role-a': '示例文字：两位参与者可以同时编辑这段非游戏验证内容。',
  'step-1.role-b': '示例文字：这里不是正式战术、点位教学或游戏内投法。',
  'step-1.notes': '检查：中文输入合并、彩色编辑位置与 PostgreSQL 已保存提示。',
  'step-2.role-a': '步骤地图彼此独立。拖动角色或箭头会申请短期租约。',
  'step-2.role-b': '同一对象由一人拖动时，另一位参与者会看到占用状态。',
  'step-2.notes': '断开连接后租约释放；超时租约也会自动过期。',
};

function marker(id, kind, label, x, y, version = 1) {
  return { id, kind, label, x, y, version };
}

export function createInitialData(documentId = DEFAULT_DOCUMENT_ID) {
  return {
    epoch: crypto.randomUUID(),
    revision: 0,
    structure: {
      document_id: documentId,
      map: 'mirage',
      roles: [
        { id: 'role-a', name: '角色 A', color: '#b64027' },
        { id: 'role-b', name: '角色 B', color: '#315d57' },
      ],
      steps: [
        {
          id: 'step-1',
          title: '步骤 1 · 中文文本协作',
          trigger: '非游戏验证内容',
          task_fields: { 'role-a': 'step-1.role-a', 'role-b': 'step-1.role-b' },
          notes_field: 'step-1.notes',
          objects: [
            marker('role-a-start', 'role', '角色 A', 27, 72),
            marker('role-b-start', 'role', '角色 B', 33, 77),
            { id: 'arrow-a-mid', kind: 'arrow', label: '移动路径', points: [{ x: 33, y: 77 }, { x: 48, y: 62 }, { x: 57, y: 47 }], version: 1 },
            marker('smoke-marker', 'smoke', '图标演示', 58, 46),
          ],
        },
        {
          id: 'step-2',
          title: '步骤 2 · 地图租约',
          trigger: '非游戏验证内容',
          task_fields: { 'role-a': 'step-2.role-a', 'role-b': 'step-2.role-b' },
          notes_field: 'step-2.notes',
          objects: [
            marker('role-a-hold', 'role', '角色 A', 66, 64),
            marker('role-b-hold', 'role', '角色 B', 72, 68),
            { id: 'arrow-b-site', kind: 'arrow', label: '移动路径', points: [{ x: 72, y: 68 }, { x: 62, y: 55 }], version: 1 },
            marker('flash-marker', 'flash', '道具图标演示', 62, 54),
          ],
        },
      ],
    },
    fields: { ...initialFields },
  };
}

export function expectedTextFields(structure) {
  const names = ['tactic.title', 'tactic.purpose'];
  for (const step of structure.steps) {
    names.push(...Object.values(step.task_fields), step.notes_field);
  }
  return names;
}

export function createYDoc(fields) {
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

function validCoordinate(value) {
  return typeof value === 'number' && Number.isFinite(value) && value >= 0 && value <= 100;
}

export function validateStructure(structure) {
  if (!structure || typeof structure !== 'object' || Array.isArray(structure)) throw new Error('invalid_structure');
  if (typeof structure.document_id !== 'string' || structure.document_id.length > 128 || structure.map !== 'mirage') throw new Error('invalid_structure');
  if (!Array.isArray(structure.roles) || structure.roles.length < 1 || structure.roles.length > 5) throw new Error('invalid_roles');
  if (!Array.isArray(structure.steps) || structure.steps.length < 1 || structure.steps.length > 20) throw new Error('invalid_steps');
  const roleIds = new Set();
  for (const role of structure.roles) {
    if (!role || !/^[a-z0-9-]{1,64}$/.test(role.id) || roleIds.has(role.id) || typeof role.name !== 'string' || role.name.length > 32 || !/^#[0-9a-f]{6}$/i.test(role.color)) throw new Error('invalid_role');
    roleIds.add(role.id);
  }
  const stepIds = new Set();
  const objectIds = new Set();
  for (const step of structure.steps) {
    if (!step || !/^[a-z0-9-]{1,64}$/.test(step.id) || stepIds.has(step.id) || typeof step.title !== 'string' || typeof step.trigger !== 'string') throw new Error('invalid_step');
    stepIds.add(step.id);
    if (!step.task_fields || typeof step.task_fields !== 'object' || Array.isArray(step.task_fields) || !step.notes_field) throw new Error('invalid_step_fields');
    if (Object.keys(step.task_fields).length !== roleIds.size || Object.keys(step.task_fields).some((id) => !roleIds.has(id))) throw new Error('invalid_step_roles');
    if (!Array.isArray(step.objects) || step.objects.length > 100) throw new Error('invalid_objects');
    for (const object of step.objects) {
      if (!object || !/^[a-z0-9-]{1,64}$/.test(object.id) || objectIds.has(object.id) || !Number.isSafeInteger(object.version) || object.version < 1) throw new Error('invalid_object');
      objectIds.add(object.id);
      if (!['role', 'arrow', 'smoke', 'flash', 'fire', 'he', 'decoy', 'note'].includes(object.kind)) throw new Error('invalid_object_kind');
      if (object.kind === 'arrow') {
        if (!Array.isArray(object.points) || object.points.length < 2 || object.points.length > 50 || object.points.some((point) => !validCoordinate(point.x) || !validCoordinate(point.y))) throw new Error('invalid_arrow');
      } else if (!validCoordinate(object.x) || !validCoordinate(object.y)) {
        throw new Error('invalid_object_position');
      }
    }
  }
  if (Buffer.byteLength(JSON.stringify(structure), 'utf8') > MAX_STRUCTURE_BYTES) throw new Error('structure_too_large');
}

export function validateTextDoc(doc, structure, baselineDoc = null) {
  if (doc.store?.pendingStructs || doc.store?.pendingDs) throw new Error('unresolved_yjs_dependencies');
  const sharedNames = [...doc.share.keys()];
  if (sharedNames.length !== 1 || sharedNames[0] !== 'fields') throw new Error('unexpected_shared_type');
  const root = doc.getMap('fields');
  if (!(root instanceof Y.Map)) throw new Error('invalid_text_root');
  const expected = expectedTextFields(structure).sort();
  const actual = [...root.keys()].sort();
  if (expected.length !== actual.length || expected.some((key, index) => key !== actual[index])) throw new Error('text_structure_changed');
  let totalBytes = 0;
  for (const key of expected) {
    const text = root.get(key);
    if (!(text instanceof Y.Text)) throw new Error('invalid_text_field');
    const previousText = baselineDoc?.share.get('fields')?.get(key);
    if (previousText && typeIdentity(previousText) !== typeIdentity(text)) throw new Error('text_field_identity_changed');
    const delta = text.toDelta();
    if (delta.some((part) => typeof part.insert !== 'string' || part.attributes)) throw new Error('text_must_be_plain');
    const value = text.toString();
    if ([...value].length > MAX_TEXT_CHARS || /[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f]/.test(value)) throw new Error('invalid_text');
    totalBytes += Buffer.byteLength(value, 'utf8');
  }
  if (totalBytes > MAX_TEXT_BYTES || Y.encodeStateAsUpdate(doc).byteLength > MAX_DOCUMENT_BYTES) throw new Error('document_too_large');
}

function typeIdentity(type) {
  const id = type?._item?.id;
  if (!id || !Number.isSafeInteger(id.client) || !Number.isSafeInteger(id.clock)) return null;
  return `${id.client}:${id.clock}`;
}

export function projectFields(doc, structure) {
  validateTextDoc(doc, structure);
  const root = doc.getMap('fields');
  return Object.fromEntries(expectedTextFields(structure).map((key) => [key, root.get(key).toString()]));
}
