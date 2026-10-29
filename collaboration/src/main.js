import * as Y from 'yjs';
import { hasUnconfirmedWork, mapMoveOperation, parseLeaseGrant, sendAuth, settleProtocolError, shouldFinishLocalMapMove } from './client-protocol.mjs';
import { dragGrantAction, mapObjectCenter, updateDragPosition } from './drag-protocol.mjs';
import { applyCompositionEdit, applyTextDiff, captureComposition } from './text-editing.mjs';
import { bindLocalTextHistory, isPersistedTextUpdateOrigin } from './text-history.mjs';
import './style.css';
import './text-history.css';

const app = document.querySelector('#app');
const DEBUG_POINTER = new URLSearchParams(location.search).get('debugPointer') === '1';
const state = { ws: null, doc: null, undoManager: null, structure: null, epoch: null, revision: 0, selectedStep: 'step-1', participants: [], locks: [], token: '', actorId: null, connected: false, composing: new Map(), pending: new Map(), fieldBindings: new Map(), previews: new Map(), failedOperations: [], recoveryDraft: null, lease: null, drag: null, localUnconfirmed: false, rejectedLocal: false, saved: '尚未连接' };
const colors = { '角色 A': '#cb5a42', '角色 B': '#4b8b82' };

app.innerHTML = `
  <main class="shell">
    <header class="topbar"><div class="brand"><span class="brand-mark">S</span><div><strong>SmokeLine</strong><small>协作机制原型</small></div></div><div class="prototype-tag">本地技术原型 · 非正式战术内容</div><div class="online" id="online">等待连接</div><button id="reconnect" class="reconnect hidden">重连</button></header>
    <section class="notice"><strong>仅验证协作机制</strong><span>示例文本、步骤和标记均为非游戏验证内容，不构成投法、点位或游戏内教学。</span></section>
    <section id="gate" class="gate"><div class="gate-card"><div class="eyebrow">LOCAL COLLABORATION</div><h1>双人协作工作台</h1><p>在另一个浏览器标签中使用另一张本地测试票据，即可观察并发中文编辑与地图租约。</p><label for="ticket">本地测试票据</label><div class="ticket-row"><input id="ticket" autocomplete="off" placeholder="粘贴当前浏览器的独立票据"/><button id="connect" class="primary">进入原型</button></div><div id="gate-error" class="error"></div><div class="gate-footer">票据仅在本机有效，5 分钟过期且只能使用一次。</div></div></section>
    <section id="board" class="board hidden">
      <aside class="steps-panel"><div class="panel-heading"><span>演练步骤</span><span class="count">02</span></div><div id="step-list" class="step-list"></div><div class="panel-hint">每个步骤拥有独立地图状态</div></aside>
      <section class="map-panel"><div class="map-heading"><div><span class="eyebrow">MIRAGE · 本地协作机制示例</span><h2 id="map-title">步骤 1</h2></div><span class="map-badge">不含游戏验证</span></div><div id="map" class="map"><img src="/assets/maps/mirage.svg" alt="Mirage 协作原型底图"/><svg id="map-overlay" viewBox="0 0 100 100" preserveAspectRatio="none"></svg><div id="map-objects"></div></div><div id="map-hint" class="map-hint">拖动角色或路径 · 同一对象一次由一人持有租约</div></section>
      <aside class="editor-panel"><div class="editor-top"><div><span class="eyebrow">共享文本</span><h2>步骤说明</h2></div><div class="save-state" id="save-state">尚未连接</div></div><div class="text-history" role="group" aria-label="文字撤销与重做"><button id="undo-text" type="button" title="Ctrl/Cmd+Z">撤销</button><button id="redo-text" type="button" title="Ctrl/Cmd+Shift+Z">重做</button><span>仅撤销本地文字操作</span></div><div id="fields"></div><div class="presence-box"><div class="panel-heading"><span>房间内位置</span><span class="live-dot"></span></div><div id="presence"></div></div><button id="export" class="export-button">导出当前协作内容</button></aside>
    </section>
    <footer><span>本地隔离 PostgreSQL · 仅确认写入后显示已保存</span><span id="revision">revision —</span></footer>
  </main>`;

