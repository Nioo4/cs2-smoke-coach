import { createHash, randomUUID } from 'node:crypto';
import * as Y from 'yjs';
import { LeaseManager, LeaseError } from './leases.mjs';
import { DEFAULT_DOCUMENT_ID, expectedTextFields, isPrototypeDocumentId, validateStructure, validateTextDoc, projectFields } from './prototype-schema.mjs';
import {
  STAGE_DOCUMENT_ID, actionDescriptionField, expectedStageTextFields, projectStageFields,
  roleNameField, stageDescriptionField, stageConditionField, stageNameField,
  isStageDocumentId, validateStageStructure, validateStageTextDoc, validateStageTextValue,
} from './stage-board-schema.mjs';
import { buildStageCommand } from './stage-commands.mjs';

const OPERATION_ID = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
const MAX_UPDATE_BYTES = 1024 * 1024;

export class CollaborationError extends Error {
  constructor(code, details = {}) {
    super(code);
    this.code = code;
    this.details = details;
  }
}

export class CollaborationCore {
  constructor({ store, authorize = async () => true, now = Date.now }) {
    this.store = store;
    this.authorize = authorize;
    this.now = now;
    this.rooms = new Map();
    this.roomLoads = new Map();
  }

  async room(documentId = DEFAULT_DOCUMENT_ID) {
    if (this.rooms.has(documentId)) return this.rooms.get(documentId);
    if (!this.roomLoads.has(documentId)) {
      const loading = (async () => {
        const saved = await this.store.loadOrCreate(documentId);
        const doc = new Y.Doc();
        Y.applyUpdate(doc, new Uint8Array(saved.state), 'postgres-load');
        validateDocument(doc, saved.structure, documentId);
        const room = {
          documentId, epoch: saved.epoch, revision: saved.revision, structure: saved.structure,
          doc, clients: new Map(), leaseManager: new LeaseManager({ now: this.now }), queue: Promise.resolve(),
        };
        this.rooms.set(documentId, room);
        return room;
      })();
      this.roomLoads.set(documentId, loading);
      loading.finally(() => this.roomLoads.delete(documentId)).catch(() => {});
    }
    return this.roomLoads.get(documentId);
  }

  async connect({ documentId = DEFAULT_DOCUMENT_ID, connectionId = randomUUID(), actor, send }) {
    const room = await this.room(documentId);
    const connection = { id: connectionId, actor, send, room, closed: false };
    room.clients.set(connectionId, connection);
    send({
      type: 'sync', document_id: documentId, epoch: room.epoch, revision: room.revision,
      state: Buffer.from(Y.encodeStateAsUpdate(room.doc)).toString('base64'),
      structure: room.structure, fields: projectDocumentFields(room.doc, room.structure, documentId),
      participants: participants(room), locks: room.leaseManager.snapshot(),
    });
    this.broadcast(room, { type: 'participants', participants: participants(room) });
    return connection;
  }

  disconnect(connection) {
    if (!connection || connection.closed) return;
    connection.closed = true;
    const { room } = connection;
    room.clients.delete(connection.id);
    room.leaseManager.releaseConnection(connection.id);
    this.broadcast(room, { type: 'locks', locks: room.leaseManager.snapshot() });
    this.broadcast(room, { type: 'participants', participants: participants(room) });
  }

