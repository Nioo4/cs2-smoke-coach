import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { runInNewContext } from 'node:vm';
import * as Y from 'yjs';

const source = readFileSync(new URL('../src/stage-board.js', import.meta.url), 'utf8');
function load(context, names) {
  for (const name of names) {
    const start = source.indexOf(`function ${name}(`);
    const rest = source.slice(start);
    const next = rest.slice(1).search(/\n(?:async )?function /);
    runInNewContext(rest.slice(0, next < 0 ? undefined : next + 1), context);
  }
}

test('remote presence shows editing location without following it and sync cannot move a locked preview', () => {
  const original = { stages: [{ id: 'first' }, { id: 'second' }], roles: [{ id: 'role' }] };
  const state = { actorId: 'a', connected: true, structure: original, selectedStageId: 'first', selectedRoleId: 'role',
    revision: 1, epoch: 'epoch', doc: {}, pending: new Map(), participants: [], localUnconfirmed: false, rejectedLocal: false, playback: null };
  const nodes = new Map();
  const context = { state, $: (key) => {
    if (!nodes.has(key)) nodes.set(key, { classList: { add() {}, remove() {} } });
    return nodes.get(key);
  }, expectedStageTextFields: () => ['stage.second.condition'], renderFieldPresence() {}, resetDocument() {}, render() {}, updateSaveState() {}, updateRevision() {}, updateControls() {} };
  load(context, ['renderPresence', 'handleMessage', 'visibleStructure', 'visibleFields', 'currentStage']);
  context.handleMessage({ type: 'participants', participants: [{ id: 'b', name: '参与者 B', step_id: 'second', field: 'stage.second.condition' }] });
  assert.equal(nodes.get('#presence').textContent, '参与者 B：第 2 阶段 · 正在编辑执行条件');
  assert.equal(state.selectedStageId, 'first');
  context.handleMessage({ type: 'participants', participants: [{ id: 'b', name: '参与者 B', step_id: 'deleted', field: 'arbitrary-secret.condition' }] });
  assert.equal(nodes.get('#presence').textContent, '参与者 B：尚未选择有效阶段 · 编辑位置已失效');
  state.playback = { snapshot: { structure: structuredClone(original), fields: { 'stage.first.name': '锁定标题' } }, stageIndex: 0 };
  context.handleMessage({ type: 'sync', revision: 2, epoch: 'epoch', state: 'encoded', structure: { stages: [{ id: 'second' }], roles: [{ id: 'new-role' }] } });
  assert.equal(state.selectedStageId, 'first'); assert.equal(state.selectedRoleId, 'role');
  assert.equal(context.currentStage().id, 'first'); assert.equal(context.visibleFields()['stage.first.name'], '锁定标题');
  assert.equal(state.playbackChanged, true);
  state.playback = null;
  context.handleMessage({ type: 'sync', revision: 2, epoch: 'epoch', state: 'encoded', structure: state.structure });
  assert.equal(state.selectedStageId, 'second'); assert.equal(state.selectedRoleId, 'new-role');
  state.connected = false; context.renderPresence();
  assert.equal(nodes.get('#presence').textContent, '离线，在线位置暂不可用');
  const doc = new Y.Doc(); const text = new Y.Text('条件'); doc.getMap('fields').set('stage.second.condition', text);
  state.doc = doc; state.connected = true;
  state.participants = [{ id: 'test-b', name: '<img onerror=unsafe>', step_id: 'second', field: 'stage.second.condition', relative_cursor: Buffer.from(Y.encodeRelativePosition(Y.createRelativePositionFromTypeIndex(text, 1))).toString('base64') }];
  const createElement = () => ({ dataset: {}, children: [], replaceChildren() { this.children = []; }, append(child) { this.children.push(child); } });
  let badges;
  const textarea = { dataset: { stageText: 'stage.second.condition' }, parentElement: { querySelector: () => badges }, insertAdjacentElement: (_where, element) => { badges = element; } };
  context.$$ = () => [textarea]; context.document = { createElement }; context.Y = Y; context.fromBase64 = (value) => Buffer.from(value, 'base64');
  load(context, ['renderFieldPresence']); context.renderFieldPresence();
  assert.equal(badges.children[0].textContent, '<img onerror=unsafe> · 正在编辑 · 光标 1');
  assert.equal(badges.children[0].className, 'field-presence-editor presence-b');
  state.connected = false; context.renderFieldPresence(); assert.equal(badges.children.length, 0);
  doc.destroy();
});