const $ = (selector) => app.querySelector(selector);
$('#connect').addEventListener('click', connect);
$('#ticket').addEventListener('keydown', (event) => { if (event.key === 'Enter') connect(); });
$('#export').addEventListener('click', exportDraft);
$('#reconnect').addEventListener('click', reconnect);
$('#undo-text').addEventListener('click', () => runTextHistory('undo'));
$('#redo-text').addEventListener('click', () => runTextHistory('redo'));
app.addEventListener('keydown', (event) => {
  if (!event.target.matches?.('[data-text-field]') || event.isComposing || state.composing.has(event.target.dataset.textField)) return;
  if ((!event.ctrlKey && !event.metaKey) || event.altKey || event.key.toLowerCase() !== 'z') return;
  event.preventDefault();
  runTextHistory(event.shiftKey ? 'redo' : 'undo');
});
window.addEventListener('beforeunload', (event) => {
  if (!hasUnconfirmedWork(state)) return;
  event.preventDefault();
  event.returnValue = '';
});

async function connect() {
  state.token = $('#ticket').value.trim();
  if (!state.token) { $('#gate-error').textContent = '请粘贴启动脚本生成的本地测试票据。'; return; }
  $('#gate-error').textContent = '';
  openSocket(state.token);
}

function openSocket(token) {
  $('#connect').disabled = true;
  $('#reconnect').classList.add('hidden');
  state.connected = false;
  updateEditableControls();
  const socket = new WebSocket(`ws://${location.host}/collab`);
  state.ws = socket;
  socket.addEventListener('open', () => sendAuth(socket, token));
  socket.addEventListener('message', (event) => handleMessage(JSON.parse(event.data)));
  socket.addEventListener('close', () => {
    state.connected = false;
    if (state.doc && state.pending.size > 0) {
      if (!state.recoveryDraft) state.recoveryDraft = readCurrentDraft();
      state.failedOperations.push(...state.pending.values());
      state.pending.clear();
      state.localUnconfirmed = true;
      state.rejectedLocal = true;
    }
    state.saved = state.localUnconfirmed || state.rejectedLocal ? '连接断开 · 有未确认内容，可导出' : '连接断开';
    updateSaveState();
    updateEditableControls();
    if (!state.doc) { $('#gate-error').textContent = '连接失败或票据无效；请重新生成本地测试票据。'; $('#connect').disabled = false; }
    else $('#reconnect').classList.remove('hidden');
    if (state.drag) finishMove(false);
  });
  socket.addEventListener('error', () => { $('#gate-error').textContent = '本地协作服务连接失败。'; $('#connect').disabled = false; });
}

async function reconnect() {
  if (!state.actorId) return;
  $('#reconnect').disabled = true;
  try {
    const response = await fetch('/__t0/test-tickets', { method: 'POST' });
    if (!response.ok) throw new Error('ticket_issue_failed');
    const issued = await response.json();
    const ticket = issued.participants.find((item) => item.actor_id === state.actorId)?.token;
    if (!ticket) throw new Error('ticket_actor_missing');
    state.token = ticket;
    openSocket(ticket);
  } catch {
    flash('本地重连票据生成失败；请联系本机原型启动端处理。');
  } finally {
    $('#reconnect').disabled = false;
  }
}

