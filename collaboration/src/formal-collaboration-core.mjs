import { randomUUID } from 'node:crypto';
import * as Y from 'yjs';
import { assertStageTextUpdateScope } from './collaboration-core.mjs';
import { applyFormalCommand } from './formal-commands.mjs';
import { canonicalJson, encodeFormalDocument, formalTextFields, FORMAL_UUID, projectFormalDocument, sha256 } from './formal-document.mjs';
import { FormalError } from './formal-http-store.mjs';
import { LeaseManager } from './leases.mjs';
import { applyObjectUndo, structureFingerprint, undoReceipt } from './formal-object-undo.mjs';

const exact = (value, keys) => value && typeof value === 'object' && !Array.isArray(value) && Object.keys(value).sort().join(',') === [...keys].sort().join(',');
const decode = (value, max = 4 * 1024 * 1024) => {
  if (typeof value !== 'string') throw new FormalError('VALIDATION_ERROR');
  const bytes = Buffer.from(value, 'base64');
  if (!bytes.length || bytes.length > max || bytes.toString('base64') !== value) throw new FormalError('VALIDATION_ERROR');
  return bytes;
};

function loadedDocument(saved) {
  const bytes = decode(saved.state_base64);
  if (sha256(bytes) !== saved.state_sha256) throw new FormalError('COLLAB_UNAVAILABLE');
  const doc = new Y.Doc();
  try {
    Y.applyUpdate(doc, bytes, 'server');
    const projection = projectFormalDocument(doc);
    if (canonicalJson(projection) !== canonicalJson(saved.projection) || sha256(canonicalJson(projection)) !== saved.projection_sha256 || projection.structure.document_id !== saved.document_id) throw new FormalError('COLLAB_UNAVAILABLE');
    return doc;
  } catch (error) { doc.destroy(); throw error; }
}

function targetMap(structure) {
  return new Map(structure.stages.flatMap((stage) => [
    ...Object.entries(stage.initial_positions).map(([id, point]) => [`position:${stage.id}:${id}`, point.version]),
    ...stage.actions.map((action) => [`action:${action.id}`, action.version]),
  ]));
}

export class FormalCollaborationCore {
  constructor({ store, now = Date.now }) {
    if (!store) throw new Error('formal_store_required');
    this.store = store; this.now = now; this.rooms = new Map(); this.loads = new Map();
  }

  async room(documentId, context) {
    if (this.rooms.has(documentId)) return this.rooms.get(documentId);
    if (!this.loads.has(documentId)) {
      const promise = (async () => {
        const saved = await this.store.load(documentId, context);
        const room = { id: documentId, epoch: saved.epoch, revision: saved.revision, doc: loadedDocument(saved), recoveries: saved.recoveries ?? [], clients: new Map(), queue: Promise.resolve(), leases: new LeaseManager({ now: this.now }), structureGeneration: 0, fieldGenerations: new Map() };
        this.rooms.set(documentId, room); return room;
      })();
      this.loads.set(documentId, promise);
      promise.finally(() => this.loads.delete(documentId)).catch(() => {});
    }
    return this.loads.get(documentId);
  }

  async connect({ ticket, connectionId = randomUUID(), send, close }) {
    const context = await this.store.consume(ticket, connectionId);
    try {
      const room = await this.room(context.tactic_id, context);
      const readOnly = context.role !== 'editor' || [...room.clients.values()].filter((item) => !item.readOnly).length >= 5;
      const connection = { id: connectionId, context, actor: context.actor, room, send, close, readOnly, closed: false, presence: null };
      room.clients.set(connectionId, connection);
      send({ type: 'auth.ok', actor_id: context.actor.id, connection_id: connectionId, role: readOnly ? 'viewer' : 'editor', read_only: readOnly });
      send(this.sync(room)); this.sendUndoState(connection); this.broadcast(room, { type: 'participants', participants: this.participants(room) });
      return connection;
    } catch (error) { await this.store.release(context).catch(() => {}); throw error; }
  }