  async presence(connection, data) {
    if (!connection || connection.closed) throw new CollaborationError('connection_closed');
    if (!data || typeof data !== 'object' || !/^[a-z0-9-]{1,64}$/.test(data.step_id ?? '') ||
        (data.field !== undefined && (typeof data.field !== 'string' || !expectedFields(connection.room).includes(data.field))) ||
        (data.relative_cursor !== undefined && (typeof data.relative_cursor !== 'string' || data.relative_cursor.length > 256 || !/^(?:[A-Za-z0-9+/]{4})*(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?$/.test(data.relative_cursor))) ||
        (data.cursor !== undefined && (!Number.isSafeInteger(data.cursor) || data.cursor < 0 || data.cursor > 2000))) {
      throw new CollaborationError('invalid_presence');
    }
    connection.presence = { step_id: data.step_id, field: data.field ?? null, relative_cursor: data.relative_cursor ?? null };
    this.broadcast(connection.room, { type: 'participants', participants: participants(connection.room) });
  }

  acquireLease(connection, payload) {
    const room = connection.room;
    const object = findObject(room.structure, payload?.step_id, payload?.object_id);
    if (!object) throw new CollaborationError('object_not_found');
    const lease = room.leaseManager.acquire({
      objectId: object.id, version: payload.version, connectionId: connection.id, actor: connection.actor, currentVersion: object.version,
    });
    this.broadcast(room, { type: 'locks', locks: room.leaseManager.snapshot() });
    return lease;
  }

  renewLease(connection, payload) {
    const lease = connection.room.leaseManager.renew({ objectId: payload.object_id, token: payload.token, connectionId: connection.id });
    this.broadcast(connection.room, { type: 'locks', locks: connection.room.leaseManager.snapshot() });
    return lease;
  }

  preview(connection, payload) {
    const accepted = connection.room.leaseManager.preview({
      objectId: payload.object_id, token: payload.token, connectionId: connection.id, x: payload.x, y: payload.y,
    });
    if (accepted) this.broadcast(connection.room, { type: 'map.preview', actor_id: connection.actor.id, object_id: payload.object_id, x: payload.x, y: payload.y });
    return accepted;
  }

  releaseLease(connection, payload) {
    const released = connection.room.leaseManager.release({ objectId: payload.object_id, token: payload.token, connectionId: connection.id });
    this.broadcast(connection.room, { type: 'locks', locks: connection.room.leaseManager.snapshot() });
    return released;
  }

  operation(connection, message) {
    const { room } = connection;
    const run = room.queue.then(() => this.applyOperation(connection, message));
    room.queue = run.catch(() => {});
    return run;
  }

  async applyOperation(connection, message) {
    const { room } = connection;
    if (connection.closed) throw new CollaborationError('connection_closed');
    if (!message || !OPERATION_ID.test(message.operation_id ?? '') || typeof message.epoch !== 'string' || !message.payload || typeof message.payload !== 'object') {
      throw new CollaborationError('invalid_operation');
    }
    if (message.epoch !== room.epoch) throw new CollaborationError('stale_epoch');
    if (!(await this.authorize(connection, message))) throw new CollaborationError('forbidden');
    const fingerprint = fingerprintOperation(message.type, message.epoch, message.payload);
    const prior = await this.store.findOperation(room.documentId, message.operation_id);
    if (prior) {
      if (prior.epoch !== message.epoch || prior.actorId !== connection.actor.id || prior.fingerprint !== fingerprint) throw new CollaborationError('operation_id_reused');
      if (prior.revision > room.revision) {
        const saved = await this.store.loadOrCreate(room.documentId);
        if (saved.epoch !== prior.epoch || saved.revision < prior.revision) throw new CollaborationError('persistence_state_unavailable');
        const recoveredDoc = new Y.Doc();
        Y.applyUpdate(recoveredDoc, new Uint8Array(saved.state), 'postgres-reconcile');
      validateDocument(recoveredDoc, saved.structure, room.documentId, room.doc);
        const oldDoc = room.doc;
        room.doc = recoveredDoc;
        room.epoch = saved.epoch;
        room.revision = saved.revision;
        room.structure = saved.structure;
        oldDoc.destroy();
        this.broadcast(room, this.syncMessage(room));
      }
      connection.send(this.operationAck(room, message.operation_id, prior.epoch, prior.revision));
      return { duplicate: true, revision: prior.revision };
    }
    let nextDoc = null;
    let nextStructure = room.structure;
    let update = null;
    try {
      if (message.type === 'text.update') {
        update = decodeUpdate(message.payload.update);
        nextDoc = cloneDoc(room.doc);
        Y.applyUpdate(nextDoc, update, connection.id);
        if (isStageDocumentId(room.documentId)) assertStageTextUpdateScope(nextDoc, room.doc, update, room.structure);
        validateDocument(nextDoc, room.structure, room.documentId, room.doc);
      } else if (message.type === 'map.move') {
        if (room.documentId !== DEFAULT_DOCUMENT_ID) throw new CollaborationError('unsupported_operation');
        nextStructure = movedStructure(room, connection, message.payload);
        validateStructure(nextStructure);
        nextDoc = cloneDoc(room.doc);
      } else if (message.type === 'stage.command') {
        if (!isStageDocumentId(room.documentId)) throw new CollaborationError('unsupported_operation');
        if (!Number.isSafeInteger(message.payload.expected_revision) || message.payload.expected_revision !== room.revision) throw new CollaborationError('revision_conflict');
        if (Object.keys(message.payload).sort().join(',') !== 'command,expected_revision') {
          throw new CollaborationError('invalid_stage_mutation');
        }
        const prepared = prepareStageCommand(room, message.payload.command);
        nextStructure = prepared.structure;
        validateStageStructure(nextStructure);
        nextDoc = cloneDoc(room.doc);
        reconcileStageFields(nextDoc, room.doc, room.structure, nextStructure, prepared.fieldSources);
        validateStageTextDoc(nextDoc, nextStructure, room.doc);
      } else {
        throw new CollaborationError('unsupported_operation');
      }
      const state = Buffer.from(Y.encodeStateAsUpdate(nextDoc));
      const result = await this.store.persist({
        documentId: room.documentId, epoch: room.epoch, expectedRevision: room.revision,
        operationId: message.operation_id, actorId: connection.actor.id, fingerprint, type: message.type,
        state, structure: nextStructure,
      });
      if (result.duplicate) {
        connection.send(this.operationAck(room, message.operation_id, room.epoch, result.revision));
        return { duplicate: true, revision: result.revision };
      }
      const oldDoc = room.doc;
      room.doc = nextDoc;
      nextDoc = null;
      room.structure = nextStructure;
      room.revision = result.revision;
      oldDoc.destroy();
      const applied = {
        type: 'operation.applied', operation_id: message.operation_id, epoch: room.epoch,
        revision: room.revision, actor_id: connection.actor.id, operation_type: message.type,
        ...(message.type === 'text.update' ? { update: Buffer.from(update).toString('base64') } : { structure: room.structure }),
        ...(message.type === 'stage.command' ? { state: state.toString('base64') } : {}),
      };
      this.broadcast(room, applied);
      return { duplicate: false, revision: room.revision };
    } finally {
      nextDoc?.destroy();
    }
  }

  reject(connection, message, error) {
    const code = error instanceof LeaseError ? error.code : error instanceof CollaborationError ? error.code : error?.message === 'stale_epoch' ? 'stale_epoch' : error?.message === 'revision_conflict' ? 'revision_conflict' : error?.message === 'operation_id_reused' ? 'operation_id_reused' : 'save_failed';
    connection.send({ type: 'operation.rejected', operation_id: message?.operation_id ?? null, code, details: error?.details ?? {} });
    connection.send(this.syncMessage(connection.room));
  }

  syncMessage(room) {
    return {
      type: 'sync', document_id: room.documentId, epoch: room.epoch, revision: room.revision,
      state: Buffer.from(Y.encodeStateAsUpdate(room.doc)).toString('base64'),
      structure: room.structure, fields: projectDocumentFields(room.doc, room.structure, room.documentId),
      participants: participants(room), locks: room.leaseManager.snapshot(),
    };
  }

  operationAck(room, operationId, epoch, revision) {
    return {
      type: 'operation.ack', operation_id: operationId, epoch, revision, duplicate: true,
      state: Buffer.from(Y.encodeStateAsUpdate(room.doc)).toString('base64'), structure: room.structure,
    };
  }

  broadcast(room, message) {
    for (const client of room.clients.values()) client.send(message);
  }
}

function cloneDoc(source) {
  const clone = new Y.Doc();
  Y.applyUpdate(clone, Y.encodeStateAsUpdate(source), 'candidate-clone');
  return clone;
}

function decodeUpdate(value) {
  if (typeof value !== 'string' || value.length > MAX_UPDATE_BYTES * 2 || !/^(?:[A-Za-z0-9+/]{4})*(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?$/.test(value)) throw new CollaborationError('invalid_update');
  const bytes = Buffer.from(value, 'base64');
  if (!bytes.length || bytes.length > MAX_UPDATE_BYTES || bytes.toString('base64') !== value) throw new CollaborationError('invalid_update');
  return new Uint8Array(bytes);
}

function fingerprintOperation(type, epoch, payload) {
  const stable = (value) => Array.isArray(value) ? `[${value.map(stable).join(',')}]` : value && typeof value === 'object'
    ? `{${Object.keys(value).sort().map((key) => `${JSON.stringify(key)}:${stable(value[key])}`).join(',')}}`
    : JSON.stringify(value);
  return createHash('sha256').update(`${type}\n${epoch}\n${stable(payload)}`).digest('hex');
}

function findObject(structure, stepId, objectId) {
  const step = structure.steps.find((item) => item.id === stepId);
  return step?.objects.find((object) => object.id === objectId) ?? null;
}

function movedStructure(room, connection, payload) {
  if (!payload || Object.keys(payload).sort().join(',') !== 'object_id,step_id,token,version,x,y' ||
      typeof payload.token !== 'string' || !Number.isSafeInteger(payload.version) ||
      ![payload.x, payload.y].every((value) => typeof value === 'number' && Number.isFinite(value) && value >= 0 && value <= 100)) {
    throw new CollaborationError('invalid_map_move');
  }
  const next = structuredClone(room.structure);
  const object = findObject(next, payload.step_id, payload.object_id);
  if (!object) throw new CollaborationError('object_not_found');
  room.leaseManager.assertHeld({ objectId: object.id, token: payload.token, connectionId: connection.id, version: payload.version, currentVersion: object.version });
  if (object.kind === 'arrow') {
    const xs = object.points.map((point) => point.x);
    const ys = object.points.map((point) => point.y);
    const centerX = (Math.min(...xs) + Math.max(...xs)) / 2;
    const centerY = (Math.min(...ys) + Math.max(...ys)) / 2;
    const dx = payload.x - centerX;
    const dy = payload.y - centerY;
    if (object.points.some((point) => point.x + dx < 0 || point.x + dx > 100 || point.y + dy < 0 || point.y + dy > 100)) throw new CollaborationError('invalid_map_move');
    object.points = object.points.map((point) => ({ x: point.x + dx, y: point.y + dy }));
  } else {
    object.x = payload.x;
    object.y = payload.y;
  }
  object.version += 1;
  return next;
}

function participants(room) {
  return [...room.clients.values()].map((connection) => ({
    id: connection.actor.id, name: connection.actor.name, color: connection.actor.color,
    ...(connection.presence ?? { step_id: 'step-1', field: null, cursor: null }),
  }));
}

function validateDocument(doc, structure, documentId, baselineDoc = null) {
  if (isStageDocumentId(documentId)) {
    if (structure?.document_id !== documentId) throw new Error('document_id_mismatch');
    validateStageStructure(structure);
    return validateStageTextDoc(doc, structure, baselineDoc);
  }
  if (!isPrototypeDocumentId(documentId)) throw new Error('unknown_document');
  validateStructure(structure);
  return validateTextDoc(doc, structure, baselineDoc);
}

function projectDocumentFields(doc, structure, documentId) {
  return isStageDocumentId(documentId) ? projectStageFields(doc, structure) : projectFields(doc, structure);
}

function expectedFields(room) {
  return isStageDocumentId(room.documentId) ? expectedStageTextFields(room.structure) : expectedTextFields(room.structure);
}

function prepareStageCommand(room, command) {
  const prepared = buildStageCommand(room.structure, command, room.doc);
  const recovered = [...room.structure.recovery, ...removedContent(room, prepared.structure)];
  if (recovered.length > 500) throw new CollaborationError('stage_recovery_limit');
  prepared.structure.recovery = recovered;
  if (prepared.structure.document_id !== room.documentId) throw new CollaborationError('document_id_mismatch');
  return prepared;
}

function removedContent(room, nextStructure) {
  const output = [];
  const nextStages = new Map(nextStructure.stages.map((stage) => [stage.id, stage]));
  const nextRoles = new Set(nextStructure.roles.map((role) => role.id));
  const root = room.doc.getMap('fields');
  const value = (key) => root.get(key)?.toString() ?? '';
  for (const role of room.structure.roles) {
    if (!nextRoles.has(role.id)) output.push({
      id: randomUUID(), kind: 'role', snapshot: { role: structuredClone(role) },
      texts: { [roleNameField(role.id)]: value(roleNameField(role.id)) },
    });
  }
  for (const stage of room.structure.stages) {
    const nextStage = nextStages.get(stage.id);
    if (!nextStage) {
      const texts = {
        [stageNameField(stage.id)]: value(stageNameField(stage.id)),
        [stageDescriptionField(stage.id)]: value(stageDescriptionField(stage.id)),
        [stageConditionField(stage.id)]: value(stageConditionField(stage.id)),
      };
      for (const action of stage.actions) texts[actionDescriptionField(action.id)] = value(actionDescriptionField(action.id));
      output.push({ id: randomUUID(), kind: 'stage', snapshot: { stage: structuredClone(stage) }, texts });
      continue;
    }
    const nextActionIds = new Set(nextStage.actions.map((action) => action.id));
    for (const action of stage.actions) {
      if (!nextActionIds.has(action.id)) output.push({
        id: randomUUID(), kind: 'action', snapshot: { stage_id: stage.id, action: structuredClone(action) },
        texts: { [actionDescriptionField(action.id)]: value(actionDescriptionField(action.id)) },
      });
    }
  }
  return output;
}

function reconcileStageFields(nextDoc, baselineDoc, oldStructure, nextStructure, sources) {
  if (!sources || typeof sources !== 'object' || Array.isArray(sources)) throw new CollaborationError('invalid_stage_field_sources');
  const oldFields = new Set(expectedStageTextFields(oldStructure));
  const nextFields = new Set(expectedStageTextFields(nextStructure));
  const additions = new Set([...nextFields].filter((key) => !oldFields.has(key)));
  if (Object.keys(sources).some((key) => !additions.has(key))) throw new CollaborationError('invalid_stage_field_sources');
  const root = nextDoc.getMap('fields');
  for (const key of oldFields) if (!nextFields.has(key)) root.delete(key);
  for (const key of additions) {
    const textValue = stageFieldValue(sources[key], oldFields, oldStructure.recovery, baselineDoc);
    validateStageTextValue(textValue);
    const text = new Y.Text();
    if (textValue) text.insert(0, textValue);
    root.set(key, text);
  }
}

function stageFieldValue(source, oldFields, recovery, baselineDoc) {
  if (source === undefined) return '';
  if (!source || typeof source !== 'object' || Array.isArray(source)) throw new CollaborationError('invalid_stage_field_source');
  if (source.kind === 'literal' && Object.keys(source).sort().join(',') === 'kind,value' && typeof source.value === 'string') return source.value;
  if (source.kind === 'active' && Object.keys(source).sort().join(',') === 'key,kind' && oldFields.has(source.key)) {
    const match = source.key.match(/^(?:role\.[0-9a-f-]+\.name|stage\.[0-9a-f-]+\.(?:name|description|condition)|action\.[0-9a-f-]+\.description)$/i);
    if (!match) throw new CollaborationError('invalid_stage_field_source');
    return baselineDoc.getMap('fields').get(source.key)?.toString() ?? '';
  }
  if (source.kind === 'recovery' && Object.keys(source).sort().join(',') === 'key,kind,recovery_id' && typeof source.key === 'string') {
    const item = recovery.find((entry) => entry.id === source.recovery_id);
    if (!item || !Object.hasOwn(item.texts, source.key)) throw new CollaborationError('invalid_stage_field_source');
    return item.texts[source.key];
  }
  throw new CollaborationError('invalid_stage_field_source');
}

function assertStageTextUpdateScope(candidateDoc, baselineDoc, update, structure) {
  const root = baselineDoc.getMap('fields');
  const permittedTypes = new Set(expectedStageTextFields(structure).map((key) => typeIdentity(root.get(key))));
  const deleteRanges = new Map();
  const priorDeletes = Y.decodeUpdate(Y.encodeStateAsUpdate(baselineDoc)).ds.clients;
  for (const type of expectedStageTextFields(structure).map((key) => root.get(key))) {
    for (const structs of baselineDoc.store.clients.values()) {
      for (const struct of structs) {
        if (struct.parent === type) {
          const ranges = deleteRanges.get(struct.id.client) ?? [];
          ranges.push([struct.id.clock, struct.id.clock + struct.length]);
          deleteRanges.set(struct.id.client, ranges);
        }
      }
    }
  }
  const decoded = Y.decodeUpdate(update);
  for (const [client, ranges] of decoded.ds.clients) {
    for (const range of ranges) {
      const allowed = [...(deleteRanges.get(client) ?? []), ...(priorDeletes.get(client) ?? []).map((item) => [item.clock, item.clock + item.len])];
      if (!rangeCovered(allowed, range.clock, range.clock + range.len)) throw new CollaborationError('stale_text_field_update');
    }
  }
  for (const [client, structs] of candidateDoc.store.clients) {
    const previous = baselineDoc.store.clients.get(client) ?? [];
    for (const struct of structs) {
      if (hasStructRange(previous, struct.id.clock, struct.id.clock + struct.length)) continue;
      if (!(struct.parent instanceof Y.Text) || !permittedTypes.has(typeIdentity(struct.parent))) throw new CollaborationError('stale_text_field_update');
    }
  }
}

function typeIdentity(type) {
  const id = type?._item?.id;
  return id && Number.isSafeInteger(id.client) && Number.isSafeInteger(id.clock) ? `${id.client}:${id.clock}` : null;
}

function hasStructRange(structs, start, end) {
  return structs.some((struct) => struct.id.clock <= start && struct.id.clock + struct.length >= end);
}

function rangeCovered(ranges, start, end) {
  const sorted = ranges.map((range) => [...range]).sort((left, right) => left[0] - right[0]);
  let cursor = start;
  for (const [left, right] of sorted) {
    if (right <= cursor) continue;
    if (left > cursor) return false;
    cursor = Math.max(cursor, right);
    if (cursor >= end) return true;
  }
  return cursor >= end;
}