function handleMessage(message) {
  if (message.type === 'auth.ok') {
    state.actorId = message.actor_id;
    state.connected = true;
    updateEditableControls();
    sendPresence();
  } else if (message.type === 'sync') {
    state.epoch = message.epoch;
    state.revision = message.revision;
    state.structure = message.structure;
    state.participants = message.participants;
    state.locks = message.locks;
    if (state.doc && (state.localUnconfirmed || state.rejectedLocal) && !state.recoveryDraft) state.recoveryDraft = readCurrentDraft();
    if (state.pending.size > 0) {
      state.failedOperations.push(...state.pending.values());
      state.pending.clear();
    }
    resetDocument(message.state);
    if (state.recoveryDraft && stableJson({ fields: state.recoveryDraft.fields, structure: state.recoveryDraft.structure }) === stableJson({ fields: message.fields, structure: message.structure })) {
      state.localUnconfirmed = false;
      state.rejectedLocal = false;
      state.failedOperations = [];
      state.recoveryDraft = null;
    }
    if (!state.localUnconfirmed && !state.rejectedLocal) state.saved = '已从 PostgreSQL 恢复';
    $('#gate').classList.add('hidden');
    $('#board').classList.remove('hidden');
    renderStructure();
    renderParticipants();
    updateEditableControls();
    updateRevision();
    updateSaveState();
  } else if (message.type === 'participants') {
    state.participants = message.participants;
    renderParticipants();
  } else if (message.type === 'locks') {
    state.locks = message.locks;
    renderMap();
  } else if (message.type === 'operation.applied') {
    state.revision = message.revision;
    tracePointer('operation.applied', { operationType: message.operation_type, actorId: message.actor_id, revision: message.revision });
    if (message.operation_type === 'text.update') {
      applyRemoteUpdate(fromBase64(message.update));
      renderParticipants();
    }
    else { state.structure = message.structure; renderMap(); }
    const wasLocal = state.pending.delete(message.operation_id);
    if (wasLocal && state.pending.size === 0 && !state.rejectedLocal) state.localUnconfirmed = false;
    state.saved = state.rejectedLocal || state.localUnconfirmed ? '有未确认内容 · 可导出' : state.pending.size ? '正在保存…' : '已保存到本地 PostgreSQL';
    updateRevision();
    updateSaveState();
    if (shouldFinishLocalMapMove(message, wasLocal)) finishMove();
  } else if (message.type === 'operation.ack') {
    state.revision = Math.max(state.revision, message.revision);
    const wasLocal = state.pending.delete(message.operation_id);
    if (wasLocal && state.pending.size === 0 && !state.rejectedLocal) state.localUnconfirmed = false;
    state.saved = state.rejectedLocal || state.localUnconfirmed ? '有未确认内容 · 可导出' : state.pending.size ? '正在保存…' : '已保存到本地 PostgreSQL';
    updateRevision();
    updateSaveState();
  } else if (message.type === 'operation.rejected') {
    const failed = state.pending.get(message.operation_id);
    if (failed) state.failedOperations.push(failed);
    state.pending.delete(message.operation_id);
    if (!state.recoveryDraft && state.doc) state.recoveryDraft = readCurrentDraft();
    state.localUnconfirmed = true;
    state.rejectedLocal = true;
    state.saved = `未保存：${message.code} · 可导出本地内容`;
    tracePointer('operation.rejected', { code: message.code, operationType: failed?.type ?? null });
    updateSaveState();
    flash(message.code === 'COLLAB_LOCKED' ? '该对象正由另一位参与者拖动。' : `保存被拒绝（${message.code}）。本地内容仍可导出。`);
    if (failed?.type === 'map.move') finishMove();
  } else if (message.type === 'lease.granted') {
    const lease = parseLeaseGrant(message);
    tracePointer('lease.granted', { matchedRequest: state.drag?.requestId === lease?.requestId, objectId: lease?.objectId ?? null, hasToken: Boolean(lease?.token) });
    if (!lease) {
      if (state.drag?.requestId === message.request_id) finishMove(false);
      return;
    }
    if (state.drag?.requestId !== lease.requestId) {
      send({ type: 'lease.release', payload: { object_id: lease.objectId, token: lease.token } });
      return;
    }
    state.lease = { ...lease, version: state.drag.version, stepId: state.drag.stepId, x: state.drag.x, y: state.drag.y };
    const action = dragGrantAction(state.drag);
    if (action === 'release') { finishMove(); return; }
    if (action === 'commit') { commitMove(); return; }
    state.leaseTimer = setInterval(() => {
      const requestId = crypto.randomUUID();
      state.lease.renewRequestId = requestId;
      send({ type: 'lease.renew', request_id: requestId, payload: { object_id: state.lease.objectId, token: state.lease.token } });
    }, lease.renewEveryMs);
    if (state.drag.moved) send({ type: 'map.preview', payload: { object_id: state.drag.objectId, token: state.lease.token, x: state.drag.x, y: state.drag.y } });
    renderMap();
  } else if (message.type === 'map.preview') {
    state.previews.set(message.object_id, { x: message.x, y: message.y });
    renderMap();
  } else if (message.type === 'protocol.error') {
    tracePointer('protocol.error', { code: message.code, requestIdPresent: Boolean(message.request_id), hasLease: Boolean(state.lease) });
    settleProtocolError(message.code, Boolean(state.drag), () => finishMove(), flash);
  }
}

