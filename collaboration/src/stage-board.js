import * as Y from 'yjs';
import { applyConfirmedUpdate, encodeConfirmedDocument, replaceConfirmedDocument } from './confirmed-document.mjs';
import { bindLocalTextHistory, isPersistedTextUpdateOrigin } from './text-history.mjs';
import { applyCompositionEdit, applyTextDiff, captureComposition } from './text-editing.mjs';
import { actionDescriptionField, expectedStageTextFields, roleNameField, stageConditionField, stageDescriptionField, stageNameField } from './stage-board-schema.mjs';
import { groupStageActions, stagePlaybackFrame } from './stage-playback.mjs';
import { mapClickIntent, mapPointFromRect, moveCommandFromDrawing } from './stage-board-interactions.mjs';
import './stage-board.css';
import './stage-board-responsive.css';

const app = document.querySelector('#stage-app');
const DOCUMENT_ID = 'mirage-stage-prototype';
const state = {
  ws: null, token: '', doc: null, confirmedDoc: null, undoManager: null, structure: null, epoch: null, revision: 0,
  actorId: null, connected: false, synced: false, confirmedState: null, participants: [], pending: new Map(), failedOperations: [],
  recoveryDraft: null, rejectedLocal: false, localUnconfirmed: false, fieldObservers: new Map(),
  composing: new Map(), selectedStageId: null, selectedRoleId: null, selectedActionId: null,
  lastCommand: null, drawing: null, draftTarget: null, tool: 'select', view: { zoom: 1, x: 0, y: 0, pan: false },
  playback: null, animationFrame: null, playbackChanged: false, suppressMapClick: false, formDirty: false,
  saved: '尚未连接', mobileReadOnly: matchMedia('(max-width: 700px)').matches,
};

app.innerHTML = `
  <main class="stage-shell">
    <header class="stage-topbar"><a class="stage-brand" href="/" aria-label="返回协作原型"><span class="brand-mark">S</span><span><strong>SmokeLine</strong><small>阶段式动态战术板</small></span></a><div class="stage-headline"><span class="stage-eyebrow">MIRAGE · 本地阶段编排原型</span><h1>动态战术板</h1></div><div class="stage-connection"><span id="stage-online">等待连接</span><button id="stage-reconnect" class="button hidden" type="button">重连</button></div></header>
    <section class="stage-notice"><strong>本地协作原型</strong><span>只在隔离本地数据库中保存；示例不含实战投法或游戏内教学。</span><a href="/">旧版协作入口</a></section>
    <section id="stage-gate" class="stage-gate"><div class="stage-gate-card"><span class="stage-eyebrow">LOCAL STAGE BOARD</span><h2>打开你的阶段式战术本</h2><p>需要本地服务生成的一次性参与者票据。另一个浏览器标签使用另一张票据，可共同查看协作更新。</p><label for="stage-ticket">本地测试票据</label><div class="ticket-row"><input id="stage-ticket" autocomplete="off" placeholder="粘贴当前浏览器的独立票据"/><button id="stage-connect" class="button primary" type="button">进入动态战术板</button></div><div id="stage-gate-error" class="stage-error" role="status"></div><small>此页面绑定独立文档 mirage-stage-prototype，不会写入旧协作原型。</small></div></section>
    <section id="stage-workspace" class="stage-workspace hidden">
      <div id="mobile-readonly" class="mobile-readonly hidden">手机端仅展示已保存的阶段、动作与地图；请使用电脑编辑。</div>
      <aside class="stage-panel panel"><div class="panel-title"><div><span class="stage-eyebrow">编排</span><h2>阶段与角色</h2></div><span id="stage-count" class="small-note"></span></div><div id="stage-list" class="stage-list"></div><div class="button-row edit-only"><button id="stage-add" class="button" type="button">＋ 新阶段</button><button id="role-add" class="button" type="button">＋ 新角色</button></div><div class="stage-divider"></div><div class="panel-title"><h3>角色名称</h3><span class="small-note">最多 5 人</span></div><div id="role-list" class="role-list"></div><div class="stage-divider"></div><div class="stage-meta" id="stage-meta"></div></aside>
      <section class="map-panel panel"><div class="map-title-row"><div><span class="stage-eyebrow">阶段地图</span><h2 id="map-stage-name">—</h2><p id="map-stage-summary" class="map-summary"></p></div><div class="map-heading-actions"><button id="toggle-sidebar" class="button" type="button">折叠侧栏</button><span id="revision-badge" class="revision-badge">rev —</span></div></div><div id="playback-changed" class="playback-changed hidden">预览期间收到新修改；退出预览后查看。</div><div class="map-toolbar"><div class="tool-group edit-only"><button id="tool-select" class="button active" type="button">选择</button><button id="tool-position" class="button" type="button">设置站位</button><button id="tool-pan" class="button" type="button">平移地图</button></div><div class="view-group"><label for="map-zoom">缩放</label><input id="map-zoom" type="range" min="1" max="4" step="0.1" value="1"/><span id="zoom-value">1.0×</span><button id="map-reset" class="button" type="button">复位</button><button id="map-fullscreen" class="button" type="button">全屏</button></div></div><div id="map-viewport" class="map-viewport" aria-label="Mirage 动态战术地图"><div id="map-world" class="map-world"><div id="map-zoom-layer" class="map-zoom-layer"><img class="map-image" src="/assets/maps/mirage.svg" alt="授权 Mirage 战术地图底图"/><svg id="map-svg" viewBox="0 0 100 100" preserveAspectRatio="none" aria-hidden="true"></svg><div id="map-markers" class="map-markers"></div></div></div></div><div class="map-foot"><span id="map-hint">选择角色后可设置站位或创建动作。</span><div class="edit-only drawing-tools"><button id="undo-route-point" class="button hidden" type="button">撤回末点</button><button id="finish-drawing" class="button primary hidden" type="button">完成绘制</button><button id="cancel-drawing" class="button hidden" type="button">取消</button></div></div><div id="playback-actions" class="playback-actions"><button id="play-auto" class="button primary" data-playback-start type="button">自动演示</button><button id="play-stage" class="button" data-playback-start type="button">分阶段演示</button><button id="play-pause" class="button hidden" data-playback-only type="button" disabled>暂停</button><button id="play-replay" class="button hidden" data-playback-only type="button">重播本阶段</button><button id="play-prev" class="button hidden" data-playback-only type="button">上一阶段</button><button id="play-next" class="button hidden" data-playback-only type="button">下一阶段</button><button id="play-exit" class="button hidden" data-playback-only type="button">退出预览</button></div><div id="playback-summary" class="playback-summary hidden"></div></section>
      <aside class="action-panel panel"><div class="panel-title"><div><span class="stage-eyebrow">动作与说明</span><h2 id="editor-stage-name">阶段</h2></div><div id="save-state" class="save-state">尚未连接</div></div><div class="text-history edit-only"><button id="undo-text" class="button" type="button" title="Ctrl/Cmd+Z">撤销文字</button><button id="redo-text" class="button" type="button" title="Ctrl/Cmd+Shift+Z">重做文字</button><span>仅本连接的文字操作</span></div><div id="recovery-banner" class="recovery-banner hidden"><span>有未确认草稿，导出不会将它标记为已保存。</span><button id="export-recovery" class="button" type="button">导出恢复副本</button></div><div id="stage-text-fields"></div><div class="stage-actions-title"><h3>动作列表</h3><span id="action-count" class="small-note"></span></div><div id="action-list" class="action-list"></div><form id="action-form" class="action-form edit-only"><h3>添加动作</h3><label>动作类型<select name="kind"><option value="move">移动路线</option><option value="wait">等待</option><option value="observe">观察方向</option><option value="utility">道具投掷</option><option value="text">文字提示</option></select></label><label id="action-role-label">执行角色<select name="role_id"></select></label><label id="utility-type-label" class="hidden">道具<select name="utility_type"><option value="smoke">烟雾弹</option><option value="flash">闪光弹</option><option value="molotov">燃烧弹</option><option value="he">高爆手雷</option><option value="decoy">诱饵弹</option></select></label><label>与上一动作<select name="group_mode"><option value="after">接着执行</option><option value="parallel">与上一动作同时</option></select></label><label>动作说明<textarea name="description" rows="2" maxlength="4000" required placeholder="用文字说明这个动作的目的或提醒"></textarea></label><div id="draft-target" class="draft-target"></div><button class="button primary" type="submit">添加到阶段</button><div id="action-error" class="stage-error" role="status"></div></form><div class="stage-actions-title"><h3>可恢复副本</h3><span id="recovery-count" class="small-note"></span></div><div id="recovery-list" class="recovery-list"></div><div class="stage-footer"><span id="presence"></span><button id="export-board" class="button" type="button">导出协作副本</button></div></aside>
    </section>
    <footer class="stage-footer-line"><span>隔离本地 PostgreSQL · 仅确认提交后显示已保存</span><span id="stage-revision">revision —</span></footer>
  </main>`;