test('actual recovery rendering hides mobile actions and controls track disconnect, pending and recovery errors', () => {
  const structure = { stages: [{ id: 'current' }], roles: [{}], recovery: [{ id: 'stage-copy', kind: 'stage', snapshot: {} }, { id: 'action-copy', kind: 'action', snapshot: { stage_id: 'deleted' } }] };
  const state = { structure, mobileReadOnly: true, connected: true, playback: null, pending: new Map(), failedOperations: [], recoveryDraft: null, rejectedLocal: false, composing: new Map() };
  const nodes = new Map();
  const node = (key) => {
    if (!nodes.has(key)) nodes.set(key, { dataset: { stageText: 'field' }, classList: { toggle() {}, contains: () => false } });
    return nodes.get(key);
  };
  const button = node('restore'); const textarea = node('textarea');
  const context = { state, visibleStructure: () => structure, $: node,
    $$: (selector) => selector === '[data-recovery-restore]' ? [button] : selector === '[data-stage-text]' ? [textarea] : [],
    renderRecoveryBannerOnly() {}, hasUnconfirmedWork: () => false, updateSaveState() {},
  };
  load(context, ['recoveryBlocked', 'canRestoreRecovery', 'renderRecovery', 'updateControls']);
  context.renderRecovery();
  assert.ok(node('#recovery-list').innerHTML.includes('请在电脑上恢复副本'));
  assert.ok(!node('#recovery-list').innerHTML.includes('data-recovery-restore'));
  state.mobileReadOnly = false; context.renderRecovery(); context.updateControls();
  assert.ok(node('#recovery-list').innerHTML.includes('恢复到当前阶段为副本'));
  assert.equal(button.disabled, false);
  for (const mode of ['disconnected', 'pending', 'recovery', 'failed', 'rejected', 'playback']) {
    state.connected = mode !== 'disconnected'; state.pending.clear();
    state.recoveryDraft = mode === 'recovery' ? {} : null; state.rejectedLocal = mode === 'rejected';
    state.failedOperations = mode === 'failed' ? [{}] : []; state.playback = mode === 'playback' ? {} : null;
    if (mode === 'pending') state.pending.set('operation', {});
    context.renderRecovery(); context.updateControls();
    assert.equal(button.disabled, true, mode);
    if (mode === 'pending') assert.equal(textarea.disabled, false, 'ordinary pending must preserve continuous text input');
  }
  state.connected = true; state.playback = null; state.rejectedLocal = false; state.failedOperations = []; state.pending.clear();
  context.updateControls(); assert.equal(button.disabled, false);
});

test('normal pending text remains editable but recovery blocks text, commands, drawing and undo', () => {
  const doc = new Y.Doc(); const text = new Y.Text('已确认'); doc.getMap('fields').set('key', text);
  const state = { doc, connected: true, playback: null, mobileReadOnly: false, recoveryDraft: null, rejectedLocal: false, failedOperations: [], pending: new Map([['pending', {}]]), composing: new Map(), undoManager: {} };
  let edits = 0; const listeners = {};
  const textarea = { dataset: { stageText: 'key' }, value: '', addEventListener: (name, callback) => { listeners[name] = callback; } };
  const context = { state, Y, flash() {}, flushDeferredRender() {}, applyTextDiff: () => edits++, sendPresence() {} };
  load(context, ['recoveryBlocked', 'bindTextarea', 'submitCommand', 'onMapClick', 'runTextHistory', 'addActionFromForm', 'finishDrawing']);
  context.bindTextarea(textarea); textarea.value = '正常连续输入'; listeners.input(); assert.equal(edits, 1);
  state.recoveryDraft = { fields: {} }; textarea.value = '不应提交'; listeners.input();
  assert.equal(edits, 1); assert.equal(textarea.value, '已确认');
  assert.equal(context.submitCommand({}), false);
  context.onMapClick({}); context.runTextHistory('undo'); context.addActionFromForm({ preventDefault() {} }); context.finishDrawing();
  state.recoveryDraft = null; state.failedOperations = [{ operation_id: 'failed' }];
  assert.equal(context.recoveryBlocked(), true);
  state.recoveryDraft = { fields: { key: '未确认' } }; state.formDirty = false;
  state.pending.clear();
  load(context, ['removeFailedOperation']);
  context.removeFailedOperation('unrelated-ack'); assert.ok(state.recoveryDraft);
  context.removeFailedOperation('failed'); assert.equal(state.recoveryDraft, null);
  assert.equal(context.recoveryBlocked(), false);
  state.synced = true; state.confirmedState = new Uint8Array([1]); state.recoveryDraft = { fields: { key: '待放弃' } };
  context.confirm = () => false; context.toBase64 = () => ''; context.resetDocument = () => {}; context.render = () => {};
  load(context, ['discardLocalDraft']); context.discardLocalDraft(); assert.ok(state.recoveryDraft);
  context.confirm = () => true; context.discardLocalDraft(); assert.equal(state.recoveryDraft, null);
  doc.destroy();
});