function renderStructure() {
  for (const { text, observer } of state.fieldBindings.values()) text.unobserve(observer);
  state.fieldBindings.clear();
  const current = state.structure.steps.find((step) => step.id === state.selectedStep) ?? state.structure.steps[0];
  state.selectedStep = current.id;
  $('#step-list').innerHTML = state.structure.steps.map((step, index) => `<button class="step-button ${step.id === current.id ? 'active' : ''}" data-step="${escapeAttr(step.id)}"><span class="step-number">0${index + 1}</span><span>${escapeHtml(step.title)}</span><span class="step-arrow">›</span></button>`).join('');
  $('#step-list').querySelectorAll('[data-step]').forEach((button) => button.addEventListener('click', () => { state.selectedStep = button.dataset.step; renderStructure(); sendPresence(); }));
  $('#map-title').textContent = current.title;
  const roleNames = new Map(state.structure.roles.map((role) => [role.id, role.name]));
  const fields = Object.entries(current.task_fields).map(([roleId, key]) => ({ key, label: `${roleNames.get(roleId)}任务`, role: roleNames.get(roleId) }));
  fields.push({ key: current.notes_field, label: '注意事项', role: null });
  $('#fields').innerHTML = fields.map(({ key, label, role }) => `
    <label class="field" data-field="${escapeAttr(key)}"><span class="field-label">${escapeHtml(label)}${role ? `<i style="--role:${colors[role]}">${escapeHtml(role)}</i>` : ''}</span><textarea data-text-field="${escapeAttr(key)}" maxlength="2000" rows="3" spellcheck="false"></textarea><span class="field-meta"><span data-cursors="${escapeAttr(key)}"></span><span data-count="${escapeAttr(key)}"></span></span></label>`).join('');
  for (const { key } of fields) bindTextarea(key);
  renderMap();
  renderParticipants();
  updateEditableControls();
}

function bindTextarea(key) {
  const textarea = $(`[data-text-field="${CSS.escape(key)}"]`);
  const text = state.doc.getMap('fields').get(key);
  const updateView = () => {
    const value = text.toString();
    if (document.activeElement !== textarea && !state.composing.has(key) && textarea.value !== value) textarea.value = value;
    $(`[data-count="${CSS.escape(key)}"]`).textContent = `${[...value].length}/2000`;
  };
  textarea.value = text.toString();
  updateView();
  text.observe(updateView);
  state.fieldBindings.set(key, { text, observer: updateView });
  textarea.addEventListener('compositionstart', () => {
    state.composing.set(key, captureComposition(text));
  });
  textarea.addEventListener('compositionend', () => {
    const composition = state.composing.get(key);
    state.composing.delete(key);
    const result = composition && applyCompositionEdit(state.doc, text, composition, textarea.value, textarea.selectionStart, textarea.selectionEnd);
    if (result?.ok) {
      textarea.value = result.value;
      const start = Y.createAbsolutePositionFromRelativePosition(result.selection.start, state.doc);
      const end = Y.createAbsolutePositionFromRelativePosition(result.selection.end, state.doc);
      if (start?.type === text && end?.type === text) textarea.setSelectionRange(start.index, end.index, textarea.selectionDirection);
      else compositionConflict(key, textarea, text);
    } else compositionConflict(key, textarea, text);
    sendPresence(key, textarea.selectionStart);
    updateView();
  });
  textarea.addEventListener('input', () => {
    if (state.composing.has(key)) return;
    applyTextDiff(state.doc, text, textarea.value);
    sendPresence(key, textarea.selectionStart);
  });
  textarea.addEventListener('focus', () => { textarea.closest('.field').classList.add('focused'); sendPresence(key, textarea.selectionStart); });
  textarea.addEventListener('blur', () => textarea.closest('.field').classList.remove('focused'));
  textarea.addEventListener('keyup', () => sendPresence(key, textarea.selectionStart));
  textarea.addEventListener('click', () => sendPresence(key, textarea.selectionStart));
}

function compositionConflict(key, textarea, text) {
  if (!state.recoveryDraft) state.recoveryDraft = readCurrentDraft();
  else state.recoveryDraft.fields[key] = textarea.value;
  state.localUnconfirmed = true;
  state.rejectedLocal = true;
  state.saved = '组合输入未安全合并 · 本地副本可导出';
  textarea.value = text.toString();
  textarea.setSelectionRange(text.length, text.length);
  flash('组合输入遇到并发变更，无法安全合并；本地副本已保留，可导出。');
  updateSaveState();
}