  disconnect(connection) {
    if (!connection || connection.closed) return;
    connection.undo = null; this.sendUndoState(connection);
    connection.closed = true; connection.room.clients.delete(connection.id); connection.room.leases.releaseConnection(connection.id);
    void this.store.release(connection.context).catch(() => {});
    this.broadcast(connection.room, { type: 'participants', participants: this.participants(connection.room) });
    this.broadcast(connection.room, { type: 'locks', locks: this.locks(connection.room) });
    const room = connection.room;
    void room.queue.finally(() => {
      const timer = setTimeout(() => {
        if (!room.clients.size && this.rooms.get(room.id) === room) { this.rooms.delete(room.id); room.doc.destroy(); }
      }, 1_000);
      timer.unref?.();
    });
  }

  participants(room) { return [...room.clients.values()].map((item) => ({ id: item.actor.id, connection_id: item.id, name: item.actor.name, color: item.actor.color, read_only: item.readOnly, ...(item.presence ?? { stage_id: null, field: null, relative_cursor: null, selected_object_id: null, pointer: null }) })); }
  locks(room) { return room.leases.snapshot().map((item) => ({ object_id: item.objectId, owner: item.owner, color: item.color, expires_at: item.expiresAt })); }
  broadcast(room, message) { for (const connection of room.clients.values()) if (!connection.closed) connection.send(message); }
  sync(room) { const packet = encodeFormalDocument(room.doc); return { type: 'sync', document_id: room.id, epoch: room.epoch, revision: room.revision, state: packet.state_base64, projection: packet.projection, recoveries: room.recoveries, participants: this.participants(room), locks: this.locks(room) }; }

  sendUndoState(connection) {
    const item = connection.undo;
    connection.send({ type: 'object.undo.state', undo: item ? { source_operation_id: item.sourceId, recovery_id: item.recoveryId, is_delete: item.isDelete } : null });
  }

  clearUndo(room) {
    for (const client of room.clients.values()) { client.undo = null; this.sendUndoState(client); }
  }

  async reload(room, context, preserveUndo = false) {
    const saved = await this.store.load(room.id, context);
    const next = loadedDocument(saved); room.doc.destroy(); room.doc = next;
    room.epoch = saved.epoch; room.revision = saved.revision; room.recoveries = saved.recoveries ?? [];
    if (!preserveUndo) this.clearUndo(room);
  }

  operation(connection, message) {
    const run = connection.room.queue.then(async () => {
      try { return await this.apply(connection, message); }
      catch (error) {
        if (['TACTIC_CONFLICT', 'COLLAB_STALE_EPOCH'].includes(error.code)) {
          await this.reload(connection.room, connection.context);
          this.broadcast(connection.room, this.sync(connection.room));
          error.authoritativeSyncSent = true;
        }
        throw error;
      }
    });
    connection.room.queue = run.catch(() => {}); return run;
  }