const $ = (selector) => app.querySelector(selector);
const $$ = (selector) => [...app.querySelectorAll(selector)];
$('#stage-connect').addEventListener('click', connectWithTicket);
$('#stage-ticket').addEventListener('keydown', (event) => { if (event.key === 'Enter') connectWithTicket(); });
$('#stage-reconnect').addEventListener('click', reconnect);
$('#export-board').addEventListener('click', exportRecovery);
$('#export-recovery').addEventListener('click', exportRecovery);
$('#stage-add').addEventListener('click', () => submitCommand({ type: 'stage.add', after_stage_id: state.selectedStageId }));
$('#role-add').addEventListener('click', () => submitCommand({ type: 'role.add' }));
$('#tool-select').addEventListener('click', () => setTool('select'));
$('#tool-position').addEventListener('click', () => setTool('position'));
$('#tool-pan').addEventListener('click', () => { state.view.pan = !state.view.pan; state.tool = state.view.pan ? 'pan' : 'select'; renderToolbar(); });
$('#map-reset').addEventListener('click', resetMapView);
$('#map-zoom').addEventListener('input', (event) => { state.view.zoom = Number(event.target.value); renderMap(); $('#zoom-value').textContent = `${state.view.zoom.toFixed(1)}×`; });
$('#map-fullscreen').addEventListener('click', toggleFullscreen);
$('#action-form').addEventListener('submit', addActionFromForm);
$('#action-form').addEventListener('input', () => { state.formDirty = true; updateSaveState(); });
$('#finish-drawing').addEventListener('click', finishDrawing);
$('#cancel-drawing').addEventListener('click', cancelDrawing);
$('#play-auto').addEventListener('click', () => startPlayback('auto'));
$('#play-stage').addEventListener('click', () => startPlayback('stage'));
$('#play-pause').addEventListener('click', togglePlaybackPause);
$('#play-replay').addEventListener('click', replayStage);
$('#play-prev').addEventListener('click', () => navigatePlayback(-1));
$('#play-next').addEventListener('click', () => navigatePlayback(1));
$('#play-exit').addEventListener('click', exitPlayback);
$('#undo-text').addEventListener('click', () => runTextHistory('undo'));
$('#redo-text').addEventListener('click', () => runTextHistory('redo'));
$('#undo-route-point').addEventListener('click', () => { if (state.drawing?.points.length > 1) state.drawing.points.pop(); renderMap(); });
const recoveryActions = document.createElement('div'); recoveryActions.className = 'recovery-actions';
const exportRecoveryButton = $('#export-recovery');
const retryFailedButton = document.createElement('button'); retryFailedButton.id = 'retry-failed-operation'; retryFailedButton.className = 'button'; retryFailedButton.type = 'button'; retryFailedButton.textContent = '重试失败操作';
const discardDraftButton = document.createElement('button'); discardDraftButton.id = 'discard-local-draft'; discardDraftButton.className = 'button danger'; discardDraftButton.type = 'button'; discardDraftButton.textContent = '确认放弃并重载';
recoveryActions.append(exportRecoveryButton, retryFailedButton, discardDraftButton); $('#recovery-banner').append(recoveryActions);
$('#retry-failed-operation').addEventListener('click', retryFailedOperation);
$('#discard-local-draft').addEventListener('click', discardLocalDraft);
$('#toggle-sidebar').addEventListener('click', () => {
  const workspace = $('#stage-workspace');
  const collapsed = workspace.classList.toggle('sidebar-collapsed');
  $('#toggle-sidebar').textContent = collapsed ? '展开侧栏' : '折叠侧栏';
});
app.addEventListener('keydown', (event) => {
  if (!event.target.matches?.('[data-stage-text]') || event.isComposing || state.composing.has(event.target.dataset.stageText)) return;
  if ((!event.ctrlKey && !event.metaKey) || event.altKey || event.key.toLowerCase() !== 'z') return;
  event.preventDefault(); runTextHistory(event.shiftKey ? 'redo' : 'undo');
});
$('#stage-list').addEventListener('click', (event) => {
  const button = event.target.closest('[data-stage-select]');
  if (!button) return;
  const id = button.dataset.stageSelect;
  if (id === state.selectedStageId) return;
  if (state.playback) {
    const index = state.playback.snapshot.structure.stages.findIndex((stage) => stage.id === id);
    if (index >= 0) {
      state.playback.stageIndex = index; state.playback.elapsed = 0; state.playback.startedAt = performance.now(); state.playback.done = false;
      state.playback.waiting = false; state.selectedStageId = id; render(); startAnimation();
    }
  } else {
    if (state.drawing && !confirm('切换阶段将取消尚未提交的地图绘制。继续？')) return;
    state.drawing = null; state.positionPreview = null; state.draftTarget = null;
    state.selectedStageId = id; state.selectedActionId = null; render(); sendPresence();
  }
});
$('#stage-list').addEventListener('click', (event) => {
  const button = event.target.closest('[data-stage-control]');
  if (!button) return;
  void runStageControl(button.dataset.stageControl, button.dataset.stageId);
});
$('#role-list').addEventListener('click', (event) => {
  const button = event.target.closest('[data-role-select]');
  if (!button) return;
  state.selectedRoleId = button.dataset.roleSelect;
  setTool('position'); renderToolbar();
});
$('#stage-meta').addEventListener('click', (event) => {
  const button = event.target.closest('[data-stage-control]');
  if (button) void runStageControl(button.dataset.stageControl, button.dataset.stageId);
});
$('#action-list').addEventListener('click', (event) => {
  const button = event.target.closest('[data-action-control]');
  if (!button) return;
  void runActionControl(button.dataset.actionControl, button.dataset.actionId);
});
$('#action-list').addEventListener('change', (event) => {
  const select = event.target.closest('[data-action-change]');
  if (select) void updateActionFromSelect(select);
});
$('#recovery-list').addEventListener('click', (event) => {
  const button = event.target.closest('[data-recovery-restore]');
  if (button) void restoreRecovery(button.dataset.recoveryRestore);
});
$('#map-markers').addEventListener('click', (event) => {
  const button = event.target.closest('[data-role-marker]');
  if (!button) return;
  state.selectedRoleId = button.dataset.roleMarker;
  renderMap(); renderRoleList();
});
$('#map-markers').addEventListener('pointerdown', beginRolePlacement);
$('#map-viewport').addEventListener('click', onMapClick);
$('#map-viewport').addEventListener('pointerdown', beginPan);
window.addEventListener('beforeunload', (event) => {
  if (!hasUnconfirmedWork()) return;
  event.preventDefault(); event.returnValue = '';
});
window.addEventListener('resize', () => { state.mobileReadOnly = matchMedia('(max-width: 700px)').matches; render(); });
document.addEventListener('visibilitychange', () => { if (document.hidden && state.playback && !state.playback.paused) pausePlayback(); });
document.addEventListener('fullscreenchange', () => {
  const active = document.fullscreenElement === $('.map-panel');
  $('#map-fullscreen').textContent = active ? '退出全屏' : '全屏';
  $('#map-fullscreen').setAttribute('aria-pressed', String(active));
});
document.addEventListener('keydown', (event) => {
  if (event.key === 'Escape' && document.fullscreenElement === $('.map-panel')) void document.exitFullscreen();
});

async function connectWithTicket() {
  state.token = $('#stage-ticket').value.trim();
  if (!state.token) { $('#stage-gate-error').textContent = '请粘贴本地服务生成的一次性票据。'; return; }
  $('#stage-gate-error').textContent = '';
  openSocket(state.token);
}

function openSocket(token) {
  $('#stage-connect').disabled = true; $('#stage-reconnect').classList.add('hidden');
  const socket = new WebSocket(`ws://${location.host}/collab`);
  state.ws = socket; state.connected = false; state.synced = false; updateControls();
  socket.addEventListener('open', () => socket.send(JSON.stringify({ type: 'auth', ticket: token })));
  socket.addEventListener('message', (event) => {
    let message;
    try { message = JSON.parse(event.data); } catch { return; }
    handleMessage(message);
  });
  socket.addEventListener('close', () => {
    state.connected = false; state.synced = false;
    if (state.doc && (state.pending.size || state.formDirty)) preserveRecoveryDraft();
    if (state.pending.size) { for (const operation of state.pending.values()) rememberFailedOperation(operation); state.pending.clear(); state.localUnconfirmed = true; }
    $('#stage-online').textContent = '连接断开'; $('#stage-reconnect').classList.remove('hidden');
    if (!state.doc) { $('#stage-connect').disabled = false; $('#stage-gate-error').textContent = '连接失败或票据无效；请重新生成本地测试票据。'; }
    updateControls(); updateSaveState();
  });
  socket.addEventListener('error', () => { $('#stage-gate-error').textContent = '本地协作服务连接失败。'; $('#stage-connect').disabled = false; });
}

async function reconnect() {
  if (!state.actorId) return;
  $('#stage-reconnect').disabled = true;
  try {
    const response = await fetch('/__t0/test-tickets', { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ document_id: DOCUMENT_ID }) });
    if (!response.ok) throw new Error('ticket_issue_failed');
    const issued = await response.json();
    const ticket = issued.participants.find((item) => item.actor_id === state.actorId)?.token;
    if (!ticket) throw new Error('ticket_actor_missing');
    state.token = ticket; openSocket(ticket);
  } catch { flash('本地重连票据生成失败。'); }
  finally { $('#stage-reconnect').disabled = false; }
}