function resetDocument(encodedState) {
  for (const { text, observer } of state.fieldBindings.values()) text.unobserve(observer);
  state.fieldBindings.clear();
  state.undoManager?.destroy();
  state.undoManager = null;
  state.doc?.destroy();
  state.composing.clear();
  state.doc = new Y.Doc();
  Y.applyUpdate(state.doc, fromBase64(encodedState), 'server');
  const root = state.doc.getMap('fields');
  const fieldKeys = state.structure.steps.flatMap((step) => [...Object.values(step.task_fields), step.notes_field]);
  const textTypes = [...new Set(fieldKeys)].map((key) => root.get(key)).filter((value) => value instanceof Y.Text);
  const undoManager = bindLocalTextHistory(textTypes, updateUndoControls);
  state.undoManager = undoManager;
  state.doc.on('update', (update, origin) => {
    if (!isPersistedTextUpdateOrigin(origin, undoManager)) return;
    const operationId = crypto.randomUUID();
    const operation = { type: 'text.update', operation_id: operationId, epoch: state.epoch, payload: { update: toBase64(update) } };
    state.pending.set(operationId, operation);
    state.localUnconfirmed = true;
    state.saved = '正在保存…';
    updateSaveState();
    send({ type: 'operation', operation });
  });
}

function applyRemoteUpdate(update) {
  const active = document.activeElement?.matches?.('[data-text-field]') ? document.activeElement : null;
  const key = active?.dataset.textField;
  const text = key ? state.doc.getMap('fields').get(key) : null;
  let selection = null;
  if (text && !state.composing.has(key)) {
    selection = {
      start: Y.createRelativePositionFromTypeIndex(text, active.selectionStart),
      end: Y.createRelativePositionFromTypeIndex(text, active.selectionEnd),
      direction: active.selectionDirection,
    };
  }
  Y.applyUpdate(state.doc, update, 'server');
  if (text && selection && !state.composing.has(key)) {
    active.value = text.toString();
    const start = Y.createAbsolutePositionFromRelativePosition(selection.start, state.doc);
    const end = Y.createAbsolutePositionFromRelativePosition(selection.end, state.doc);
    if (start?.type === text && end?.type === text) active.setSelectionRange(start.index, end.index, selection.direction);
    $(`[data-count="${CSS.escape(key)}"]`).textContent = `${[...text.toString()].length}/2000`;
  }
}

function updateEditableControls() {
  for (const textarea of app.querySelectorAll('[data-text-field]')) textarea.disabled = !state.connected;
  updateUndoControls();
  $('#map')?.classList.toggle('offline', !state.connected);
}

function updateUndoControls() {
  const online = state.connected && Boolean(state.undoManager);
  $('#undo-text').disabled = !online || state.undoManager.undoStack.length === 0;
  $('#redo-text').disabled = !online || state.undoManager.redoStack.length === 0;
}

function runTextHistory(action) {
  const manager = state.undoManager;
  if (!state.connected || !manager || !['undo', 'redo'].includes(action)) return;
  const active = document.activeElement?.matches?.('[data-text-field]') ? document.activeElement : null;
  const key = active?.dataset.textField;
  const text = key ? state.doc.getMap('fields').get(key) : null;
  const selection = text ? {
    start: Y.createRelativePositionFromTypeIndex(text, active.selectionStart),
    end: Y.createRelativePositionFromTypeIndex(text, active.selectionEnd),
    direction: active.selectionDirection,
  } : null;
  const stack = action === 'undo' ? manager.undoStack : manager.redoStack;
  if (!stack.length) return;
  manager[action]();
  for (const [fieldKey, binding] of state.fieldBindings) {
    const textarea = $(`[data-text-field="${CSS.escape(fieldKey)}"]`);
    if (!textarea || state.composing.has(fieldKey)) continue;
    textarea.value = binding.text.toString();
    $(`[data-count="${CSS.escape(fieldKey)}"]`).textContent = `${[...textarea.value].length}/2000`;
  }
  if (active && selection && text) {
    const start = Y.createAbsolutePositionFromRelativePosition(selection.start, state.doc);
    const end = Y.createAbsolutePositionFromRelativePosition(selection.end, state.doc);
    if (start?.type === text && end?.type === text) active.setSelectionRange(start.index, end.index, selection.direction);
  }
  updateUndoControls();
}