  async apply(connection, operation) {
    const room = connection.room;
    if (connection.closed || connection.readOnly) throw new FormalError('USER_FORBIDDEN');
    if (!exact(operation, ['type', 'operation_id', 'epoch', 'payload']) || !FORMAL_UUID.test(operation.operation_id) || !['text.update', 'stage.command'].includes(operation.type)) throw new FormalError('VALIDATION_ERROR');
    if (operation.epoch !== room.epoch) throw new FormalError('COLLAB_STALE_EPOCH');
    const payloadJson = canonicalJson(operation.payload);
    const fingerprint = sha256(`${operation.type}\n${operation.epoch}\n${payloadJson}`);
    const prior = await this.store.findOperation(room.id, room.epoch, operation.operation_id, connection.context);
    if (prior) {
      if (prior.actor_id !== connection.actor.id || prior.fingerprint !== fingerprint || prior.type !== operation.type) throw new FormalError('OPERATION_ID_REUSED');
      await this.reload(room, connection.context);
      this.broadcast(room, this.sync(room));
      connection.send({ ...this.sync(room), type: 'operation.ack', operation_id: operation.operation_id, revision: prior.revision, current_revision: room.revision, duplicate: true });
      return;
    }
    const candidate = new Y.Doc(); Y.applyUpdate(candidate, Y.encodeStateAsUpdate(room.doc), 'candidate');
    const temporaryLeases = []; const changedFields = new Set(); let command; let next;
    try {
      const previous = projectFormalDocument(room.doc).structure;
      if (operation.type === 'text.update') {
        if (!exact(operation.payload, ['update'])) throw new FormalError('VALIDATION_ERROR');
        const update = decode(operation.payload.update, 1024 * 1024);
        const observe = (events) => { for (const event of events) if (typeof event.path[0] === 'string') changedFields.add(event.path[0]); };
        candidate.getMap('fields').observeDeep(observe);
        try { Y.applyUpdate(candidate, update, connection.id); }
        finally { candidate.getMap('fields').unobserveDeep(observe); }
        assertStageTextUpdateScope(candidate, room.doc, update, null, formalTextFields(previous));
      } else {
        if (!exact(operation.payload, ['expected_revision', 'command']) || operation.payload.expected_revision !== room.revision) throw new FormalError('TACTIC_CONFLICT', { current_revision: room.revision });
        command = structuredClone(operation.payload.command);
        if (!command || typeof command.type !== 'string') throw new FormalError('VALIDATION_ERROR');
        if (command.type === 'object.undo') {
          const receipt = connection.undo;
          if (!exact(command, ['type', 'source_operation_id', 'recovery_id']) || !FORMAL_UUID.test(command.source_operation_id) || (command.recovery_id !== null && !FORMAL_UUID.test(command.recovery_id))) throw new FormalError('VALIDATION_ERROR');
          if (!receipt || receipt.sourceId !== command.source_operation_id || receipt.recoveryId !== command.recovery_id || receipt.generation !== room.structureGeneration || receipt.after !== structureFingerprint(previous) || receipt.removedFields.some(([key, generation]) => (room.fieldGenerations.get(key) ?? 0) !== generation)) throw new FormalError('TACTIC_CONFLICT');
          const recovery = receipt.isDelete ? await this.store.readRecovery(room.id, receipt.recoveryId, connection.context) : null;
          next = applyObjectUndo(room.doc, candidate, receipt, recovery);
          const targets = targetMap(previous); const future = targetMap(next);
          for (const id of [...targets.keys()].filter((key) => targets.get(key) !== future.get(key)).sort()) {
            room.leases.sweep(); const existed = room.leases.leases.has(id);
            const acquired = room.leases.acquire({ objectId: id, version: targets.get(id), currentVersion: targets.get(id), connectionId: connection.id, actor: connection.actor });
            if (!existed) temporaryLeases.push({ objectId: id, token: acquired.token, connectionId: connection.id });
          }
          this.broadcast(room, { type: 'locks', locks: this.locks(room) });
        } else {
          let leaseId = null;
          if (command.type === 'position.set') leaseId = `position:${command.stage_id}:${command.role_id}`;
          else if (command.type.startsWith('action.') && command.type !== 'action.add') leaseId = `action:${command.action_id}`;
          const lease = command.lease; delete command.lease;
          if (leaseId) {
            if (!exact(lease, ['token', 'version'])) throw new FormalError('COLLAB_LOCKED');
            room.leases.assertHeld({ objectId: leaseId, token: lease.token, version: lease.version, connectionId: connection.id, currentVersion: targetMap(previous).get(leaseId) });
          } else if (lease !== undefined) throw new FormalError('VALIDATION_ERROR');
          const recovery = command.type === 'recovery.restore' ? await this.store.readRecovery(room.id, command.recovery_id, connection.context) : null;
          next = applyFormalCommand(room.doc, candidate, command, recovery);
        }
        const oldTargets = targetMap(previous); const nextTargets = targetMap(next);
        room.leases.sweep();
        for (const [id, held] of room.leases.leases) {
          if (held.connectionId !== connection.id && oldTargets.get(id) !== nextTargets.get(id)) throw new FormalError('COLLAB_LOCKED', { owner: held.actor.name, color: held.actor.color });
        }
      }
      const packet = encodeFormalDocument(candidate);
      const result = await this.store.persist(room.id, connection.context, { epoch: room.epoch, expected_revision: room.revision, operation_id: operation.operation_id, type: operation.type, original_payload: operation.payload, payload_json: payloadJson, fingerprint, ...packet });
      if (result.duplicate || operation.type === 'stage.command') await this.reload(room, connection.context, !result.duplicate);
      else { const next = new Y.Doc(); Y.applyUpdate(next, Y.encodeStateAsUpdate(candidate), 'confirmed'); room.doc.destroy(); room.doc = next; room.revision = result.revision; }
      if (result.duplicate) this.broadcast(room, this.sync(room));
      else this.broadcast(room, { ...this.sync(room), type: 'operation.applied', operation_id: operation.operation_id, actor_id: connection.actor.id, connection_id: connection.id, operation_type: operation.type });
      if (!result.duplicate && operation.type === 'stage.command') {
        room.structureGeneration += 1; this.clearUndo(room);
        const keys = new Set(formalTextFields(next));
        for (const key of room.fieldGenerations.keys()) if (!keys.has(key)) room.fieldGenerations.delete(key);
        if (command.type !== 'object.undo' && !connection.closed && room.revision === result.revision && room.epoch === operation.epoch) connection.undo = undoReceipt(previous, next, command, operation.operation_id, room);
        this.sendUndoState(connection);
      } else if (!result.duplicate) {
        for (const key of changedFields) room.fieldGenerations.set(key, (room.fieldGenerations.get(key) ?? 0) + 1);
      }
      if (result.duplicate) connection.send({ ...this.sync(room), type: 'operation.ack', operation_id: operation.operation_id, revision: result.revision, current_revision: room.revision, duplicate: true });
    } finally {
      for (const lease of temporaryLeases) room.leases.release(lease);
      if (temporaryLeases.length) this.broadcast(room, { type: 'locks', locks: this.locks(room) });
      candidate.destroy();
    }
  }