function handleMessage(message) {
  if (message.type === 'auth.ok') {
    state.actorId = message.actor_id; state.connected = true;
    $('#stage-online').textContent = '已连接'; updateControls(); sendPresence();
  } else if (message.type === 'sync') {
    if (state.doc && (state.pending.size || state.rejectedLocal || state.localUnconfirmed)) preserveRecoveryDraft();
    state.epoch = message.epoch; state.revision = message.revision; state.structure = message.structure; state.participants = message.participants ?? [];
    state.selectedStageId = state.structure.stages.some((stage) => stage.id === state.selectedStageId) ? state.selectedStageId : state.structure.stages[0].id;
    state.selectedRoleId = state.structure.roles.some((role) => role.id === state.selectedRoleId) ? state.selectedRoleId : state.structure.roles[0].id;
    resetDocument(message.state);
    state.synced = true;
    $('#stage-gate').classList.add('hidden'); $('#stage-workspace').classList.remove('hidden');
    render(); updateSaveState(); updateRevision();
    if (!state.localUnconfirmed && !state.rejectedLocal) state.saved = '已从隔离 PostgreSQL 恢复';
    updateControls();
  } else if (message.type === 'participants') {
    state.participants = message.participants ?? []; renderPresence();
  } else if (message.type === 'operation.applied') {
    const own = message.actor_id === state.actorId;
    const pendingOperation = state.pending.get(message.operation_id);
    state.revision = message.revision;
    if (message.operation_type === 'text.update') {
      const update = fromBase64(message.update);
      applyRemoteUpdate(update);
      applyConfirmedUpdate(state.confirmedDoc, update);
    }
    else {
      if (state.playback && !own) state.playbackChanged = true;
      const previousStructure = state.structure;
      if (message.operation_type === 'stage.command') {
        const update = fromBase64(message.state);
        applyStructuralState(message.state, message.structure);
        applyConfirmedUpdate(state.confirmedDoc, update);
      }
      else state.structure = message.structure;
      if (!state.playback) updateSelectedAfterCommand(pendingOperation?.payload?.command, message.structure, previousStructure);
      if (message.operation_type !== 'stage.command' || !state.deferredRender) render();
    }
    state.pending.delete(message.operation_id);
    state.confirmedState = encodeConfirmedDocument(state.confirmedDoc);
    removeFailedOperation(message.operation_id);
    state.rejectedLocal = state.failedOperations.length > 0;
    if (state.pending.size === 0 && !state.rejectedLocal && !state.recoveryDraft) state.localUnconfirmed = false;
    state.saved = state.rejectedLocal || state.localUnconfirmed ? '有未确认内容 · 可导出' : state.pending.size ? '正在保存…' : '已保存到隔离 PostgreSQL';
    updateRevision(); updateSaveState(); updateControls();
  } else if (message.type === 'operation.ack') {
    const pendingOperation = state.pending.get(message.operation_id);
    state.pending.delete(message.operation_id); state.revision = Math.max(state.revision, message.revision);
    if (message.state && state.doc && state.confirmedDoc) {
      const previousStructure = state.structure;
      const update = fromBase64(message.state);
      if (message.structure?.schema_kind === 'stage-board') {
        applyStructuralState(message.state, message.structure);
        updateSelectedAfterCommand(pendingOperation?.payload?.command, message.structure, previousStructure);
      } else applyRemoteUpdate(update);
      applyConfirmedUpdate(state.confirmedDoc, update);
      state.confirmedState = encodeConfirmedDocument(state.confirmedDoc);
    }
    removeFailedOperation(message.operation_id);
    state.rejectedLocal = state.failedOperations.length > 0;
    if (!state.pending.size && !state.rejectedLocal && !state.recoveryDraft) state.localUnconfirmed = false;
    state.saved = state.rejectedLocal || state.localUnconfirmed ? '有未确认内容 · 可导出' : '已保存到隔离 PostgreSQL';
    updateRevision(); updateSaveState(); updateControls();
  } else if (message.type === 'operation.rejected') {
    const operation = state.pending.get(message.operation_id);
    if (operation) rememberFailedOperation(operation);
    state.pending.delete(message.operation_id);
    preserveRecoveryDraft(); state.rejectedLocal = true; state.localUnconfirmed = true;
    state.saved = `未保存：${message.code} · 草稿可导出`; updateSaveState(); updateControls();
  } else if (message.type === 'protocol.error') {
    flash(`操作未完成：${message.code}`);
  }
}

function resetDocument(encoded) {
  disconnectTextObservers(); state.undoManager?.destroy(); state.undoManager = null; state.doc?.destroy(); state.composing.clear();
  const serverState = fromBase64(encoded);
  state.doc = new Y.Doc(); Y.applyUpdate(state.doc, serverState, 'server');
  state.confirmedDoc = replaceConfirmedDocument(state.confirmedDoc, serverState);
  state.confirmedState = encodeConfirmedDocument(state.confirmedDoc);
  const root = state.doc.getMap('fields');
  const texts = expectedStageTextFields(state.structure).map((key) => root.get(key)).filter((value) => value instanceof Y.Text);
  state.undoManager = bindLocalTextHistory(texts, updateUndoControls);
  state.doc.on('update', (update, origin) => {
    updateTextViews();
    if (!isPersistedTextUpdateOrigin(origin, state.undoManager)) return;
    if (!state.connected || state.mobileReadOnly || state.playback) { preserveRecoveryDraft(); state.localUnconfirmed = true; updateSaveState(); return; }
    const operationId = crypto.randomUUID();
    const operation = { type: 'text.update', operation_id: operationId, epoch: state.epoch, payload: { update: toBase64(update) } };
    state.pending.set(operationId, operation); state.localUnconfirmed = true; state.saved = '正在保存…'; updateSaveState();
    send({ type: 'operation', operation });
  });
  bindTextObservers();
}

function applyStructuralState(encoded, nextStructure) {
  const active = document.activeElement?.matches?.('[data-stage-text]') ? document.activeElement : null;
  const activeKey = active?.dataset.stageText;
  const nextKeys = new Set(expectedStageTextFields(nextStructure));
  const protectedKey = activeKey && nextKeys.has(activeKey) ? activeKey : [...state.composing.keys()].find((key) => nextKeys.has(key));
  if ((activeKey && !nextKeys.has(activeKey)) || [...state.composing.keys()].some((key) => !nextKeys.has(key))) preserveRecoveryDraft();
  const previousKeys = new Set(expectedStageTextFields(state.structure));
  Y.applyUpdate(state.doc, fromBase64(encoded), 'server');
  state.structure = nextStructure;
  disconnectTextObservers(); bindTextObservers();
  const addedTypes = [...nextKeys].filter((key) => !previousKeys.has(key)).map((key) => state.doc.getMap('fields').get(key)).filter((value) => value instanceof Y.Text);
  if (addedTypes.length) state.undoManager?.addToScope(addedTypes);
  if (protectedKey) {
    state.deferredRender = true;
    updateTextViews(); renderMap(); renderRecovery(); updateRevision(); updateControls();
    return;
  }
  render();
}

function flushDeferredRender() {
  if (!state.deferredRender || state.composing.size) return;
  if (document.activeElement?.matches?.('[data-stage-text]')) return;
  state.deferredRender = false; render();
}

function disconnectTextObservers() {
  for (const { text, observer } of state.fieldObservers.values()) text.unobserve(observer);
  state.fieldObservers.clear();
}

function bindTextObservers() {
  if (!state.doc) return;
  const root = state.doc.getMap('fields');
  for (const key of expectedStageTextFields(state.structure)) {
    const text = root.get(key);
    if (!(text instanceof Y.Text)) continue;
    const observer = () => updateTextViews(key);
    text.observe(observer); state.fieldObservers.set(key, { text, observer });
  }
}

function bindTextarea(textarea) {
  const key = textarea.dataset.stageText;
  const text = state.doc.getMap('fields').get(key);
  if (!(text instanceof Y.Text)) { textarea.disabled = true; return; }
  textarea.value = text.toString();
  textarea.addEventListener('compositionstart', () => state.composing.set(key, captureComposition(text)));
  textarea.addEventListener('compositionend', () => {
    const composition = state.composing.get(key); state.composing.delete(key);
    const edit = composition && applyCompositionEdit(state.doc, text, composition, textarea.value, textarea.selectionStart, textarea.selectionEnd);
    if (edit?.ok) {
      textarea.value = edit.value;
      const start = Y.createAbsolutePositionFromRelativePosition(edit.selection.start, state.doc);
      const end = Y.createAbsolutePositionFromRelativePosition(edit.selection.end, state.doc);
      if (start?.type === text && end?.type === text) textarea.setSelectionRange(start.index, end.index, textarea.selectionDirection);
    } else compositionConflict(key, textarea);
    updateTextViews(key); sendPresence(key, textarea.selectionStart); setTimeout(flushDeferredRender, 0);
  });
  textarea.addEventListener('input', () => {
    if (state.composing.has(key)) return;
    if ([...textarea.value].length > 2000) { flash('单个文字字段最多 2000 个 Unicode 字符。'); return; }
    applyTextDiff(state.doc, text, textarea.value); sendPresence(key, textarea.selectionStart);
  });
  textarea.addEventListener('focus', () => sendPresence(key, textarea.selectionStart));
  textarea.addEventListener('blur', flushDeferredRender);
  textarea.addEventListener('keyup', () => sendPresence(key, textarea.selectionStart));
  textarea.addEventListener('click', () => sendPresence(key, textarea.selectionStart));
}

function compositionConflict(key, textarea) {
  preserveRecoveryDraft();
  if (state.recoveryDraft) state.recoveryDraft.fields[key] = textarea.value;
  state.localUnconfirmed = true; state.rejectedLocal = true;
  const current = state.doc.getMap('fields').get(key).toString();
  textarea.value = current; textarea.setSelectionRange(current.length, current.length);
  flash('组合输入无法安全合并；本地副本已保留，可导出。'); updateSaveState();
}

function applyRemoteUpdate(update) {
  const active = document.activeElement?.matches?.('[data-stage-text]') ? document.activeElement : null;
  const key = active?.dataset.stageText;
  const text = key ? state.doc.getMap('fields').get(key) : null;
  const selection = text && !state.composing.has(key) ? {
    start: Y.createRelativePositionFromTypeIndex(text, active.selectionStart),
    end: Y.createRelativePositionFromTypeIndex(text, active.selectionEnd), direction: active.selectionDirection,
  } : null;
  Y.applyUpdate(state.doc, update, 'server');
  if (text && selection && !state.composing.has(key)) {
    active.value = text.toString();
    const start = Y.createAbsolutePositionFromRelativePosition(selection.start, state.doc);
    const end = Y.createAbsolutePositionFromRelativePosition(selection.end, state.doc);
    if (start?.type === text && end?.type === text) active.setSelectionRange(start.index, end.index, selection.direction);
  }
  if (state.playback) state.playbackChanged = true;
  updateTextViews(); renderStageLabels(); updatePlaybackChanged();
}