function renderMap() {
  if (!state.structure) return;
  const step = state.structure.steps.find((item) => item.id === state.selectedStep);
  const overlay = $('#map-overlay');
  const objects = $('#map-objects');
  overlay.innerHTML = step.objects.filter((object) => object.kind === 'arrow').map((object) => `<polyline class="route" points="${object.points.map((point) => `${point.x},${point.y}`).join(' ')}"/>`).join('');
  const ids = step.objects.map((object) => object.id).join('|');
  if (objects.dataset.step !== step.id || objects.dataset.ids !== ids) {
    tracePointer('map.dom-rebuild', { stepId: step.id, reason: objects.dataset.step !== step.id ? 'step' : 'objects' });
    objects.replaceChildren();
    objects.dataset.step = step.id;
    objects.dataset.ids = ids;
    for (const object of step.objects) {
      const button = document.createElement('button');
      const kind = object.kind === 'role' ? 'role-marker' : object.kind === 'arrow' ? 'arrow-marker' : `utility-marker ${object.kind}`;
      button.className = `map-object ${kind}`;
      button.dataset.object = object.id;
      button.dataset.step = step.id;
      button.type = 'button';
      button.draggable = false;
      button.innerHTML = `<span></span><small></small>`;
      button.addEventListener('pointerdown', (event) => beginDrag(event, object.id, step.id));
      button.addEventListener('dragstart', (event) => { event.preventDefault(); tracePointer('native-drag-blocked', { objectId: object.id }); });
      button.addEventListener('lostpointercapture', onLostPointerCapture);
      objects.append(button);
    }
  }
  for (const object of step.objects) {
    const button = objects.querySelector(`[data-object="${CSS.escape(object.id)}"]`);
    const preview = state.previews.get(object.id);
    let { x, y } = mapObjectCenter(object);
    if (preview) ({ x, y } = preview);
    const lock = state.locks.find((item) => item.objectId === object.id);
    const icon = object.kind === 'role' ? object.label.slice(-1) : object.kind === 'arrow' ? '↗' : object.kind === 'smoke' ? '烟' : object.kind === 'flash' ? '闪' : '·';
    button.classList.toggle('locked', Boolean(lock));
    button.style.left = `${x}%`;
    button.style.top = `${y}%`;
    button.style.setProperty('--owner', lock?.color ?? '#171711');
    button.setAttribute('aria-label', `${object.label}${lock ? `，${lock.owner}正在拖动` : ''}`);
    button.querySelector('span').textContent = icon;
    button.querySelector('small').textContent = lock ? `${lock.owner} 正在拖动` : object.label;
  }
}

function beginDrag(event, objectId, stepId) {
  if (event.button !== 0 || state.drag || state.lease || !state.connected || !state.ws || state.ws.readyState !== WebSocket.OPEN) return;
  event.preventDefault();
  tracePointer('pointerdown', { objectId, pointerId: event.pointerId, pointerType: event.pointerType });
  const step = state.structure.steps.find((item) => item.id === stepId);
  const object = step.objects.find((item) => item.id === objectId);
  const { x, y } = mapObjectCenter(object);
  const bounds = $('#map').getBoundingClientRect();
  const pointerX = clamp((event.clientX - bounds.left) / bounds.width * 100);
  const pointerY = clamp((event.clientY - bounds.top) / bounds.height * 100);
  state.drag = { objectId, stepId, version: object.version, startX: x, startY: y, offsetX: x - pointerX, offsetY: y - pointerY, x, y, moved: false, ended: false, cancelled: false, requestId: crypto.randomUUID(), pointerId: event.pointerId };
  state.drag.pointerTarget = event.currentTarget;
  event.currentTarget.setPointerCapture(event.pointerId);
  tracePointer('pointercapture.set', { objectId, captured: event.currentTarget.hasPointerCapture(event.pointerId) });
  send({ type: 'lease.acquire', request_id: state.drag.requestId, payload: { step_id: stepId, object_id: objectId, version: object.version } });
  const pointerMove = (move) => {
    if (move.pointerId !== state.drag?.pointerId || state.drag.ended) return;
    if (!state.drag.tracedFirstMove) {
      state.drag.tracedFirstMove = true;
      tracePointer('pointermove', { objectId, pointerId: move.pointerId, leaseReady: Boolean(state.lease) });
    }
    const bounds = $('#map').getBoundingClientRect();
    updateDragPosition(state.drag, clamp((move.clientX - bounds.left) / bounds.width * 100 + state.drag.offsetX), clamp((move.clientY - bounds.top) / bounds.height * 100 + state.drag.offsetY));
    state.previews.set(objectId, { x: state.drag.x, y: state.drag.y });
    if (state.lease?.objectId === objectId) {
      state.lease.x = state.drag.x;
      state.lease.y = state.drag.y;
      send({ type: 'map.preview', payload: { object_id: objectId, token: state.lease.token, x: state.drag.x, y: state.drag.y } });
    }
    renderMap();
  };
  const pointerUp = (up) => {
    tracePointer('pointerup', { pointerId: up.pointerId, matchesDrag: up.pointerId === state.drag?.pointerId, leaseObjectId: state.lease?.objectId ?? null });
    if (up.pointerId !== state.drag?.pointerId || state.drag.ended) return;
    pointerMove(up);
    state.drag.ended = true;
    detachDragListeners(state.drag);
    if (state.lease?.objectId === objectId) {
      if (dragGrantAction(state.drag) === 'commit') commitMove();
      else finishMove();
    }
  };
  const pointerCancel = (cancel) => {
    tracePointer('pointercancel', { pointerId: cancel.pointerId, matchesDrag: cancel.pointerId === state.drag?.pointerId, hasLease: Boolean(state.lease) });
    if (cancel.pointerId !== state.drag?.pointerId || state.drag.ended) return;
    state.drag.ended = true;
    state.drag.cancelled = true;
    detachDragListeners(state.drag);
    if (state.lease?.objectId === objectId) finishMove();
  };
  state.drag.handlers = { pointerMove, pointerUp, pointerCancel };
  window.addEventListener('pointermove', pointerMove, true);
  window.addEventListener('pointerup', pointerUp, true);
  window.addEventListener('pointercancel', pointerCancel, true);
}