test('composition tail is retained when a recovery sync protects a field that is about to disappear', () => {
  const state = { recoveryDraft: { fields: { deleted: '组合前' } }, composing: new Map([['deleted', {}]]) };
  const context = { state, $$: () => [{ dataset: { stageText: 'deleted' }, value: '中文组合尾字' }], updateSaveState() {} };
  load(context, ['preserveRecoveryDraft']); context.preserveRecoveryDraft();
  assert.equal(state.recoveryDraft.fields.deleted, '中文组合尾字');
  state.doc = new Y.Doc();
  context.preserveRecoveryDraft = () => {}; context.flash = () => {}; context.updateSaveState = () => {};
  load(context, ['compositionConflict']);
  const textarea = { value: '删除字段的尾字', setSelectionRange() {} };
  context.compositionConflict('deleted', textarea);
  assert.equal(state.recoveryDraft.fields.deleted, '删除字段的尾字');
  assert.equal(textarea.value, ''); state.doc.destroy();
});

test('action recovery uses the original live stage or explicitly selected surviving stage', async () => {
  const state = { selectedStageId: 'current', structure: { stages: [{ id: 'current' }], recovery: [{ id: 'copy', kind: 'action' }] } };
  let command;
  const context = { state, cssEscape: (value) => value, $: () => ({ querySelector: () => ({ dataset: { stageId: 'deleted-original' } }) }), submitCommand: (value) => { command = value; } };
  load(context, ['restoreRecovery']);
  await context.restoreRecovery('copy'); assert.equal(command.stage_id, 'current');
  state.structure.stages.push({ id: 'deleted-original' });
  await context.restoreRecovery('copy'); assert.equal(command.stage_id, 'deleted-original');
});

test('repeated authoritative sync captures cannot erase earlier unconfirmed recovery contents', () => {
  const source = readFileSync(new URL('../src/stage-board.js', import.meta.url), 'utf8');
  const state = { recoveryDraft: null, failedOperations: [], revision: 7, composing: new Map() };
  let current = { fields: { description: '未确认中文' }, structure: { stages: ['local'] }, mapDraft: { points: [1, 2] } };
  let exported;
  const context = {
    state, structuredClone, JSON, $$: () => [], DOCUMENT_ID: 'mirage-stage-prototype', captureDraft: () => structuredClone(current), updateSaveState() {}, flash() {},
    Blob: class { constructor(parts) { exported = JSON.parse(parts[0]); } },
    URL: { createObjectURL() { return 'blob:test'; }, revokeObjectURL() {} },
    document: { createElement() { return { click() {} }; } },
  };
  runInNewContext(source.slice(source.indexOf('function preserveRecoveryDraft()'), source.indexOf('function rememberFailedOperation(')), context);
  runInNewContext(source.slice(source.indexOf('function exportRecovery()'), source.indexOf('function captureDraft()')), context);
  context.preserveRecoveryDraft();
  current = { fields: { description: '服务器确认版本' }, structure: { stages: ['server'] }, mapDraft: null };
  context.preserveRecoveryDraft(); context.preserveRecoveryDraft();
  assert.equal(state.recoveryDraft.fields.description, '未确认中文');
  assert.equal(state.recoveryDraft.structure.stages[0], 'local');
  assert.deepEqual(state.recoveryDraft.mapDraft.points, [1, 2]);
  context.exportRecovery();
  assert.equal(exported.recovery_copy.fields.description, '未确认中文');
  assert.equal(state.localUnconfirmed, true, 'export must not claim persistence');
});