function updateTextViews(key = null) {
  if (!state.doc) return;
  const keys = key ? [key] : [...state.fieldObservers.keys()];
  for (const fieldKey of keys) {
    const text = state.doc.getMap('fields').get(fieldKey);
    if (!(text instanceof Y.Text)) continue;
    const value = text.toString();
    for (const textarea of $$(`[data-stage-text="${cssEscape(fieldKey)}"]`)) {
      if (document.activeElement !== textarea && !state.composing.has(fieldKey) && textarea.value !== value) textarea.value = value;
      const counter = textarea.parentElement?.querySelector('[data-character-count]');
      if (counter) counter.textContent = `${[...value].length}/2000`;
    }
    for (const node of $$(`[data-stage-display="${cssEscape(fieldKey)}"]`)) node.textContent = value || '（尚未填写）';
  }
  renderStageLabels();
}

function render() {
  if (!state.structure || !state.doc) return;
  const structure = visibleStructure();
  if (!structure.stages.some((stage) => stage.id === state.selectedStageId)) state.selectedStageId = structure.stages[0].id;
  if (!structure.roles.some((role) => role.id === state.selectedRoleId)) state.selectedRoleId = structure.roles[0].id;
  renderStageList(); renderRoleList(); renderStageEditor(); renderMap(); renderRecovery(); renderPresence(); renderToolbar(); renderPlaybackControls(); updateControls(); updateRevision(); updateUndoControls();
}

function visibleStructure() { return state.playback?.snapshot.structure ?? state.structure; }
function visibleFields() { return state.playback?.snapshot.fields ?? projectCurrentFields(); }
function currentStage() { return visibleStructure()?.stages.find((stage) => stage.id === state.selectedStageId) ?? visibleStructure()?.stages[0]; }
function fieldValue(key) { return visibleFields()?.[key] ?? ''; }

function projectCurrentFields() {
  if (!state.doc || !state.structure) return {};
  return Object.fromEntries(expectedStageTextFields(state.structure).map((key) => [key, state.doc.getMap('fields').get(key)?.toString() ?? '']));
}

function renderStageList() {
  const structure = visibleStructure();
  $('#stage-count').textContent = `${structure.stages.length}/20`;
  $('#stage-list').innerHTML = structure.stages.map((stage, index) => {
    const key = stageNameField(stage.id); const selected = stage.id === state.selectedStageId;
    const controls = state.playback ? '' : `<span class="stage-list-controls edit-only"><button type="button" data-stage-control="up" data-stage-id="${stage.id}" aria-label="上移阶段" ${index === 0 ? 'disabled' : ''}>↑</button><button type="button" data-stage-control="down" data-stage-id="${stage.id}" aria-label="下移阶段" ${index === structure.stages.length - 1 ? 'disabled' : ''}>↓</button></span>`;
    return `<div class="stage-list-item"><button class="stage-select ${selected ? 'active' : ''}" type="button" data-stage-select="${stage.id}"><span class="stage-number">${String(index + 1).padStart(2, '0')}</span><span data-stage-display="${key}">${escapeHtml(fieldValue(key) || `阶段 ${index + 1}`)}</span></button>${controls}</div>`;
  }).join('');
}

function renderRoleList() {
  const structure = visibleStructure();
  $('#role-list').innerHTML = structure.roles.map((role) => {
    const field = roleNameField(role.id); const selected = state.selectedRoleId === role.id;
    if (state.mobileReadOnly || state.playback) return `<div class="role-readonly"><i style="--role-color:${role.color}">${role.number}</i><span>${escapeHtml(fieldValue(field) || `角色 ${role.number}`)}</span></div>`;
    return `<div class="role-row"><button type="button" data-role-select="${role.id}" class="role-pick ${selected ? 'selected' : ''}" aria-label="选择角色 ${role.number}" title="选中后可在主地图点击或拖动角色标记设置站位"><i style="--role-color:${role.color}">${role.number}</i>选中 ${role.number}</button><label class="role-name-edit"><span>角色 ${role.number} 名称</span><textarea data-stage-text="${field}" rows="1" maxlength="4000" aria-label="编辑角色 ${role.number} 名称"></textarea><span class="character-count" data-character-count></span></label></div>`;
  }).join('');
  const form = $('#action-form');
  const select = form.elements.role_id;
  const previous = select.value;
  select.innerHTML = structure.roles.map((role) => `<option value="${role.id}">${role.number} · ${escapeHtml(fieldValue(roleNameField(role.id)) || `角色 ${role.number}`)}</option>`).join('');
  if (structure.roles.some((role) => role.id === previous)) select.value = previous;
  else if (structure.roles.some((role) => role.id === state.selectedRoleId)) select.value = state.selectedRoleId;
}

function renderStageEditor() {
  const stage = currentStage(); if (!stage) return;
  const index = visibleStructure().stages.indexOf(stage);
  $('#editor-stage-name').textContent = fieldValue(stageNameField(stage.id)) || `阶段 ${index + 1}`;
  $('#map-stage-name').textContent = fieldValue(stageNameField(stage.id)) || `阶段 ${index + 1}`;
  $('#map-stage-summary').textContent = fieldValue(stageConditionField(stage.id)) || fieldValue(stageDescriptionField(stage.id)) || '可在右侧填写本阶段说明与执行条件。';
  const readonly = state.mobileReadOnly || state.playback;
  const controls = readonly ? '' : `<div class="stage-control-row edit-only"><button class="button" data-stage-control="copy" data-stage-id="${stage.id}" type="button">复制</button><button class="button" data-stage-control="reinherit" data-stage-id="${stage.id}" type="button">重新继承站位</button><button class="button danger" data-stage-control="delete" data-stage-id="${stage.id}" type="button" ${visibleStructure().stages.length <= 1 ? 'disabled' : ''}>删除阶段</button></div>`;
  const metadata = readonly
    ? `<div class="readonly-copy"><strong>阶段名称</strong><p>${escapeHtml(fieldValue(stageNameField(stage.id)) || '（未命名阶段）')}</p><strong>整体说明</strong><p>${escapeHtml(fieldValue(stageDescriptionField(stage.id)) || '（尚未填写）')}</p><strong>执行条件</strong><p>${escapeHtml(fieldValue(stageConditionField(stage.id)) || '（尚未填写）')}</p></div>`
    : `<label class="text-field"><span>阶段名称</span>${textArea(stageNameField(stage.id), '阶段名称')}</label><label class="text-field"><span>整体说明</span>${textArea(stageDescriptionField(stage.id), '描述本阶段战术意图')}</label><label class="text-field"><span>执行条件</span>${textArea(stageConditionField(stage.id), '例如：队友确认后再开始')}</label>`;
  $('#stage-meta').innerHTML = `${controls}${metadata}`;
  $('#stage-text-fields').innerHTML = '';
  renderActions(stage);
  for (const textarea of $$('[data-stage-text]')) bindTextarea(textarea);
  $('#action-count').textContent = `${stage.actions.length}/100`;
}

function renderActions(stage) {
  const structure = visibleStructure();
  if (!stage.actions.length) { $('#action-list').innerHTML = '<p class="empty-state">还没有动作。选择类型、填写说明，并按地图提示添加路线或目标。</p>'; return; }
  const groups = groupStageActions(stage);
  const groupNumber = new Map(groups.map((group, index) => [group.id, index + 1]));
  $('#action-list').innerHTML = stage.actions.map((action, index) => {
    const descriptionKey = actionDescriptionField(action.id);
    const roleId = action.kind === 'utility' ? action.thrower_role_id : action.role_id;
    const role = structure.roles.find((item) => item.id === roleId);
    const label = action.kind === 'move' ? '移动' : action.kind === 'wait' ? '等待' : action.kind === 'observe' ? '观察方向' : action.kind === 'utility' ? utilityLabel(action.utility_type) : '文字提示';
    const active = state.playback?.snapshot.structure.stages[state.playback.stageIndex]?.id === stage.id && state.playback?.frame?.activeActionIds.includes(action.id);
    if (state.playback || state.mobileReadOnly) return `<article class="action-card ${active ? 'playing' : ''}" data-action-id="${action.id}"><div class="action-card-heading"><strong>第 ${groupNumber.get(action.group_id)} 组 · ${label}${role ? ` · 角色 ${role.number}` : ''}</strong></div><p class="action-copy">${escapeHtml(fieldValue(descriptionKey) || '（尚未填写）')}</p></article>`;
    const roleChange = action.kind === 'text' ? '' : `<label class="inline-control">执行者<select data-action-change="role" data-action-id="${action.id}">${structure.roles.map((item) => `<option value="${item.id}" ${item.id === roleId ? 'selected' : ''}>${item.number} · ${escapeHtml(fieldValue(roleNameField(item.id)) || `角色 ${item.number}`)}</option>`).join('')}</select></label>`;
    const groupOptions = groups.map((group, groupIndex) => `<option value="${groupIndex}" ${group.id === action.group_id ? 'selected' : ''}>第 ${groupIndex + 1} 组</option>`).join('') + `<option value="${groups.length}">新接续组</option>`;
    const detail = action.kind === 'move' ? `${action.path.length} 个路线点` : action.kind === 'utility' || action.kind === 'observe' || action.kind === 'text' ? '可在地图上重新选点' : '保持当前站位';
    return `<article class="action-card ${state.selectedActionId === action.id ? 'selected' : ''}" data-action-id="${action.id}"><div class="action-card-heading"><strong>第 ${groupNumber.get(action.group_id)} 组 · ${label}</strong><span class="small-note">${index + 1}. ${detail}</span></div><label class="text-field action-description"><span>动作说明</span>${textArea(descriptionKey, '填写动作说明')}</label><div class="action-controls edit-only">${roleChange}<label class="inline-control">编组<select data-action-change="group" data-action-id="${action.id}">${groupOptions}</select></label></div><div class="action-controls edit-only"><button class="button" data-action-control="up" data-action-id="${action.id}" type="button" ${index === 0 ? 'disabled' : ''} aria-label="动作前移">↑ 前移</button><button class="button" data-action-control="down" data-action-id="${action.id}" type="button" ${index === stage.actions.length - 1 ? 'disabled' : ''} aria-label="动作后移">↓ 后移</button><button class="button" data-action-control="redraw" data-action-id="${action.id}" type="button">${action.kind === 'move' ? '重画路线' : action.kind === 'wait' ? '无地图目标' : '重新选点'}</button><button class="button danger" data-action-control="delete" data-action-id="${action.id}" type="button">删除</button></div></article>`;
  }).join('');
}