function commitMove() {
  const drag = state.drag;
  const lease = state.lease;
  if (!drag || !lease) return;
  detachDragListeners(drag);
  const operationId = crypto.randomUUID();
  const operation = mapMoveOperation({ operationId, epoch: state.epoch, drag, lease });
  tracePointer('map.move.send', { objectId: drag.objectId, stepId: drag.stepId, version: drag.version });
  state.pending.set(operationId, operation);
  state.saved = '正在保存地图变更…';
  updateSaveState();
  send({ type: 'operation', operation });
  $('#map-hint').textContent = '落点提交中…';
}

function detachDragListeners(drag) {
  if (!drag?.handlers) return;
  window.removeEventListener('pointermove', drag.handlers.pointerMove, true);
  window.removeEventListener('pointerup', drag.handlers.pointerUp, true);
  window.removeEventListener('pointercancel', drag.handlers.pointerCancel, true);
  if (drag.pointerTarget?.hasPointerCapture(drag.pointerId)) drag.pointerTarget.releasePointerCapture(drag.pointerId);
  delete drag.handlers;
}

function onLostPointerCapture(event) {
  tracePointer('pointercapture.lost', { pointerId: event.pointerId, matchesDrag: event.pointerId === state.drag?.pointerId, ended: state.drag?.ended ?? true });
  if (state.drag?.pointerId !== event.pointerId || state.drag.ended) return;
  state.drag.ended = true;
  state.drag.cancelled = true;
  detachDragListeners(state.drag);
  if (state.lease) finishMove();
}

function finishMove(release = true) {
  tracePointer('drag.finish', { objectId: state.drag?.objectId ?? state.lease?.objectId ?? null, release, hasLease: Boolean(state.lease) });
  if (state.leaseTimer) clearInterval(state.leaseTimer);
  detachDragListeners(state.drag);
  if (release && state.lease) send({ type: 'lease.release', payload: { object_id: state.lease.objectId, token: state.lease.token } });
  state.lease = null;
  state.drag = null;
  state.previews.clear();
  $('#map-hint').textContent = '拖动角色或路径 · 同一对象一次由一人持有租约';
  renderMap();
}

function renderParticipants() {
  if (!state.structure) return;
  $('#online').textContent = `${state.participants.length} 位参与者在线`;
  $('#presence').innerHTML = state.participants.map((person) => {
    const step = state.structure.steps.find((item) => item.id === person.step_id)?.title ?? '当前房间';
    const field = person.field ? ` · ${fieldName(person.field)}` : '';
    const cursor = person.field ? ` · 光标 ${relativeCursorIndex(person) ?? '—'}` : '';
    return `<div class="participant"><span class="avatar" style="--role:${person.color}">${escapeHtml(person.name.slice(-1))}</span><div><strong>${escapeHtml(person.name)}${person.name === '参与者 A' || person.name === '参与者 B' ? ' · 你们' : ''}</strong><small>${escapeHtml(step)}${escapeHtml(field)}${escapeHtml(cursor)}</small></div><span class="participant-dot" style="--role:${person.color}"></span></div>`;
  }).join('') || '<div class="empty-presence">暂时没有参与者</div>';
  for (const node of app.querySelectorAll('[data-cursors]')) {
    const key = node.dataset.cursors;
    const active = state.participants.filter((person) => person.field === key && person.relative_cursor);
    node.innerHTML = active.map((person) => `<span class="cursor-chip" style="--role:${person.color}">${escapeHtml(person.name)} · 光标 ${relativeCursorIndex(person) ?? '—'}</span>`).join('');
  }
}