  presence(connection, payload) {
    if (connection.closed || !payload || typeof payload !== 'object' || Object.keys(payload).some((key) => !['stage_id', 'field', 'relative_cursor', 'selected_object_id', 'pointer'].includes(key))) throw new FormalError('VALIDATION_ERROR');
    const { structure } = projectFormalDocument(connection.room.doc);
    const stage = structure.stages.find((item) => item.id === payload.stage_id);
    if (!stage || (payload.field != null && !formalTextFields(structure).includes(payload.field))) throw new FormalError('VALIDATION_ERROR');
    if (payload.field?.startsWith('stage.') && !payload.field.startsWith(`stage.${stage.id}.`)) throw new FormalError('VALIDATION_ERROR');
    if (payload.field?.startsWith('action.') && !stage.actions.some((item) => payload.field === `action.${item.id}.description`)) throw new FormalError('VALIDATION_ERROR');
    let relative = null;
    if (payload.relative_cursor != null) {
      if (!payload.field || typeof payload.relative_cursor !== 'string' || payload.relative_cursor.length > 256) throw new FormalError('VALIDATION_ERROR');
      relative = Y.createAbsolutePositionFromRelativePosition(Y.decodeRelativePosition(decode(payload.relative_cursor, 192)), connection.room.doc);
      if (relative?.type !== connection.room.doc.getMap('fields').get(payload.field)) throw new FormalError('VALIDATION_ERROR');
    }
    const pointer = payload.pointer;
    if (pointer != null && (!exact(pointer, ['x', 'y']) || ![pointer.x, pointer.y].every((value) => typeof value === 'number' && Number.isFinite(value) && value >= 0 && value <= 100))) throw new FormalError('VALIDATION_ERROR');
    const objectId = payload.selected_object_id;
    if (objectId != null && !stage.actions.some((item) => item.id === objectId) && !structure.roles.some((item) => item.id === objectId)) throw new FormalError('VALIDATION_ERROR');
    if (this.now() - (connection.lastPresenceAt ?? -Infinity) < 100) return;
    connection.lastPresenceAt = this.now();
    connection.presence = { stage_id: stage.id, field: payload.field ?? null, relative_cursor: relative ? payload.relative_cursor : null, selected_object_id: objectId ?? null, pointer: pointer ?? null };
    this.broadcast(connection.room, { type: 'participants', participants: this.participants(connection.room) });
  }