function textArea(key, placeholder) {
  return `<textarea data-stage-text="${key}" maxlength="4000" rows="2" spellcheck="false" placeholder="${escapeAttr(placeholder)}"></textarea><span class="character-count" data-character-count></span>`;
}

function renderStageLabels() {
  if (!state.structure || !state.doc) return;
  for (const stage of visibleStructure().stages) {
    const key = stageNameField(stage.id);
    for (const node of $$(`[data-stage-display="${cssEscape(key)}"]`)) node.textContent = fieldValue(key) || '（未命名阶段）';
  }
  if (!state.playback) {
    const stage = currentStage();
    if (stage) {
      $('#editor-stage-name').textContent = fieldValue(stageNameField(stage.id)) || '阶段';
      $('#map-stage-name').textContent = fieldValue(stageNameField(stage.id)) || '阶段';
      $('#map-stage-summary').textContent = fieldValue(stageConditionField(stage.id)) || fieldValue(stageDescriptionField(stage.id)) || '可在右侧填写本阶段说明与执行条件。';
    }
  }
}

function renderMap() {
  const stage = currentStage(); if (!stage) return;
  const structure = visibleStructure();
  const zoomLayer = $('#map-zoom-layer');
  zoomLayer.style.transform = `scale(${state.view.zoom})`;
  $('#map-world').style.transform = `translate(${state.view.x}px, ${state.view.y}px)`;
  zoomLayer.style.setProperty('--inverse-zoom', String(1 / state.view.zoom));
  const activeActions = state.playback?.frame?.activeActionIds ?? [];
  const paths = [];
  const markers = [];
  const rolePositions = state.playback?.stageIndex === structure.stages.indexOf(stage) ? state.playback.frame?.positions ?? stage.initial_positions : stage.initial_positions;
  for (const action of stage.actions) {
    const active = activeActions.includes(action.id) ? ' active-route' : '';
    if (action.kind === 'move') paths.push(`<polyline class="action-route${active}" points="${action.path.map((point) => `${point.x},${point.y}`).join(' ')}"/>`);
    else if (action.kind === 'observe' || action.kind === 'utility') {
      const roleId = action.kind === 'utility' ? action.thrower_role_id : action.role_id;
      const start = positionBeforeAction(stage, action.id, roleId);
      const target = action.target;
      const end = action.kind === 'observe' ? displayedObserveEnd(stage, action, start, target) : target;
      paths.push(`<line class="action-route target-route${action.kind === 'observe' ? ' observe-ray' : ''}${active}" x1="${start.x}" y1="${start.y}" x2="${end.x}" y2="${end.y}"/>`);
      markers.push(pin(target, action.kind === 'utility' ? utilityIcon(action.utility_type) : '↗', action.kind, action.kind === 'utility' && activeActions.includes(action.id)));
    } else if (action.kind === 'text') markers.push(pin(action.position, 'T', 'text-pin'));
  }
  if (state.drawing?.mode === 'new-move' || state.drawing?.mode === 'edit-move') {
    const points = state.drawing.points;
    if (points.length) paths.push(`<polyline class="action-route draft-route" points="${points.map((point) => `${point.x},${point.y}`).join(' ')}"/>`);
    for (const point of points.slice(1)) markers.push(pin(point, '·', 'draft-point'));
  }
  $('#map-svg').innerHTML = `<defs><marker id="observe-arrow" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="5" markerHeight="5" orient="auto"><path d="M0 0L8 4L0 8Z" fill="#f5db85"/></marker></defs>${paths.join('')}`;
  $('#map-markers').innerHTML = `${markers.join('')}${structure.roles.map((role) => {
    const pos = state.positionPreview?.roleId === role.id ? state.positionPreview.point : rolePositions[role.id] ?? stage.initial_positions[role.id];
    const name = fieldValue(roleNameField(role.id)) || `角色 ${role.number}`;
    const selected = role.id === state.selectedRoleId;
    return `<div class="role-anchor" style="left:${pos.x}%;top:${pos.y}%"><button class="role-marker ${selected ? 'selected' : ''}" data-role-marker="${role.id}" type="button" style="--role-color:${role.color}" aria-label="${escapeAttr(name)}，选择角色 ${role.number}"><span>${role.number}</span><small>${escapeHtml(name)}</small></button></div>`;
  }).join('')}`;
  $('#map-viewport').classList.toggle('pan-tool', state.view.pan);
  renderDrawingControls(); renderMapHint();
}

function pin(point, label, kind, active = false) { return `<span class="map-pin ${kind}${active ? ' active' : ''}" style="left:${point.x}%;top:${point.y}%" aria-hidden="true">${escapeHtml(label)}</span>`; }

function displayedObserveEnd(stage, action, start, target) {
  if (!state.playback?.frame?.activeActionIds.includes(action.id)) return target;
  const prior = stage.actions.slice(0, stage.actions.indexOf(action)).filter((item) => item.kind === 'observe' && item.role_id === action.role_id).at(-1);
  const originAngle = prior ? Math.atan2(prior.target.y - positionBeforeAction(stage, prior.id, action.role_id).y, prior.target.x - positionBeforeAction(stage, prior.id, action.role_id).x) : 0;
  const targetAngle = Math.atan2(target.y - start.y, target.x - start.x);
  const delta = ((targetAngle - originAngle + Math.PI * 3) % (Math.PI * 2)) - Math.PI;
  const angle = originAngle + delta * (state.playback.frame.progress ?? 1);
  const length = Math.hypot(target.x - start.x, target.y - start.y);
  return { x: start.x + Math.cos(angle) * length, y: start.y + Math.sin(angle) * length };
}

function positionBeforeAction(stage, actionId, roleId) {
  const positions = structuredClone(stage.initial_positions);
  for (const action of stage.actions) {
    if (action.id === actionId) return positions[roleId] ?? { x: 50, y: 50 };
    if (action.kind === 'move') positions[action.role_id] = structuredClone(action.path.at(-1));
  }
  return positions[roleId] ?? { x: 50, y: 50 };
}

function renderDrawingControls() {
  const visible = Boolean(state.drawing);
  const isPath = visible && state.drawing.mode.endsWith('move');
  $('#finish-drawing').classList.toggle('hidden', !isPath);
  $('#undo-route-point').classList.toggle('hidden', !isPath || state.drawing.points.length <= 1);
  $('#cancel-drawing').classList.toggle('hidden', !visible);
  $('#finish-drawing').disabled = isPath && state.drawing.points.length < 2;
}

function renderMapHint() {
  let hint = state.mobileReadOnly ? '手机端可查看已保存的阶段路线与动作说明；编辑功能仅在电脑端开放。' : '选择角色后设置初始站位；添加动作时按提示在地图上选路线或目标。';
  if (!state.mobileReadOnly && state.view.pan) hint = '按住地图空白处拖动平移；可用缩放滑杆调整视图。';
  else if (!state.mobileReadOnly && state.drawing?.mode.includes('move')) hint = '点击地图添加路线折点（起点由当前组自动衔接），完成后确认。';
  else if (!state.mobileReadOnly && state.drawing) hint = '点击地图选择目标位置。';
  else if (!state.mobileReadOnly && state.tool === 'position') hint = `点击地图设置角色 ${roleNumber(state.selectedRoleId)} 的本阶段初始站位。`;
  else if (!state.mobileReadOnly && state.playback) hint = state.playback.done ? '阶段演示完成；可重播、切换阶段或退出预览。' : state.playback.paused ? '演示已暂停；可继续播放、重播或切换阶段。' : '正在按默认演示节奏播放，不表示实战耗时。';
  $('#map-hint').textContent = hint;
}

function renderToolbar() {
  $('#tool-select').classList.toggle('active', state.tool === 'select');
  $('#tool-position').classList.toggle('active', state.tool === 'position');
  $('#tool-pan').classList.toggle('active', state.view.pan);
}

function renderPresence() {
  if (!state.structure) return;
  $('#stage-online').textContent = state.connected ? `${state.participants.length || 1} 位参与者在线` : '连接断开';
  $('#presence').textContent = state.participants.map((person) => person.id === state.actorId ? '你' : person.name).join(' · ') || (state.connected ? '已连接' : '');
}