function sendPresence(field = null, cursor = null) {
  const text = field ? state.doc?.getMap('fields').get(field) : null;
  const relative = text ? Y.createRelativePositionFromTypeIndex(text, Math.min(cursor ?? 0, text.length)) : null;
  const relativeCursor = relative ? toBase64(Y.encodeRelativePosition(relative)) : null;
  send({ type: 'presence', payload: { step_id: state.selectedStep, ...(field ? { field, relative_cursor: relativeCursor } : {}) } });
}

function relativeCursorIndex(person) {
  try {
    const text = person.field ? state.doc.getMap('fields').get(person.field) : null;
    if (!text || !person.relative_cursor) return null;
    const relative = Y.decodeRelativePosition(fromBase64(person.relative_cursor));
    const absolute = Y.createAbsolutePositionFromRelativePosition(relative, state.doc);
    return absolute?.type === text ? absolute.index : null;
  } catch { return null; }
}

function updateSaveState() { const element = $('#save-state'); if (element) { element.textContent = state.saved; element.classList.toggle('saving', state.saved.includes('正在')); element.classList.toggle('failed', state.saved.includes('未保存')); } }
function updateRevision() { $('#revision').textContent = `revision ${state.revision}`; }
function send(message) { if (state.connected && state.ws?.readyState === WebSocket.OPEN) state.ws.send(JSON.stringify(message)); }
function flash(message) { $('#map-hint').textContent = message; window.setTimeout(() => { if ($('#map-hint').textContent === message) $('#map-hint').textContent = '拖动角色或路径 · 同一对象一次由一人持有租约'; }, 2500); }
function fieldName(key) { return key.replace(/^step-[0-9]+\./, '').replace('role-a', '角色 A任务').replace('role-b', '角色 B任务').replace('notes', '注意事项').replace('tactic.', '战术 '); }
function clamp(value) { return Math.max(0, Math.min(100, value)); }
function toBase64(bytes) { let binary = ''; for (const byte of bytes) binary += String.fromCharCode(byte); return btoa(binary); }
function fromBase64(value) { return Uint8Array.from(atob(value), (character) => character.charCodeAt(0)); }
function escapeHtml(value) { return String(value).replace(/[&<>"']/g, (character) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[character]); }
function escapeAttr(value) { return escapeHtml(value); }

function tracePointer(event, details) { if (DEBUG_POINTER) console.debug(`[T0 pointer] ${event}`, details); }

function exportDraft() {
  if (!state.doc || !state.structure) return;
  const data = new Blob([JSON.stringify({ warning: 'T0本地协作原型导出；非正式战术内容。', current: readCurrentDraft(), recovery_copy: state.recoveryDraft, failed_operations: state.failedOperations }, null, 2)], { type: 'application/json' });
  const link = document.createElement('a'); link.href = URL.createObjectURL(data); link.download = 'smokeline-t0-unconfirmed-export.json'; link.click(); URL.revokeObjectURL(link.href);
}

function readCurrentDraft() {
  if (!state.doc || !state.structure) return null;
  const root = state.doc.getMap('fields');
  const fields = Object.fromEntries([...root.keys()].map((key) => [key, root.get(key).toString()]));
  for (const textarea of app.querySelectorAll('[data-text-field]')) fields[textarea.dataset.textField] = textarea.value;
  return {
    epoch: state.epoch, revision: state.revision, structure: structuredClone(state.structure),
    fields,
  };
}

function stableJson(value) {
  if (Array.isArray(value)) return `[${value.map(stableJson).join(',')}]`;
  if (value && typeof value === 'object') return `{${Object.keys(value).sort().map((key) => `${JSON.stringify(key)}:${stableJson(value[key])}`).join(',')}}`;
  return JSON.stringify(value);
}