  lease(connection, kind, payload) {
    if (connection.closed || connection.readOnly) throw new FormalError('USER_FORBIDDEN');
    const manager = connection.room.leases;
    const targets = targetMap(projectFormalDocument(connection.room.doc).structure);
    if (!payload || (kind !== 'release' && !targets.has(payload.object_id))) throw new FormalError('VALIDATION_ERROR');
    let result;
    if (kind === 'acquire') {
      if (!exact(payload, ['object_id', 'version'])) throw new FormalError('VALIDATION_ERROR');
      result = manager.acquire({ objectId: payload.object_id, version: payload.version, connectionId: connection.id, actor: connection.actor, currentVersion: targets.get(payload.object_id) });
    } else if (kind === 'renew') {
      if (!exact(payload, ['object_id', 'token'])) throw new FormalError('VALIDATION_ERROR');
      result = manager.renew({ objectId: payload.object_id, token: payload.token, connectionId: connection.id });
    } else {
      const parts = typeof payload.object_id === 'string' ? payload.object_id.split(':') : [];
      const validId = (parts[0] === 'action' && parts.length === 2 && FORMAL_UUID.test(parts[1])) ||
        (parts[0] === 'position' && parts.length === 3 && FORMAL_UUID.test(parts[1]) && FORMAL_UUID.test(parts[2]));
      if (kind !== 'release' || !exact(payload, ['object_id', 'token']) || !validId || !FORMAL_UUID.test(payload.token)) throw new FormalError('VALIDATION_ERROR');
      result = { objectId: payload.object_id, released: manager.release({ objectId: payload.object_id, token: payload.token, connectionId: connection.id }) };
    }
    this.broadcast(connection.room, { type: 'locks', locks: this.locks(connection.room) }); return result;
  }

  preview(connection, payload) {
    if (connection.closed || connection.readOnly || !exact(payload, ['object_id', 'token', 'x', 'y'])) throw new FormalError('USER_FORBIDDEN');
    if (!targetMap(projectFormalDocument(connection.room.doc).structure).has(payload.object_id)) throw new FormalError('VALIDATION_ERROR');
    if (connection.room.leases.preview({ objectId: payload.object_id, token: payload.token, connectionId: connection.id, x: payload.x, y: payload.y })) this.broadcast(connection.room, { type: 'map.preview', actor_id: connection.actor.id, object_id: payload.object_id, x: payload.x, y: payload.y });
  }

  async checkConnections() {
    const connections = [...this.rooms.values()].flatMap((room) => [...room.clients.values()]);
    if (!connections.length) return;
    try {
      const grants = [];
      for (let index = 0; index < connections.length; index += 100) grants.push(...await this.store.checkGrants(connections.slice(index, index + 100).map((item) => item.context.grant_id)));
      const valid = new Map(grants.filter((item) => item.active).map((item) => [item.id, item.role]));
      for (const connection of connections) if (!valid.has(connection.context.grant_id) || valid.get(connection.context.grant_id) !== connection.context.role) { this.disconnect(connection); connection.close(1008, 'authorization_expired'); }
    } catch {
      for (const connection of connections) { this.disconnect(connection); connection.close(1013, 'authorization_unavailable'); }
    }
  }

  sweepLocks() {
    for (const room of this.rooms.values()) {
      const locks = this.locks(room); const signature = canonicalJson(locks);
      if (signature !== room.lockSignature) { room.lockSignature = signature; this.broadcast(room, { type: 'locks', locks }); }
    }
  }
}