function renderRecovery() {
  const entries = visibleStructure().recovery ?? [];
  $('#recovery-count').textContent = `${entries.length} 份`;
  $('#recovery-list').innerHTML = entries.length ? entries.map((item) => {
    const name = item.kind === 'stage' ? '已删除阶段副本' : item.kind === 'action' ? '已删除动作副本' : '已删除角色副本';
    const stageId = item.kind === 'stage' ? '' : item.snapshot.stage_id;
    return `<div class="recovery-item"><span>${name}</span>${item.kind === 'role' || state.playback ? '<small>此类恢复尚未开放</small>' : `<button class="button" type="button" data-recovery-restore="${item.id}" data-stage-id="${stageId}">恢复为副本</button>`}</div>`;
  }).join('') : '<p class="empty-state">删除阶段或动作后，可在这里恢复副本。</p>';
  renderRecoveryBannerOnly();
}

function renderPlaybackControls() {
  for (const node of $$('[data-playback-start]')) node.classList.toggle('hidden', Boolean(state.playback));
  for (const node of $$('[data-playback-only]')) node.classList.toggle('hidden', !state.playback);
  $('#playback-changed').classList.toggle('hidden', !state.playbackChanged);
  $('#playback-summary').classList.toggle('hidden', !state.playback?.done);
  if (!state.playback) return;
  const stages = state.playback.snapshot.structure.stages;
  const stage = stages[state.playback.stageIndex];
  const frame = stagePlaybackFrame(stage, state.playback.elapsed);
  state.playback.frame = frame;
  state.playback.done = state.playback.done || frame.complete;
  $('#play-pause').disabled = state.playback.done;
  $('#play-pause').textContent = state.playback.paused ? '继续' : '暂停';
  $('#play-prev').disabled = state.playback.stageIndex === 0;
  $('#play-next').disabled = state.playback.stageIndex >= stages.length - 1;
  $('#playback-summary').textContent = state.playback.done ? `${fieldValue(stageNameField(stage.id))}：${fieldValue(stageDescriptionField(stage.id)) || '阶段动作已完成。'}` : '';
}

function updateRevision() { $('#revision-badge').textContent = `rev ${state.revision}`; $('#stage-revision').textContent = `revision ${state.revision}`; }
function updateSaveState() {
  const node = $('#save-state'); if (!node) return;
  const unresolved = state.pending.size || state.localUnconfirmed || state.rejectedLocal || state.recoveryDraft || state.failedOperations.length || state.formDirty;
  node.textContent = state.playback ? '预览快照' : unresolved ? state.saved.includes('正在') ? state.saved : '有未确认内容 · 可导出' : state.saved;
  node.classList.toggle('failed', Boolean(state.rejectedLocal || state.recoveryDraft || state.failedOperations.length));
  node.classList.toggle('saving', Boolean(state.pending.size));
  renderRecoveryBannerOnly();
}
function renderRecoveryBannerOnly() {
  const banner = $('#recovery-banner'); if (!banner) return;
  const unresolved = Boolean(state.recoveryDraft || state.failedOperations.length || state.localUnconfirmed || state.rejectedLocal);
  banner.classList.toggle('hidden', !unresolved);
  const message = banner.querySelector(':scope > span');
  if (message) message.textContent = state.failedOperations.length
    ? `${state.failedOperations.length} 项操作未确认。可安全重试最新失败项，或先导出并确认重载服务器版本。`
    : '有未确认草稿。导出不会标记为已保存；确认重载前请先保留副本。';
  $('#retry-failed-operation')?.classList.toggle('hidden', !state.failedOperations.length);
  $('#discard-local-draft')?.classList.toggle('hidden', !unresolved);
}

function updateControls() {
  const canEdit = state.connected && !state.playback && !state.mobileReadOnly;
  for (const node of $$('.edit-only')) node.classList.toggle('disabled-edit', !canEdit);
  for (const textarea of $$('[data-stage-text]')) textarea.disabled = !canEdit;
  for (const node of $$('#action-form button, #action-form select, #action-form textarea')) node.disabled = !canEdit;
  $('#stage-add').disabled = !canEdit || state.structure?.stages.length >= 20 || state.pending.size > 0;
  $('#role-add').disabled = !canEdit || state.structure?.roles.length >= 5 || state.pending.size > 0;
  $('#undo-text').disabled = !canEdit || !state.undoManager?.undoStack.length;
  $('#redo-text').disabled = !canEdit || !state.undoManager?.redoStack.length;
  const canResolveDraft = state.connected && state.synced && !state.mobileReadOnly && state.pending.size === 0;
  $('#retry-failed-operation').disabled = !canResolveDraft || !state.failedOperations.length;
  $('#discard-local-draft').disabled = !canResolveDraft || !hasUnconfirmedWork();
  $('#mobile-readonly').classList.toggle('hidden', !state.mobileReadOnly);
  $('#stage-reconnect').classList.toggle('hidden', state.connected || !state.doc);
  $('#play-auto').disabled = !state.structure || !state.connected || hasUnconfirmedWork() || Boolean(state.playback);
  $('#play-stage').disabled = !state.structure || !state.connected || hasUnconfirmedWork() || Boolean(state.playback);
  $('#toggle-sidebar').textContent = $('#stage-workspace').classList.contains('sidebar-collapsed') ? '展开侧栏' : '折叠侧栏';
  updateSaveState();
}

function bindTextHistoryButtons() {
  $('#undo-text').disabled = !state.connected || !state.undoManager?.undoStack.length;
  $('#redo-text').disabled = !state.connected || !state.undoManager?.redoStack.length;
}

function updateUndoControls() { bindTextHistoryButtons(); }

function runTextHistory(action) {
  if (!state.connected || state.playback || state.mobileReadOnly || !state.undoManager) return;
  const active = document.activeElement?.matches?.('[data-stage-text]') ? document.activeElement : null;
  const text = active ? state.doc.getMap('fields').get(active.dataset.stageText) : null;
  const selection = text ? { start: Y.createRelativePositionFromTypeIndex(text, active.selectionStart), end: Y.createRelativePositionFromTypeIndex(text, active.selectionEnd), direction: active.selectionDirection } : null;
  const stack = action === 'undo' ? state.undoManager.undoStack : state.undoManager.redoStack;
  if (!stack.length) return;
  state.undoManager[action]();
  updateTextViews();
  if (active && selection) {
    const start = Y.createAbsolutePositionFromRelativePosition(selection.start, state.doc);
    const end = Y.createAbsolutePositionFromRelativePosition(selection.end, state.doc);
    if (start?.type === text && end?.type === text) active.setSelectionRange(start.index, end.index, selection.direction);
  }
}

function setTool(tool) { state.tool = tool; state.view.pan = tool === 'pan'; renderToolbar(); renderMapHint(); }

function onMapClick(event) {
  if (state.suppressMapClick) { state.suppressMapClick = false; return; }
  if (event.target.closest('[data-role-marker]')) return;
  const position = mapPoint(event);
  if (state.view.pan || state.playback) return;
  const intent = mapClickIntent(state.tool, state.drawing);
  if (intent === 'position') {
    if (!state.selectedRoleId) { flash('请先选择一个角色。'); return; }
    void submitCommand({ type: 'position.set', stage_id: currentStage().id, role_id: state.selectedRoleId, position }); return;
  }
  if (intent !== 'drawing') return;
  if (state.drawing.mode === 'edit-target') {
    const drawing = state.drawing;
    const action = currentStage().actions.find((item) => item.id === state.drawing.actionId);
    if (action) {
      const changes = action.kind === 'text' ? { position } : { target: position };
      const actionId = action.id; state.drawing = null;
      if (!submitCommand({ type: 'action.update', stage_id: currentStage().id, action_id: actionId, changes })) { state.drawing = drawing; preserveRecoveryDraft(); }
      renderMap();
    }
    return;
  }
  if (state.drawing.mode.endsWith('move')) {
    state.drawing.points.push(position); renderMap(); return;
  }
  state.draftTarget = position;
  state.drawing = null;
  $('#draft-target').textContent = `地图目标：${position.x.toFixed(1)}, ${position.y.toFixed(1)}`;
  renderMap(); renderDrawingControls();
  flash('目标已选定；检查说明后提交动作。');
}

function beginRolePlacement(event) {
  const button = event.target.closest('[data-role-marker]');
  if (!button || state.tool !== 'position' || !state.connected || state.playback || state.mobileReadOnly || event.button !== 0) return;
  event.preventDefault(); event.stopPropagation();
  const roleId = button.dataset.roleMarker;
  const move = (next) => { state.positionPreview = { roleId, point: mapPoint(next) }; renderMap(); };
  const cleanup = () => {
    window.removeEventListener('pointermove', move, true); window.removeEventListener('pointerup', finish, true); window.removeEventListener('pointercancel', cancel, true);
  };
  const finish = (next) => {
    cleanup();
    state.positionPreview = null; state.suppressMapClick = true;
    if (next?.type === 'pointerup') void submitCommand({ type: 'position.set', stage_id: currentStage().id, role_id: roleId, position: mapPoint(next) });
    renderMap(); setTimeout(() => { state.suppressMapClick = false; }, 0);
  };
  const cancel = () => finish(null);
  window.addEventListener('pointermove', move, true); window.addEventListener('pointerup', finish, true); window.addEventListener('pointercancel', cancel, true);
}

