import * as Y from 'yjs';
import { accountRequest } from './api';
import { ApiError } from '../api/client';
import type { Point, TacticProjection } from './editorTypes';

export interface Participant { id: string; connection_id: string; name: string; color: string; read_only: boolean; stage_id?: string; field?: string; relative_cursor?: string; selected_object_id?: string; pointer?: Point }
interface MapPresence { selectedObjectId?: string; pointer?: Point }
export interface ObjectUndo { source_operation_id: string; recovery_id: string | null; is_delete: boolean }
const uuid4 = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;
function undoSummary(value: unknown): ObjectUndo | null {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return null;
  const summary = value as Record<string, unknown>;
  if (Object.keys(summary).sort().join(',') !== 'is_delete,recovery_id,source_operation_id' || typeof summary.source_operation_id !== 'string' || !uuid4.test(summary.source_operation_id) || !(summary.recovery_id === null || typeof summary.recovery_id === 'string' && uuid4.test(summary.recovery_id)) || typeof summary.is_delete !== 'boolean') return null;
  return { source_operation_id: summary.source_operation_id, recovery_id: summary.recovery_id, is_delete: summary.is_delete };
}
export interface Recovery { id: string; kind: 'action' | 'stage'; entity_id: string; expires_at: string }
export interface Lock { object_id: string; owner: string; color: string; expires_at: number }
interface Operation { type: 'text.update' | 'stage.command'; operation_id: string; epoch: string; payload: Record<string, unknown> }
interface Lease { object_id: string; token: string; version: number; expires_at: number; renew_every_ms: number }
interface Authority { document_id: string; epoch: string; revision: number; current_revision?: number; state: string; projection: TacticProjection; recoveries?: Recovery[]; participants?: Participant[]; locks?: Lock[] }
export interface ClientState { projection: TacticProjection | null; confirmed: TacticProjection | null; epoch: string; revision: number; connected: boolean; readOnly: boolean; connectionId: string; pending: number; recovery: boolean; error: string; auxiliaryError: string; participants: Participant[]; recoveries: Recovery[]; locks: Lock[]; previews: Record<string, { x: number; y: number }>; objectUndo: ObjectUndo | null }
export const encodeBytes = (value: Uint8Array) => { let data = ''; for (const byte of value) data += String.fromCharCode(byte); return btoa(data); };
export const decodeBytes = (value: string) => Uint8Array.from(atob(value), character => character.charCodeAt(0));
export function projectDocument(doc: Y.Doc): TacticProjection {
  return { structure: structuredClone(doc.getMap('document').get('structure') as TacticProjection['structure']), fields: Object.fromEntries([...doc.getMap<Y.Text>('fields').entries()].map(([key, text]) => [key, text.toString()])) };
}
function authorityCheck(condition: unknown): asserts condition { if (!condition) throw Error('invalid_authority'); }
const nonempty = (value: unknown): value is string => typeof value === 'string' && value.length > 0;
const revisionNumber = (value: unknown): value is number => Number.isSafeInteger(value) && (value as number) >= 0;
function orderedJson(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(orderedJson);
  if (value !== null && typeof value === 'object') {
    authorityCheck(Object.getPrototypeOf(value) === Object.prototype || Object.getPrototypeOf(value) === null);
    return Object.fromEntries(Object.keys(value).sort().map(key => [key, orderedJson((value as Record<string, unknown>)[key])]));
  }
  authorityCheck(value === null || typeof value === 'string' || typeof value === 'boolean' || typeof value === 'number' && Number.isFinite(value));
  return value;
}
function checkedState(doc: Y.Doc, tacticId: string): TacticProjection {
  authorityCheck(!doc.store.pendingStructs && !doc.store.pendingDs && [...doc.share.keys()].sort().join(',') === 'document,fields');
  // Wire roots initially use AbstractType. A map root has no sequence items; reject a text/array root
  // before getMap can promote that raw root into the requested class.
  for (const root of doc.share.values()) authorityCheck(root._start === null);
  const root = doc.getMap('document');
  authorityCheck([...root.keys()].join(',') === 'structure');
  const structure = root.get('structure') as TacticProjection['structure'];
  authorityCheck(structure && !(structure instanceof Y.AbstractType) && structure.document_id === tacticId && structure.schema_kind === 'stage-board' && structure.schema_version === 2 && structure.map === 'mirage' && (structure.side === 'T' || structure.side === 'CT') && Array.isArray(structure.roles) && Array.isArray(structure.stages));
  for (const field of doc.getMap('fields').values()) authorityCheck(field instanceof Y.Text && field.toDelta().every((part: { insert?: unknown; attributes?: unknown }) => typeof part.insert === 'string' && !part.attributes));
  const projection = projectDocument(doc);
  orderedJson(projection); // JSON types only; full geometry/quota validation remains server-owned.
  return projection;
}
const friendly = (code: string) => ({ COLLAB_LOCKED: '该对象正在被其他窗口编辑，请稍后再试。', LEASE_EXPIRED: '编辑权已过期，请重新选择后再操作。', TACTIC_CONFLICT: '内容已更新，请核对最新版本后重新操作。', TACTIC_LINEUP_UNAVAILABLE: '新增教学已不可用，关联选择未保存；可导出保留选择，放弃未确认操作后重新选择。', COLLAB_STALE_EPOCH: '战术身份已变化，旧草稿只能导出，不能直接重试。', COLLAB_UNAVAILABLE: '协作服务暂时无法加载战术，请稍后重新连接。', USER_FORBIDDEN: '当前连接没有访问此战术的权限。', TACTIC_NOT_FOUND: '找不到此战术，可能已归档或已失去访问权限。', USER_AUTH_REQUIRED: '登录已失效，请重新登录。', VALIDATION_ERROR: '内容不符合要求，请检查长度、动作编组和路径。' })[code] ?? `操作未完成（${code}）。`;