function beginPan(event) {
  if (!state.view.pan || event.button !== 0 || state.playback) return;
  event.preventDefault();
  const startX = event.clientX; const startY = event.clientY; const originX = state.view.x; const originY = state.view.y;
  state.suppressMapClick = true;
  const move = (next) => { state.view.x = originX + next.clientX - startX; state.view.y = originY + next.clientY - startY; renderMap(); };
  const end = () => { window.removeEventListener('pointermove', move, true); window.removeEventListener('pointerup', end, true); window.removeEventListener('pointercancel', end, true); setTimeout(() => { state.suppressMapClick = false; }, 0); };
  window.addEventListener('pointermove', move, true); window.addEventListener('pointerup', end, true); window.addEventListener('pointercancel', end, true);
}

function mapPoint(event) {
  return mapPointFromRect(event.clientX, event.clientY, $('#map-zoom-layer').getBoundingClientRect());
}

function resetMapView() { state.view.zoom = 1; state.view.x = 0; state.view.y = 0; $('#map-zoom').value = '1'; $('#zoom-value').textContent = '1.0×'; renderMap(); }

async function toggleFullscreen() {
  try {
    if (document.fullscreenElement) await document.exitFullscreen();
    else if ($('.map-panel').requestFullscreen) await $('.map-panel').requestFullscreen();
    else flash('此浏览器不支持地图全屏。');
  } catch { flash('无法进入地图全屏模式。'); }
}

function addActionFromForm(event) {
  event.preventDefault();
  const form = $('#action-form'); const kind = form.elements.kind.value; const description = form.elements.description.value;
  if (!description.trim()) { setActionError('每个动作都需要非空说明。'); return; }
  if ([...description].length > 2000) { setActionError('动作说明最多 2000 个 Unicode 字符。'); return; }
  const roleId = form.elements.role_id.value;
  const command = { type: 'action.add', stage_id: currentStage().id, kind, description, group_mode: form.elements.group_mode.value };
  if (kind === 'move') {
    const start = actionStartPosition(currentStage(), roleId, command.group_mode);
    state.drawing = { mode: 'new-move', roleId, points: [start], command };
    setTool('select'); setActionError(''); renderMap(); return;
  }
  if (kind === 'wait') command.role_id = roleId;
  else if (kind === 'observe') { command.role_id = roleId; command.target = state.draftTarget; }
  else if (kind === 'utility') { command.thrower_role_id = roleId; command.utility_type = form.elements.utility_type.value; command.target = state.draftTarget; }
  else { command.role_id = null; command.position = state.draftTarget; }
  if (kind !== 'wait' && !command.target && !command.position) {
    state.drawing = { mode: 'new-target', kind }; setTool('select'); renderMap(); setActionError('点击地图选择目标位置。'); return;
  }
  state.formDirty = false; void submitCommand(command);
}

function finishDrawing() {
  if (!state.drawing || !state.drawing.mode.endsWith('move') || state.drawing.points.length < 2) return;
  const drawing = state.drawing; state.drawing = null;
  let submitted;
  if (drawing.mode === 'new-move') { state.formDirty = false; submitted = submitCommand(moveCommandFromDrawing(drawing)); }
  if (drawing.mode === 'edit-move') submitted = submitCommand({ type: 'action.update', stage_id: currentStage().id, action_id: drawing.actionId, changes: { path: drawing.points } });
  if (!submitted) { state.drawing = drawing; preserveRecoveryDraft(); }
  renderMap();
}

function cancelDrawing() { state.drawing = null; state.draftTarget = null; $('#draft-target').textContent = ''; renderMap(); }

async function runStageControl(control, stageId) {
  if (state.playback) return;
  const structure = state.structure; const index = structure.stages.findIndex((stage) => stage.id === stageId); const stage = structure.stages[index];
  if (!stage) return;
  if (control === 'up' || control === 'down') return submitCommand({ type: 'stage.reorder', stage_id: stageId, to_index: index + (control === 'up' ? -1 : 1) });
  if (control === 'copy') return submitCommand({ type: 'stage.copy', stage_id: stageId });
  if (control === 'reinherit') {
    if (index === 0 || !confirm('重新继承将替换本阶段所有角色的初始站位，并重算本阶段移动起点。继续？')) return;
    return submitCommand({ type: 'stage.reinherit', stage_id: stageId });
  }
  if (control === 'delete') {
    if (!confirm('删除阶段会保留可恢复副本。确定删除？')) return;
    return submitCommand({ type: 'stage.delete', stage_id: stageId });
  }
}

async function runActionControl(control, actionId) {
  const stage = currentStage(); const action = stage.actions.find((item) => item.id === actionId); if (!action) return;
  if (control === 'up' || control === 'down') {
    const index = stage.actions.indexOf(action);
    return submitCommand({ type: 'action.reorder', stage_id: stage.id, action_id: actionId, to_index: index + (control === 'up' ? -1 : 1), group_mode: 'after' });
  }
  if (control === 'delete') {
    if (!confirm('删除动作会保留可恢复副本。确定删除？')) return;
    return submitCommand({ type: 'action.delete', stage_id: stage.id, action_id: actionId });
  }
  if (control === 'redraw' && action.kind === 'move') {
    state.drawing = { mode: 'edit-move', actionId, roleId: action.role_id, points: [actionStartForExisting(stage, action)] };
    setTool('select'); renderMap(); return;
  }
  if (control === 'redraw' && action.kind !== 'wait') {
    state.drawing = { mode: 'edit-target', actionId, kind: action.kind }; setTool('select'); renderMap(); return;
  }
  if (control === 'redraw') flash('等待动作无需地图目标。');
}

async function updateActionFromSelect(select) {
  const stage = currentStage(); const actionId = select.dataset.actionId; const action = stage.actions.find((item) => item.id === actionId); if (!action) return;
  if (select.dataset.actionChange === 'group') return submitCommand({ type: 'action.group', stage_id: stage.id, action_id: actionId, group_index: Number(select.value) });
  const changes = action.kind === 'utility' ? { thrower_role_id: select.value } : { role_id: select.value };
  return submitCommand({ type: 'action.update', stage_id: stage.id, action_id: actionId, changes });
}

async function restoreRecovery(recoveryId) {
  const item = state.structure.recovery.find((entry) => entry.id === recoveryId); if (!item || item.kind === 'role') return;
  if (item.kind === 'stage') return submitCommand({ type: 'recovery.restore', recovery_id: recoveryId, after_stage_id: state.selectedStageId });
  const select = $('#recovery-list').querySelector(`[data-recovery-restore="${cssEscape(recoveryId)}"]`);
  return submitCommand({ type: 'recovery.restore', recovery_id: recoveryId, stage_id: select.dataset.stageId || state.selectedStageId });
}

function submitCommand(command) {
  if (!state.connected || state.mobileReadOnly || state.playback) { flash('当前状态不能编辑；请连接后在电脑上操作。'); return false; }
  if (state.pending.size) { flash('上一项正在确认保存，请稍候再提交结构变更。'); return false; }
  const operationId = crypto.randomUUID();
  const operation = { type: 'stage.command', operation_id: operationId, epoch: state.epoch, payload: { expected_revision: state.revision, command } };
  state.pending.set(operationId, operation); state.lastCommand = command; state.saved = '正在保存结构变更…'; updateSaveState(); updateControls();
  if (!send({ type: 'operation', operation })) { state.pending.delete(operationId); updateSaveState(); updateControls(); return false; }
  return true;
}

function updateSelectedAfterCommand(command, structure, previous = state.structure) {
  if (!command) return;
  if (command.type === 'stage.add' || command.type === 'stage.copy' || (command.type === 'recovery.restore' && structure.stages.length > previous.stages.length)) {
    const added = structure.stages.find((stage) => !previous.stages.some((old) => old.id === stage.id));
    if (added) state.selectedStageId = added.id;
  }
  if (command.type === 'role.add') {
    const added = structure.roles.find((role) => !previous.roles.some((old) => old.id === role.id));
    if (added) state.selectedRoleId = added.id;
  }
  if (command.type === 'action.add') {
    const oldStage = previous.stages.find((stage) => stage.id === command.stage_id);
    const newStage = structure.stages.find((stage) => stage.id === command.stage_id);
    const added = newStage?.actions.find((action) => !oldStage?.actions.some((old) => old.id === action.id));
    if (added) state.selectedActionId = added.id;
  }
}

function actionStartPosition(stage, roleId, mode) {
  const positions = structuredClone(stage.initial_positions);
  const groups = groupStageActions(stage);
  for (let index = 0; index < groups.length; index += 1) {
    const group = groups[index];
    if (mode === 'parallel' && index === groups.length - 1) break;
    for (const action of group.actions) if (action.kind === 'move') positions[action.role_id] = structuredClone(action.path.at(-1));
  }
  return positions[roleId] ?? { x: 50, y: 50 };
}

function actionStartForExisting(stage, targetAction) {
  const group = targetAction.group_id;
  const position = structuredClone(stage.initial_positions[targetAction.role_id] ?? { x: 50, y: 50 });
  for (const action of stage.actions) {
    if (action.group_id === group) return position;
    if (action.kind === 'move' && action.role_id === targetAction.role_id) Object.assign(position, action.path.at(-1));
  }
  return position;
}

function stageRoleActionOptions(kind) {
  const utility = kind === 'utility';
  $('#action-role-label').classList.toggle('hidden', kind === 'text');
  $('#utility-type-label').classList.toggle('hidden', !utility);
}

$('#action-form').elements.kind.addEventListener('change', (event) => {
  stageRoleActionOptions(event.target.value);
  state.draftTarget = null; $('#draft-target').textContent = '';
});

function send(message) {
  if (state.ws?.readyState !== WebSocket.OPEN) { if (message.type === 'operation') preserveRecoveryDraft(); return false; }
  state.ws.send(JSON.stringify(message)); return true;
}

function sendPresence(field = null, cursor = null) {
  const stage = currentStage(); if (!stage || !state.connected) return;
  const payload = { step_id: stage.id };
  if (field) {
    payload.field = field; payload.cursor = Math.max(0, Math.min(2000, cursor ?? 0));
    const text = state.doc?.getMap('fields').get(field);
    if (text instanceof Y.Text) payload.relative_cursor = toBase64(Y.encodeRelativePosition(Y.createRelativePositionFromTypeIndex(text, payload.cursor)));
  }
  send({ type: 'presence', payload });
}

function exportRecovery() {
  const draft = state.recoveryDraft ?? captureDraft();
  const exportValue = { kind: 'smokeline-stage-board-recovery', document_id: DOCUMENT_ID, exported_at: new Date().toISOString(), confirmed_revision: state.revision, recovery_copy: draft, failed_operations: state.failedOperations };
  const blob = new Blob([JSON.stringify(exportValue, null, 2)], { type: 'application/json' });
  const link = document.createElement('a'); link.href = URL.createObjectURL(blob); link.download = 'smokeline-stage-recovery.json'; link.click(); URL.revokeObjectURL(link.href);
  flash('已导出恢复副本；它仍是未确认内容，不代表已保存。');
}

function captureDraft() {
  return {
    fields: projectCurrentFields(), structure: state.structure ? structuredClone(state.structure) : null,
    form: Object.fromEntries(new FormData($('#action-form')).entries()), selectedStageId: state.selectedStageId,
    mapDraft: state.drawing ? structuredClone(state.drawing) : null,
  };
}

function preserveRecoveryDraft() {
  state.recoveryDraft = { ...(state.recoveryDraft ?? {}), ...captureDraft() };
  state.localUnconfirmed = true; updateSaveState();
}

function rememberFailedOperation(operation) {
  if (!operation?.operation_id) return;
  const index = state.failedOperations.findIndex((item) => item.operation_id === operation.operation_id);
  if (index < 0) state.failedOperations.push(operation);
  else state.failedOperations[index] = operation;
}

function removeFailedOperation(operationId) {
  state.failedOperations = state.failedOperations.filter((item) => item.operation_id !== operationId);
}

function retryFailedOperation() {
  if (!state.connected || !state.synced || state.mobileReadOnly || state.pending.size) return;
  const operation = state.failedOperations.at(-1);
  if (!operation) return;
  if (operation.epoch !== state.epoch) { flash('文档版本已变化，不能安全重试该操作；请导出草稿后重新编辑。'); return; }
  state.pending.set(operation.operation_id, operation); state.localUnconfirmed = true; state.saved = '正在安全重试同一操作…';
  updateSaveState(); updateControls();
  if (!send({ type: 'operation', operation })) {
    state.pending.delete(operation.operation_id); rememberFailedOperation(operation); updateSaveState(); updateControls();
  }
}

function discardLocalDraft() {
  if (!state.connected || !state.synced || state.mobileReadOnly || state.pending.size || !state.confirmedState) return;
  if (!confirm('请先用“导出恢复副本”或其他方式保留未确认内容。确认后将丢弃本地未确认草稿，并重载服务器最近确认版本；服务器数据不会被删除。继续？')) return;
  state.recoveryDraft = null; state.failedOperations = []; state.rejectedLocal = false; state.localUnconfirmed = false;
  state.formDirty = false; state.drawing = null; state.positionPreview = null; state.draftTarget = null; state.deferredRender = false;
  state.saved = '已重载服务器确认版本'; resetDocument(toBase64(state.confirmedState)); render();
}

function hasUnconfirmedWork() {
  return state.pending.size > 0 || state.failedOperations.length > 0 || state.recoveryDraft !== null || state.localUnconfirmed || state.rejectedLocal || state.formDirty || state.composing.size > 0 || Boolean(state.drawing);
}

function projectRecoveryValue(key) { return state.recoveryDraft?.fields?.[key]; }

async function startPlayback(mode) {
  if (hasUnconfirmedWork() || state.composing.size) { flash('请先完成或导出未确认编辑，再开始预览。'); return; }
  const snapshot = { structure: structuredClone(state.structure), fields: projectCurrentFields() };
  const stageIndex = Math.max(0, snapshot.structure.stages.findIndex((stage) => stage.id === state.selectedStageId));
  state.playbackChanged = false;
  state.playback = { mode, snapshot, stageIndex, startedAt: performance.now(), elapsed: 0, paused: false, done: false, frame: null };
  $('#playback-summary').classList.add('hidden'); render(); startAnimation();
}

function startAnimation() {
  if (!state.playback || state.playback.paused) return;
  if (state.animationFrame !== null) cancelAnimationFrame(state.animationFrame);
  state.animationFrame = requestAnimationFrame(playbackTick);
}

function playbackTick(now) {
  state.animationFrame = null; const play = state.playback; if (!play || play.paused) return;
  const stage = play.snapshot.structure.stages[play.stageIndex];
  const elapsed = play.elapsed + now - play.startedAt;
  const duration = groupStageActions(stage).length * 3_000;
  if (elapsed < duration) {
    play.frame = stagePlaybackFrame(stage, elapsed); renderMap(); renderActionHighlights(); state.animationFrame = requestAnimationFrame(playbackTick); return;
  }
  play.elapsed = duration; play.frame = stagePlaybackFrame(stage, duration); play.done = true;
  renderMap(); renderActionHighlights(); renderPlaybackControls();
  if (play.mode === 'auto' && play.stageIndex < play.snapshot.structure.stages.length - 1) {
    if (!play.waiting) { play.waiting = true; play.waitStartedAt = now; }
    if (now - play.waitStartedAt < 1_000) { state.animationFrame = requestAnimationFrame(playbackTick); return; }
    play.waiting = false; play.stageIndex += 1; play.elapsed = 0; play.done = false; play.startedAt = now; state.selectedStageId = play.snapshot.structure.stages[play.stageIndex].id; play.frame = stagePlaybackFrame(play.snapshot.structure.stages[play.stageIndex], 0); render(); state.animationFrame = requestAnimationFrame(playbackTick); return;
  }
  renderPlaybackControls(); updateSaveState();
}

function togglePlaybackPause() {
  const play = state.playback; if (!play || play.done) return;
  if (play.paused) { play.paused = false; play.startedAt = performance.now(); startAnimation(); }
  else pausePlayback();
  renderPlaybackControls();
}

function pausePlayback() {
  const play = state.playback; if (!play || play.paused) return;
  play.elapsed += performance.now() - play.startedAt; play.paused = true;
  if (state.animationFrame !== null) cancelAnimationFrame(state.animationFrame);
  state.animationFrame = null; play.frame = stagePlaybackFrame(play.snapshot.structure.stages[play.stageIndex], play.elapsed);
  renderMap(); renderPlaybackControls();
}

function replayStage() {
  const play = state.playback; if (!play) return;
  play.elapsed = 0; play.startedAt = performance.now(); play.paused = false; play.done = false; play.waiting = false;
  play.frame = stagePlaybackFrame(play.snapshot.structure.stages[play.stageIndex], 0); startAnimation(); renderMap(); renderPlaybackControls();
}

function navigatePlayback(direction) {
  const play = state.playback; if (!play) return;
  const next = Math.max(0, Math.min(play.snapshot.structure.stages.length - 1, play.stageIndex + direction));
  if (next === play.stageIndex) return;
  play.stageIndex = next; play.elapsed = 0; play.startedAt = performance.now(); play.paused = false; play.done = false; play.waiting = false;
  state.selectedStageId = play.snapshot.structure.stages[next].id; play.frame = stagePlaybackFrame(play.snapshot.structure.stages[next], 0); startAnimation(); render();
}

function exitPlayback() {
  if (state.animationFrame !== null) cancelAnimationFrame(state.animationFrame);
  state.animationFrame = null; state.playback = null; state.playbackChanged = false; render();
}

function renderActionHighlights() {
  for (const card of $$('.action-card')) card.classList.toggle('playing', state.playback?.frame?.activeActionIds.includes(card.dataset.actionId) ?? false);
}

function updatePlaybackChanged() { $('#playback-changed').classList.toggle('hidden', !state.playbackChanged); }

function roleNumber(id) { return visibleStructure()?.roles.find((role) => role.id === id)?.number ?? '—'; }

function utilityLabel(value) { return ({ smoke: '烟雾弹', flash: '闪光弹', molotov: '燃烧弹', he: '高爆手雷', decoy: '诱饵弹' })[value] ?? '道具'; }
function utilityIcon(value) { return ({ smoke: '烟', flash: '闪', molotov: '火', he: '雷', decoy: '诱' })[value] ?? '•'; }
function clamp(value) { return Math.max(0, Math.min(100, Number.isFinite(value) ? value : 0)); }
function cssEscape(value) { return globalThis.CSS?.escape ? CSS.escape(value) : value.replace(/[^a-zA-Z0-9_-]/g, '\\$&'); }
function escapeHtml(value) { return String(value).replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;').replaceAll('"', '&quot;').replaceAll("'", '&#39;'); }
function escapeAttr(value) { return escapeHtml(value); }
function toBase64(value) {
  const bytes = value instanceof Uint8Array ? value : new Uint8Array(value);
  let binary = ''; const chunk = 0x8000;
  for (let offset = 0; offset < bytes.length; offset += chunk) binary += String.fromCharCode(...bytes.subarray(offset, offset + chunk));
  return btoa(binary);
}
function fromBase64(value) { return Uint8Array.from(atob(value), (character) => character.charCodeAt(0)); }
function flash(message) { $('#map-hint').textContent = message; }
function setActionError(message) { $('#action-error').textContent = message; }

stageRoleActionOptions($('#action-form').elements.kind.value);
updateControls();