export class TacticCollaborationClient {
  doc = new Y.Doc();
  confirmedDoc = new Y.Doc();
  undo: Y.UndoManager | null = null;
  state: ClientState = { projection: null, confirmed: null, epoch: '', revision: 0, connected: false, readOnly: true, connectionId: '', pending: 0, recovery: false, error: '', auxiliaryError: '', participants: [], recoveries: [], locks: [], previews: {}, objectUndo: null };
  private socket: WebSocket | null = null;
  private generation = 0;
  private disposed = false;
  private authenticated = false;
  private authorityReady = false;
  private actorId: string | null = null;
  private listeners = new Set<() => void>();
  private operations = new Map<string, Operation>();
  private inFlight: string | null = null;
  private operationTimer?: ReturnType<typeof setTimeout>;
  private lastOperationAt = -Infinity;
  private completions = new Map<string, { resolve: () => void; reject: (error: Error) => void }>();
  private leases = new Map<string, Lease>();
  private leaseRequests = new Map<string, { resolve: (value: Lease) => void; reject: (error: Error) => void; timer: ReturnType<typeof setTimeout>; version: number }>();
  private renewals = new Map<string, ReturnType<typeof setInterval>>();
  private renewalRequests = new Map<string, string>();
  private throttle = new Map<string, { at: number; timer?: ReturnType<typeof setTimeout>; next?: object }>();
  private draft: object | null = null;
  private recoveryConfirmed = false;
  private recoveryHadPending = false;
  private localDraft: object | null = null;
  private drafts = new Map<string, object>();
  private timestamps = new Map<string, number>();
  private watchdog: ReturnType<typeof setInterval>;
  private lastLeaseError: string | null = null;
  private releaseRequests = new Map<string, string>();
  private releaseFailureObject: string | null = null;
  private lastPresence: { stageId: string; field?: string; cursor?: number; map?: MapPresence } | null = null;
  clearLeaseError() {
    if (this.lastLeaseError && this.state.error === this.lastLeaseError) this.emit({ error: '' });
    this.lastLeaseError = null;
  }
  constructor(readonly tacticId: string, private csrf: string) {
    this.bindDoc();
    this.watchdog = setInterval(() => {
      if (this.state.recovery || ![...this.timestamps.values()].some(time => Date.now() - time > 10000)) return;
      this.preserve(); this.releaseAll();
      for (const completion of this.completions.values()) completion.reject(Error('未收到保存确认，草稿已保留。'));
      this.completions.clear(); this.emit({ error: '尚未收到保存确认，请核对连接后按原操作重试或导出。' });
    }, 1000);
  }
  subscribe = (listener: () => void) => { this.listeners.add(listener); return () => { this.listeners.delete(listener); }; };
  snapshot = () => this.state;
  private emit(patch: Partial<ClientState> = {}) {
    this.state = { ...this.state, ...patch, pending: this.operations.size };
    for (const listener of this.listeners) listener();
  }
  private bindDoc() {
    this.doc.on('update', (update: Uint8Array, origin: unknown) => {
      if (origin !== 'local-ui' && origin !== this.undo) return;
      this.emit({ projection: projectDocument(this.doc) });
      const operation: Operation = { type: 'text.update', operation_id: crypto.randomUUID(), epoch: this.state.epoch, payload: { update: encodeBytes(update) } };
      if (this.draft) this.recoveryConfirmed = false;
      this.operations.set(operation.operation_id, operation);
      if (this.canWrite()) this.pumpOperations();
      else this.preserve();
      this.emit();
    });
  }
  private send(value: object) {
    const message = value as { type?: string; payload?: Record<string, unknown> };
    if (message.type === 'presence' && message.payload) {
      const stage = this.state.confirmed?.structure.stages.find(item => item.id === message.payload?.stage_id);
      if (!stage) return;
      const payload = { ...message.payload }; const field = payload.field;
      if (typeof field === 'string' && (!Object.hasOwn(this.state.confirmed!.fields, field) || (field.startsWith('stage.') && !field.startsWith(`stage.${stage.id}.`)) || (field.startsWith('action.') && !stage.actions.some(item => field === `action.${item.id}.description`)))) { delete payload.field; delete payload.relative_cursor; }
      if (this.operations.size) delete payload.relative_cursor;
      if (typeof payload.selected_object_id === 'string' && !stage.actions.some(item => item.id === payload.selected_object_id) && !this.state.confirmed!.structure.roles.some(item => item.id === payload.selected_object_id)) delete payload.selected_object_id;
      const pointer = payload.pointer as Point | undefined;
      if (pointer && ![pointer.x, pointer.y].every(value => Number.isFinite(value) && value >= 0 && value <= 100)) delete payload.pointer;
      value = { type: 'presence', payload };
    }
    if (message.type === 'map.preview' && message.payload) {
      const lease = this.leases.get(String(message.payload.object_id));
      if (!lease || lease.token !== message.payload.token || lease.expires_at <= Date.now()) return;
    }
    if (this.socket?.readyState === WebSocket.OPEN) this.socket.send(JSON.stringify(value));
  }
  private pumpOperations() {
    if (this.disposed || !this.canWrite() || this.inFlight || this.operationTimer || !this.operations.size) return;
    const wait = 100 - (Date.now() - this.lastOperationAt);
    if (wait > 0) {
      this.operationTimer = setTimeout(() => { this.operationTimer = undefined; this.pumpOperations(); }, wait);
      return;
    }
    const operation = this.operations.values().next().value!;
    if (operation.epoch !== this.state.epoch || this.socket?.readyState !== WebSocket.OPEN) return;
    this.inFlight = operation.operation_id; this.lastOperationAt = Date.now();
    this.timestamps.set(operation.operation_id, Date.now());
    this.send({ type: 'operation', operation });
  }
  private clearOperationSend() {
    clearTimeout(this.operationTimer); this.operationTimer = undefined;
    this.inFlight = null; this.lastOperationAt = -Infinity;
  }
  canWrite() { return this.authenticated && this.authorityReady && this.state.connected && !this.state.readOnly && !this.state.recovery; }
  setLocalDraft(value: object | null, key = 'form') {
    const hadDraft = Boolean(this.localDraft);
    if (value && this.draft) this.recoveryConfirmed = false;
    if (value) this.drafts.set(key, value); else this.drafts.delete(key);
    this.localDraft = this.drafts.size ? Object.fromEntries(this.drafts) : null;
    const recovered = this.finishRecovery();
    if (hadDraft !== Boolean(this.localDraft) || recovered) this.emit(recovered ? { recovery: false, error: '' } : {});
  }
  private finishRecovery() {
    if (!this.draft || !this.recoveryConfirmed || this.operations.size || this.localDraft) return false;
    this.draft = null; this.recoveryConfirmed = false; this.recoveryHadPending = false;
    return true;
  }
  hasUnconfirmedWork() { return Boolean(this.operations.size || this.state.recovery || this.localDraft || this.draft); }
  private preserve() {
    if (!this.draft && (this.operations.size || this.localDraft)) { this.recoveryConfirmed = false; this.recoveryHadPending = this.operations.size > 0; this.draft = { epoch: this.state.epoch, confirmed_revision: this.state.revision, projection: this.state.projection, local: this.localDraft, operations: [...this.operations.values()] }; }
    this.emit({ recovery: Boolean(this.draft), error: this.state.error });
  }
  recoveryExport() { return { kind: 'smokeline-formal-recovery', document_id: this.tacticId, exported_at: new Date().toISOString(), confirmed_revision: this.state.revision, recovery_copy: this.draft ?? { projection: this.state.projection, local: this.localDraft }, pending_operations: [...this.operations.values()], current_local_draft: this.localDraft }; }
  async connect() {
    this.preserve();
    this.authenticated = false; this.authorityReady = false; this.actorId = null;
    this.clearTransientMessages();
    for (const completion of this.completions.values()) completion.reject(Error('正在重新连接，未确认操作已保留。'));
    this.completions.clear();
    const generation = ++this.generation;
    this.socket?.close(); this.releaseAll();
    this.emit({ connected: false, objectUndo: null, error: '' });
    try {
      const issued = await accountRequest<{ ticket: string }>(`tactics/${this.tacticId}/collaboration-ticket`, 'POST', {}, this.csrf);
      if (this.disposed || generation !== this.generation) return;
      const url = new URL('/ws/tactics', location.href); url.protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
      const socket = new WebSocket(url); this.socket = socket;
      socket.addEventListener('open', () => { if (generation === this.generation) socket.send(JSON.stringify({ type: 'auth', ticket: issued.ticket })); });
      socket.addEventListener('message', event => {
        if (generation !== this.generation || this.disposed || socket.readyState !== WebSocket.OPEN) return;
        try { this.message(JSON.parse(String(event.data))); } catch {
          // Preserve the original pending/draft before touching either live document.
          this.preserve(); this.authenticated = false; this.authorityReady = false; this.actorId = null;
          this.emit({ error: '协作确认无效，已暂停编辑。未确认内容仍保留，可导出后重新连接。', connected: false, objectUndo: null }); socket.close();
        }
      });
      socket.addEventListener('close', event => {
        if (generation !== this.generation || this.disposed) return;
        this.authenticated = false; this.authorityReady = false; this.actorId = null;
        this.preserve(); this.releaseAll(); this.clearTransientMessages();
        for (const pending of this.completions.values()) pending.reject(new Error('连接断开，尚未确认的内容已保留。'));
        this.completions.clear();
        const reason = ({ authorization_invalid: '无法验证此战术的访问权限，请确认账户后重新连接。', authorization_expired: '访问权限或登录状态已变化，请确认后重新连接。', authorization_unavailable: '暂时无法验证访问权限，请稍后重新连接。', auth_timeout: '连接验证超时，请重新连接。', message_limit: '输入请求过于密集，连接已暂停。未确认内容已保留，请重新连接后按原操作重试。' } as Record<string, string>)[event.reason];
        this.emit({ connected: false, objectUndo: null, error: this.state.error || reason || '连接已断开，请重连。未确认内容仍保留在本页。' });
      });
      socket.addEventListener('error', () => { if (generation === this.generation) this.emit({ error: '协作服务暂时无法连接。' }); });
    } catch (failure) {
      if (!this.disposed && generation === this.generation) {
        const code = failure instanceof ApiError ? ({ 401: 'USER_AUTH_REQUIRED', 403: 'USER_FORBIDDEN', 404: 'TACTIC_NOT_FOUND' } as Record<number, string>)[failure.status] : undefined;
        this.emit({ connected: false, error: code ? friendly(code) : '暂时无法取得协作连接，请稍后重试。' });
      }
    }
  }
  private applyAuthority(message: Authority, clearUndo: boolean, revision: number) {
    authorityCheck(typeof message.state === 'string' && message.state.length <= 6 * 1024 * 1024);
    const bytes = decodeBytes(message.state);
    authorityCheck(bytes.length > 0 && bytes.length <= 4 * 1024 * 1024);
    let candidate: Y.Doc | null = new Y.Doc();
    let replacement: Y.Doc | null = null;
    try {
      Y.applyUpdate(candidate, bytes);
      const confirmed = checkedState(candidate, this.tacticId);
      authorityCheck(JSON.stringify(orderedJson(confirmed)) === JSON.stringify(orderedJson(message.projection)));
      const changedEpoch = Boolean(this.state.epoch && this.state.epoch !== message.epoch);
      // Preflight the merge too, while retaining the live Y.Text identities for cursors/undo.
      replacement = new Y.Doc();
      if (!changedEpoch) Y.applyUpdate(replacement, Y.encodeStateAsUpdate(this.doc));
      Y.applyUpdate(replacement, bytes); checkedState(replacement, this.tacticId);
      if (changedEpoch) {
        this.preserve(); this.undo?.destroy(); this.undo = null;
        this.doc.destroy(); this.doc = replacement; replacement = null; this.bindDoc();
      } else Y.applyUpdate(this.doc, bytes, 'server');
      this.confirmedDoc.destroy(); this.confirmedDoc = candidate; candidate = null;
      const fields = [...this.doc.getMap<Y.Text>('fields').values()];
      if (!this.undo) this.undo = new Y.UndoManager(fields, { trackedOrigins: new Set(['local-ui']) });
      else this.undo.addToScope(fields);
      this.authorityReady = true;
      this.emit({ ...(clearUndo || changedEpoch ? { objectUndo: null } : {}), connected: true, projection: projectDocument(this.doc), confirmed, epoch: message.epoch, revision, recoveries: message.recoveries ?? this.state.recoveries, participants: message.participants ?? this.state.participants, locks: message.locks ?? this.state.locks, previews: {} });
    } finally { candidate?.destroy(); replacement?.destroy(); }
  }
  private authority(message: Record<string, unknown>): boolean {
    const type = message.type;
    authorityCheck(this.authenticated && (type === 'sync' || this.state.connected) && message.document_id === this.tacticId && nonempty(message.epoch) && revisionNumber(message.revision) && nonempty(message.state) && message.state.length <= 6 * 1024 * 1024 && message.projection !== null && typeof message.projection === 'object' && !Array.isArray(message.projection));
    let effective = message.revision; let acknowledge = false;
    if (type === 'sync') {
      authorityCheck(message.epoch !== this.state.epoch || effective >= this.state.revision);
    } else {
      authorityCheck(this.authorityReady && nonempty(message.operation_id));
      const pending = this.operations.get(message.operation_id);
      if (type === 'operation.ack') {
        authorityCheck(message.epoch === this.state.epoch && message.duplicate === true && revisionNumber(message.current_revision) && message.revision <= message.current_revision);
        // A late/unknown ACK is not a new authority channel. Its preceding sync is independent.
        if (!pending) return false;
        authorityCheck(message.epoch === pending.epoch && message.epoch === this.state.epoch);
        effective = message.current_revision; acknowledge = true;
      } else {
        authorityCheck(message.epoch === this.state.epoch && nonempty(message.actor_id) && nonempty(message.connection_id) && (message.operation_type === 'text.update' || message.operation_type === 'stage.command'));
        if (message.connection_id === this.state.connectionId) {
          authorityCheck(message.actor_id === this.actorId);
          if (pending) { authorityCheck(pending.epoch === message.epoch && pending.type === message.operation_type); acknowledge = true; }
        }
      }
      authorityCheck(effective >= this.state.revision);
    }
    this.applyAuthority(message as unknown as Authority, type === 'sync', effective);
    return acknowledge;
  }
  private message(message: Record<string, unknown>) {
    const type = message.type;
    if (type === 'auth.ok') {
      authorityCheck(nonempty(message.actor_id) && nonempty(message.connection_id) && typeof message.read_only === 'boolean' && message.role === (message.read_only ? 'viewer' : 'editor'));
      this.actorId = message.actor_id; this.authenticated = true;
      this.releaseFailureObject = null; this.emit({ connected: this.authorityReady, readOnly: message.read_only, connectionId: message.connection_id, error: '', auxiliaryError: '' });
    }
    else if (type === 'sync' || type === 'operation.applied' || type === 'operation.ack') {
      const acknowledge = this.authority(message);
      if (acknowledge) {
        const id = String(message.operation_id);
        const command = this.operations.get(id)?.payload.command as { type?: string } | undefined;
        this.operations.delete(id); this.timestamps.delete(id); this.completions.get(id)?.resolve(); this.completions.delete(id);
        if (this.draft && this.recoveryHadPending && !this.operations.size) this.recoveryConfirmed = true;
        if (this.inFlight === id) this.inFlight = null;
        this.emit({ ...(this.finishRecovery() ? { recovery: false, error: '' } : {}), ...(command?.type === 'object.undo' ? { objectUndo: null } : {}) });
        this.pumpOperations();
      }
      if ((type === 'operation.applied' || acknowledge) && this.lastPresence) this.presence(this.lastPresence.stageId, this.lastPresence.field, this.lastPresence.cursor, this.lastPresence.map);
    } else if (type === 'operation.rejected') {
      this.lastLeaseError = null;
      this.preserve();
      const id = String(message.operation_id);
      const command = this.operations.get(id)?.payload.command as { type?: string } | undefined;
      const error = new Error(command?.type === 'object.undo' && message.code === 'TACTIC_CONFLICT' ? '后续内容已变化，无法撤销。请核对当前内容；不会自动改用新版本重试。' : friendly(String(message.code)));
      this.completions.get(id)?.reject(error); this.completions.delete(id);
      this.emit({ error: error.message });
    } else if (type === 'object.undo.state') this.emit({ objectUndo: this.state.connected ? undoSummary(message.undo) : null });
    else if (type === 'participants') this.emit({ participants: message.participants as Participant[] });
    else if (type === 'locks') {
      const locks = message.locks as Lock[];
      this.emit({ locks, previews: Object.fromEntries(Object.entries(this.state.previews).filter(([id]) => locks.some(lock => lock.object_id === id))) });
    }
    else if (type === 'map.preview') this.emit({ previews: { ...this.state.previews, [String(message.object_id)]: { x: Number(message.x), y: Number(message.y) } } });
    else if (type === 'lease.renewed') {
      const id = String(message.request_id); const objectId = this.renewalRequests.get(id); this.renewalRequests.delete(id);
      const lease = objectId ? this.leases.get(objectId) : undefined;
      if (lease) lease.expires_at = Number(message.expires_at);
    }
    else if (type === 'lease.released') {
      const objectId = this.releaseRequests.get(String(message.request_id)); this.releaseRequests.delete(String(message.request_id));
      if (objectId && objectId === this.releaseFailureObject) { this.releaseFailureObject = null; this.emit({ auxiliaryError: '' }); }
    }
    else if (type === 'lease.granted') {
      const id = String(message.request_id); const request = this.leaseRequests.get(id); if (!request) return;
      clearTimeout(request.timer); this.leaseRequests.delete(id);
      const lease = { ...message, version: request.version } as unknown as Lease;
      this.clearLeaseError();
      if (this.releaseFailureObject === lease.object_id) { this.releaseFailureObject = null; this.emit({ auxiliaryError: '' }); }
      this.leases.set(lease.object_id, lease);
      this.renewals.set(lease.object_id, setInterval(() => {
        if (Date.now() >= lease.expires_at) { this.release(lease.object_id); this.preserve(); this.emit({ error: '编辑权已过期，请重新选择；未提交内容已保留。' }); return; }
        const requestId = crypto.randomUUID(); this.renewalRequests.set(requestId, lease.object_id);
        this.send({ type: 'lease.renew', request_id: requestId, payload: { object_id: lease.object_id, token: lease.token } });
      }, lease.renew_every_ms));
      request.resolve(lease);
    } else if (type === 'protocol.error') {
      if (!this.state.connected || !this.state.projection) { this.emit({ error: friendly(String(message.code)) }); return; }
      const releaseId = this.releaseRequests.get(String(message.request_id));
      if (releaseId) { this.releaseFailureObject = releaseId; this.releaseRequests.delete(String(message.request_id)); this.emit({ auxiliaryError: '内容保存状态未改变，但编辑权释放未确认；如仍显示占用，请重新连接。' }); return; }
      if (!message.request_id) { this.emit({ auxiliaryError: `在线位置更新未完成：${friendly(String(message.code))}` }); return; }
      const request = this.leaseRequests.get(String(message.request_id));
      if (request) { clearTimeout(request.timer); request.reject(new Error(friendly(String(message.code)))); this.leaseRequests.delete(String(message.request_id)); }
      const objectId = this.renewalRequests.get(String(message.request_id));
      if (objectId) { this.release(objectId); this.preserve(); }
      const error = friendly(String(message.code));
      this.lastLeaseError = request || objectId ? error : null;
      this.emit({ error });
    }
  }
  async acquire(objectId: string, version: number): Promise<Lease> {
    if (!this.canWrite()) throw Error('当前无法编辑。');
    const existing = this.leases.get(objectId); if (existing && existing.expires_at > Date.now()) return existing;
    if (existing) this.release(objectId);
    const id = crypto.randomUUID();
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => { this.leaseRequests.delete(id); reject(Error('未取得编辑权，请稍后再试。')); }, 5000);
      this.leaseRequests.set(id, { resolve, reject, timer, version });
      this.send({ type: 'lease.acquire', request_id: id, payload: { object_id: objectId, version } });
    });
  }
  release(objectId: string) {
    const lease = this.leases.get(objectId); if (!lease) return;
    const requestId = crypto.randomUUID(); this.releaseRequests.set(requestId, objectId);
    this.send({ type: 'lease.release', request_id: requestId, payload: { object_id: objectId, token: lease.token } });
    clearInterval(this.renewals.get(objectId)); this.renewals.delete(objectId); this.leases.delete(objectId);
    clearTimeout(this.throttle.get(`preview:${objectId}`)?.timer); this.throttle.delete(`preview:${objectId}`);
    for (const [id, value] of this.renewalRequests) if (value === objectId) this.renewalRequests.delete(id);
  }
  private releaseAll() {
    for (const id of this.leases.keys()) this.release(id);
    for (const request of this.leaseRequests.values()) { clearTimeout(request.timer); request.reject(Error('连接已结束。')); }
    this.leaseRequests.clear();
  }
  async command(command: Record<string, unknown>, object?: { id: string; version: number }, expectedRevision = this.state.revision) {
    if (!this.canWrite() || this.operations.size) throw Error('请先等待当前内容确认保存，再操作。');
    let lease: Lease | undefined;
    try {
      if (object) lease = await this.acquire(object.id, object.version);
      const operation: Operation = { type: 'stage.command', operation_id: crypto.randomUUID(), epoch: this.state.epoch, payload: { expected_revision: expectedRevision, command: { ...command, ...(lease ? { lease: { token: lease.token, version: lease.version } } : {}) } } };
      if (this.draft) this.recoveryConfirmed = false;
      this.operations.set(operation.operation_id, operation); this.emit();
      await new Promise<void>((resolve, reject) => { this.completions.set(operation.operation_id, { resolve, reject }); this.pumpOperations(); });
    } finally { if (object) this.release(object.id); }
  }
  async undoObject() {
    if (this.disposed || !this.canWrite() || this.hasUnconfirmedWork() || !this.state.objectUndo) throw Error('当前没有可用的对象撤销机会，请先处理未确认内容。');
    const summary = this.state.objectUndo;
    return this.command({ type: 'object.undo', source_operation_id: summary.source_operation_id, recovery_id: summary.recovery_id });
  }
  presence(stageId: string, field?: string, cursor?: number, map?: MapPresence) {
    this.lastPresence = { stageId, field, cursor, map };
    if (!this.state.connected) return;
    const text = field ? this.doc.getMap<Y.Text>('fields').get(field) : null;
    const relative = text && cursor !== undefined && this.operations.size === 0 ? encodeBytes(Y.encodeRelativePosition(Y.createRelativePositionFromTypeIndex(text, Math.min(cursor, text.length)))) : undefined;
    this.throttled('presence', { type: 'presence', payload: { stage_id: stageId, ...(field && text ? { field, ...(relative ? { relative_cursor: relative } : {}) } : {}), ...(map?.selectedObjectId ? { selected_object_id: map.selectedObjectId } : {}), ...(map?.pointer ? { pointer: { ...map.pointer } } : {}) } });
  }
  private throttled(key: string, message: object) {
    if (this.disposed) return;
    const entry = this.throttle.get(key) ?? { at: -Infinity };
    if (Date.now() - entry.at >= 100) {
      clearTimeout(entry.timer); entry.timer = undefined; entry.next = undefined;
      this.send(message); entry.at = Date.now();
    }
    else {
      entry.next = message;
      if (!entry.timer) entry.timer = setTimeout(() => {
        if (this.disposed || this.throttle.get(key) !== entry) return;
        entry.timer = undefined;
        const latest = entry.next; entry.next = undefined;
        if (latest) { this.send(latest); entry.at = Date.now(); }
      }, 100 - (Date.now() - entry.at));
    }
    this.throttle.set(key, entry);
  }
  private clearTransientMessages() {
    this.clearOperationSend();
    for (const entry of this.throttle.values()) clearTimeout(entry.timer);
    this.throttle.clear(); this.lastPresence = null;
  }
  preview(objectId: string, point: { x: number; y: number }) { const lease = this.leases.get(objectId); if (lease && lease.expires_at > Date.now()) this.throttled(`preview:${objectId}`, { type: 'map.preview', payload: { object_id: objectId, token: lease.token, ...point } }); }
  retry() {
    if (!this.state.connected) return;
    if ([...this.operations.values()].some(operation => operation.epoch !== this.state.epoch)) { this.emit({ error: friendly('COLLAB_STALE_EPOCH') }); return; }
    this.emit({ recovery: false, error: '' });
    this.clearOperationSend(); this.timestamps.clear(); this.pumpOperations();
  }
  async discard() {
    this.operations.clear(); this.timestamps.clear(); this.draft = null; this.recoveryConfirmed = false; this.recoveryHadPending = false; this.localDraft = null; this.drafts.clear();
    this.undo?.destroy(); this.undo = null; this.doc.destroy(); this.doc = new Y.Doc(); this.bindDoc();
    this.emit({ recovery: false, projection: null, error: '' }); await this.connect();
  }
  dispose() { this.disposed = true; ++this.generation; this.authenticated = false; this.authorityReady = false; this.actorId = null; this.state = { ...this.state, objectUndo: null, connected: false }; clearInterval(this.watchdog); this.clearTransientMessages(); this.releaseAll(); this.socket?.close(); this.undo?.destroy(); this.doc.destroy(); this.confirmedDoc.destroy(); this.listeners.clear(); }
}
