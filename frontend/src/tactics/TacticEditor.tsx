import { useEffect, useId, useRef, useState, useSyncExternalStore, type FormEvent } from 'react';
import { Link } from 'react-router-dom';
import { TacticCollaborationClient } from './collaborationClient';
import TacticMap from './TacticMap';
import TacticTextField from './TacticTextField';
import TacticPlaybackPanel from './TacticPlaybackPanel';
import type { Point, TacticAction } from './editorTypes';
import type { Session } from './api';
import { actionDraftStart } from './actionGeometry';
import TacticNavigationGuard from './TacticNavigationGuard';
import StageTeachingReferences from './StageTeachingReferences';
import CopyTactic, { SourceAttribution } from './CopyTactic';
import TacticPublicationTools from './TacticPublicationTools';
import './workshop-editor.css';
import WorkshopDialog from './WorkshopDialog';
import { parallelRoleConflict } from './actionPreflight';
import TacticOrigin from './TacticOrigin';

function EditorLoading({ message, retry }: { message: string; retry?: () => void }) {
  return <section className="tactic-editor-loading" aria-busy="true" ref={node => { if (node) node.style.setProperty('--editor-offset', `${node.getBoundingClientRect().top}px`); }}><header><p role="status">{message}</p>{retry && <button className="button" onClick={retry}>重新连接</button>}</header><div className="editor-loading-map" aria-label="地图加载占位">正在准备地图与编辑区域…</div></section>;
}

export default function TacticEditor({ tacticId, session }: { tacticId: string; session: Session }) {
  const [ownedClient, setClient] = useState<{ tacticId: string; csrf: string; instance: TacticCollaborationClient } | null>(null);
  const client = ownedClient?.tacticId === tacticId && ownedClient.csrf === session.csrf_token ? ownedClient.instance : null;
  useEffect(() => {
    let disposed = false; let instance: TacticCollaborationClient | null = null;
    // Create after the effect subscribes, so React StrictMode's first cleanup cannot kill the live client.
    void Promise.resolve().then(() => {
      if (disposed) return;
      instance = new TacticCollaborationClient(tacticId, session.csrf_token); setClient({ tacticId, csrf: session.csrf_token, instance }); void instance.connect();
    });
    return () => { disposed = true; instance?.dispose(); };
  }, [tacticId, session.csrf_token]);
  return client ? <EditorBody key={`${tacticId}:${session.csrf_token}`} client={client} session={session} /> : <EditorLoading message="正在准备战术编辑器…" />;
}
interface ActionDraft { kind: TacticAction['kind']; role: string; utility: 'smoke' | 'flash' | 'molotov' | 'he' | 'decoy'; description: string; group: 'after' | 'parallel'; editing: string | null; points: Point[]; stageId: string | null }
const emptyDraft: ActionDraft = { kind: 'move', role: '', utility: 'smoke', description: '', group: 'after', editing: null, points: [], stageId: null };
function EditorBody({ client, session }: { client: TacticCollaborationClient; session: Session }) {
  const state = useSyncExternalStore(client.subscribe, client.snapshot);
  const [notebookId, setNotebookId] = useState<string | null>(null);
  const [contextRefresh, setContextRefresh] = useState(0);
  const [stageId, setStageId] = useState('');
  const [roleId, setRoleId] = useState('');
  const [filter, setFilter] = useState<string | null>(null);
  const [draft, setDraftState] = useState<ActionDraft>(emptyDraft);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [leaseError, setLeaseError] = useState('');
  const [confirmDelete, setConfirmDelete] = useState<string | null>(null);
  const [discardConfirm, setDiscardConfirm] = useState(false);
  const [confirmInherit, setConfirmInherit] = useState<string | null>(null);
  const [infoOpen, setInfoOpen] = useState(false);
  const infoTrigger = useRef<HTMLElement | null>(null);
  const editorRoot = useRef<HTMLElement>(null);
  const composing = useRef(false);
  const [context, setContext] = useState<'stage' | 'role' | 'action'>('stage');
  const [actionId, setActionId] = useState('');
  const [stagesCollapsed, setStagesCollapsed] = useState(() => matchMedia('(max-width: 820px)').matches);
  const [actionsCollapsed, setActionsCollapsed] = useState(() => matchMedia('(max-width: 1080px)').matches);
  const [menu, setMenu] = useState<'document' | 'status' | 'panel' | null>(null);
  const menuFocus = useRef<HTMLElement | null>(null);
  const panelSummary = useRef<HTMLElement | null>(null);
  const statusSummary = useRef<HTMLElement | null>(null);
  const stagePanel = useRef<HTMLElement>(null);
  const actionPanel = useRef<HTMLElement>(null);
  const sidebarId = useId();
  function closeMenu() { setMenu(null); menuFocus.current?.focus(); }
  function toggleMenu(value: 'document' | 'status' | 'panel', trigger: HTMLElement) {
    menuFocus.current = trigger; setMenu(current => current === value ? null : value);
  }
  function togglePanel(kind: 'stage' | 'action') {
    const closing = kind === 'stage' ? !stagesCollapsed : !actionsCollapsed;
    if (kind === 'stage') { setStagesCollapsed(closing); if (!closing && matchMedia('(max-width: 820px)').matches) setActionsCollapsed(true); }
    else { setActionsCollapsed(closing); if (!closing && matchMedia('(max-width: 820px)').matches) setStagesCollapsed(true); }
    setMenu(null);
    queueMicrotask(() => {
      if (closing) panelSummary.current?.focus();
      else (kind === 'stage' ? stagePanel : actionPanel).current?.querySelector<HTMLElement>('button,textarea,select')?.focus();
    });
  }
  const mapSelection = useRef<string | undefined>(undefined);
  const [restoreTarget, setRestoreTarget] = useState('');
  const [mobile] = useState(() => matchMedia('(max-width: 700px) and (pointer: coarse) and (hover: none)').matches && !matchMedia('(any-pointer: fine)').matches);
  const [showPreview, setShowPreview] = useState(false);
  const [mapReady, setMapReady] = useState(false);
  const projection = state.projection;
  const loaded = Boolean(projection);
  const stage = projection?.structure.stages.find(item => item.id === stageId) ?? projection?.structure.stages[0];
  const selectedRole = roleId ? projection?.structure.roles.find(role => role.id === roleId)?.id ?? '' : projection?.structure.roles[0]?.id ?? '';
  const missingRole = Boolean(roleId && !selectedRole);
  const missingAction = Boolean(actionId && stage && !stage.actions.some(action => action.id === actionId));
  const draftDirty = Boolean(draft.editing || draft.points.length || draft.description);
  const visibleStageId = stage?.id;
  const statusReason = error || leaseError || state.error || (state.recovery ? 'recovery' : '') || (draft.stageId && draft.stageId !== stage?.id ? 'missing-stage' : '');
  useEffect(() => {
    let cancelled = false;
    if (statusReason) queueMicrotask(() => { if (!cancelled) { menuFocus.current = statusSummary.current; setMenu('status'); } });
    return () => { cancelled = true; };
  }, [statusReason]);
  useEffect(() => {
    const clear = () => { mapSelection.current = undefined; if (visibleStageId) client.presence(visibleStageId); };
    clear(); window.addEventListener('blur', clear);
    return () => window.removeEventListener('blur', clear);
  }, [client, visibleStageId, showPreview, state.connectionId]);
  function setDraft(value: ActionDraft | ((current: ActionDraft) => ActionDraft)) {
    setDraftState(current => {
      const next = typeof value === 'function' ? value(current) : value;
      return { ...next, stageId: next.editing || next.points.length || next.description ? next.stageId ?? stage?.id ?? null : null };
    });
  }
  useEffect(() => { client.setLocalDraft(draftDirty ? { stage_id: draft.stageId, action_form: draft } : null); }, [client, draft, draftDirty]);
  useEffect(() => {
    const measure = () => {
      if (editorRoot.current) editorRoot.current.style.setProperty('--editor-offset', `${editorRoot.current.getBoundingClientRect().top}px`);
      if (editorRoot.current) editorRoot.current.style.setProperty('--editor-menu-top', `${editorRoot.current.querySelector('header')?.getBoundingClientRect().bottom ?? 126}px`);
      if (matchMedia('(max-width: 820px)').matches) {
        if (stagePanel.current?.contains(document.activeElement)) panelSummary.current?.focus();
        setStagesCollapsed(true);
      }
    };
    measure(); window.addEventListener('resize', measure);
    return () => window.removeEventListener('resize', measure);
  }, [loaded]);
  useEffect(() => {
    const warn = (event: BeforeUnloadEvent) => { if (client.hasUnconfirmedWork()) event.preventDefault(); };
    const navigation = (event: Event) => {
      const target = event.target as Element;
      if (client.hasUnconfirmedWork() && (target.closest('a') || (event.type === 'submit' && !target.closest('.tactic-editor')))) {
        event.preventDefault(); event.stopPropagation(); setError('有未确认内容，请先保存、导出或明确放弃后再离开。');
      }
    };
    window.addEventListener('beforeunload', warn); document.addEventListener('click', navigation, true); document.addEventListener('submit', navigation, true);
    return () => { window.removeEventListener('beforeunload', warn); document.removeEventListener('click', navigation, true); document.removeEventListener('submit', navigation, true); };
  }, [client]);
  const writable = state.connected && !state.readOnly && !state.recovery && !mobile && !showPreview;
  const blocked = !writable || busy || state.pending > 0;
  async function run(command: Record<string, unknown>, object?: { id: string; version: number }) {
    const targetStage = projection?.structure.stages.find(item => item.id === command.stage_id);
    const conflict = targetStage && parallelRoleConflict(targetStage, command);
    if (conflict) {
      setError(`“${projection?.fields[`role.${conflict}.name`] || '该角色'}”在同一并行编组已有动作。请改为接着执行、选择其他角色或调整编组；当前表单仍保留。`);
      return false;
    }
    setError(''); setBusy(true);
    try { await client.command(command, object); return true; }
    catch (err) { setError(err instanceof Error ? err.message : '操作未完成。'); return false; }
    finally { setBusy(false); }
  }
  async function undoObject() {
    setError(''); setBusy(true);
    try { await client.undoObject(); }
    catch (err) { setError(err instanceof Error ? err.message : '对象撤销未完成。'); }
    finally { setBusy(false); }
  }
  function cancelDrawing() {
    if (draft.editing) client.release(`action:${draft.editing}`);
    setLeaseError(''); client.clearLeaseError();
    setDraft(emptyDraft); client.setLocalDraft(null);
    mapSelection.current = undefined; if (stage) client.presence(stage.id);
  }
  function allowSelection() {
    if (!draftDirty) return true;
    setError('请先提交或取消当前动作绘制，再切换选择。'); setContext('action'); setActionsCollapsed(false);
    if (matchMedia('(max-width: 820px)').matches) setStagesCollapsed(true);
    return false;
  }
  function chooseStage(id: string) {
    if (!allowSelection()) return;
    setContext('stage'); setActionsCollapsed(false); if (matchMedia('(max-width: 820px)').matches) setStagesCollapsed(true); mapSelection.current = undefined; setStageId(id); client.presence(id);
  }
  async function editAction(action: TacticAction) {
    if (!stage || draftDirty) { setError('请先处理当前动作表单。'); return; }
    mapSelection.current = action.id; client.presence(stage.id, undefined, undefined, { selectedObjectId: action.id });
    try {
      await client.acquire(`action:${action.id}`, action.version);
      setLeaseError('');
      setDraft({ kind: action.kind, role: action.kind === 'utility' ? action.thrower_role_id : action.role_id ?? '', utility: action.kind === 'utility' ? action.utility_type : 'smoke', description: '', group: 'after', editing: action.id, stageId: stage.id, points: action.kind === 'move' ? action.path : action.kind === 'observe' || action.kind === 'utility' ? [action.target] : action.kind === 'text' ? [action.position] : [] });
    } catch (err) { setLeaseError(err instanceof Error ? err.message : '无法取得编辑权。'); }
  }
  async function submitAction(event: FormEvent) {
    event.preventDefault(); return saveAction();
  }
  async function saveAction() {
    if (!stage || blocked) return;
    if (draft.kind !== 'wait' && !mapReady) { setError('地图尚未加载完成，请等待或重试；当前绘制仍保留。'); return; }
    if (!draft.editing && !draft.description.trim()) { setError('请先退出全屏，在动作属性填写说明后再提交；当前绘制仍保留。'); return; }
    if (draft.stageId && draft.stageId !== stage.id) { setError('原动作所属阶段已被删除；请导出或取消该草稿，不能提交到其他阶段。'); return; }
    const role = draft.role || selectedRole;
    if ((draft.kind !== 'text' || draft.role) && !projection?.structure.roles.some(item => item.id === role)) { setError('原执行角色已删除，请重新选择角色，草稿仍保留。'); return; }
    const kindFields = draft.kind === 'move' ? { role_id: role, path: draft.points }
      : draft.kind === 'wait' ? { role_id: role }
        : draft.kind === 'utility' ? { thrower_role_id: role, utility_type: draft.utility, target: draft.points[0] }
          : draft.kind === 'observe' ? { role_id: role, target: draft.points[0] }
            : { role_id: draft.role || null, position: draft.points[0] };
    if ((draft.kind === 'move' && draft.points.length < 2) || (['observe', 'utility', 'text'].includes(draft.kind) && !draft.points.length)) { setError('请先在地图上完成路线或目标。'); return; }
    const existing = stage.actions.find(action => action.id === draft.editing);
    if (draft.editing && !existing) { setError('原动作已删除，不能提交到其他动作；请导出或取消原草稿。'); return; }
    const success = draft.editing ? existing && await run({ type: 'action.update', stage_id: stage.id, action_id: existing.id, changes: kindFields }, { id: `action:${existing.id}`, version: existing.version })
      : await run({ type: 'action.add', stage_id: stage.id, kind: draft.kind, description: draft.description, group_mode: draft.group, ...kindFields });
    if (success) cancelDrawing();
  }
  function point(point: Point, tool: 'select' | 'position') {
    if (!stage || blocked) return;
    if (draft.stageId && draft.stageId !== stage.id) { setError('原阶段已删除，请先导出或取消原动作草稿。'); return; }
    if (tool === 'position' && !draftDirty) {
      const position = stage.initial_positions[selectedRole];
      if (!position) { setError('原角色已删除，请重新选择角色后设置站位。'); return; }
      void run({ type: 'position.set', stage_id: stage.id, role_id: selectedRole, position: point }, { id: `position:${stage.id}:${selectedRole}`, version: position.version });
      return;
    }
    const role = draft.role || selectedRole;
    if ((draft.kind !== 'text' || draft.role) && !projection?.structure.roles.some(item => item.id === role)) { setError('原角色已删除，请重新选择角色；原草稿仍保留。'); return; }
    if (draft.kind === 'move') {
      const start = actionDraftStart(stage, role, draft.group);
      setDraft(value => ({ ...value, role, points: value.points.length ? [...value.points, point].slice(0, 50) : [{ x: start.x, y: start.y }, point] }));
    } else if (draft.kind !== 'wait') setDraft(value => ({ ...value, points: [point] }));
  }
  function exportDraft() {
    const blob = new Blob([JSON.stringify(client.recoveryExport(), null, 2)], { type: 'application/json' });
    const url = URL.createObjectURL(blob); const link = document.createElement('a'); link.href = url; link.download = 'smokeline-unconfirmed-draft.json'; link.click(); URL.revokeObjectURL(url);
  }
  if (!projection || !stage) return <EditorLoading message={state.error || '正在连接并读取已保存战术…'} retry={() => void client.connect()} />;
  const stageIndex = projection.structure.stages.findIndex(item => item.id === stage.id);
  const activeFilter = projection.structure.roles.some(role => role.id === filter) ? filter : null;
  const deleteTargetValid = Boolean(confirmDelete && (stage.actions.some(action => action.id === confirmDelete) || projection.structure.stages.some(item => item.id === confirmDelete)));
  const text = (field: string, label: string, maxLength?: number) => <TacticTextField key={field} client={client} field={field} label={label} stageId={stage.id} disabled={!writable} maxLength={maxLength} />;
  return <section ref={editorRoot} className="tactic-editor" aria-label="正式动态战术编辑器" onCompositionStartCapture={() => { composing.current = true; }} onCompositionEndCapture={() => { composing.current = false; }} onKeyDownCapture={event => { if ((composing.current || event.nativeEvent.isComposing) && ['Enter', 'Escape'].includes(event.key)) { event.stopPropagation(); if (event.key === 'Escape') event.preventDefault(); } else if (event.key === 'Escape' && menu && !(event.target as Element).closest('dialog[open]')) { event.preventDefault(); closeMenu(); } }}>
    <TacticNavigationGuard hasUnconfirmedWork={() => client.hasUnconfirmedWork()} exportDraft={exportDraft} />
    <header className="tactic-editor-header"><TacticOrigin notebookId={notebookId} scope={JSON.stringify([client.tacticId, session.user.id, contextRefresh])} connected={state.connected} retry={() => setContextRefresh(value => value + 1)} /><Link to={`/tactics/${client.tacticId}/present`}>演示已保存版本</Link><button className="button editor-title" onClick={event => { infoTrigger.current = event.currentTarget; setInfoOpen(true); }}>{projection.fields['tactic.title'] || '战术信息'}</button><strong className="editor-save-slot" role="status">{!state.connected ? (client.hasUnconfirmedWork() ? '连接中断 · 有未确认修改' : '连接中断 · 上次修改已确认') : state.recovery ? '草稿未确认 · 可导出' : state.pending ? `正在保存 ${state.pending} 项…` : client.hasUnconfirmedWork() ? '本页有未确认内容' : '已保存'} · 版本 {state.revision}</strong><span className="editor-online-slot">{state.connected ? `${state.participants.length} 在线` : '未连接'}</span><button className="button" onClick={() => void client.connect()}>重新连接</button><details className="editor-document-menu" open={menu === 'document'}><summary onClick={event => { event.preventDefault(); toggleMenu('document', event.currentTarget); }}>文档、历史与分享</summary><div className="editor-menu-body"><button className="button" aria-label="关闭文档菜单" onClick={closeMenu}>关闭菜单</button><SourceAttribution tacticId={client.tacticId} />
    <CopyTactic tacticId={client.tacticId} session={session} />
    <TacticPublicationTools onNotebookId={setNotebookId} contextRefresh={contextRefresh} tacticId={client.tacticId} session={session} hasUnconfirmedWork={client.hasUnconfirmedWork()} connected={state.connected} connectionId={state.connectionId} readOnly={state.readOnly} /></div></details><details className="editor-state-details" open={menu === 'status'}><summary ref={statusSummary} onClick={event => { event.preventDefault(); toggleMenu('status', event.currentTarget); }}>连接、权限与草稿详情</summary><div className="editor-menu-body"><button className="button" aria-label="关闭状态菜单" onClick={closeMenu}>关闭菜单</button>
    <section aria-label="当前协作状态"><dl><dt>连接</dt><dd>{state.connected ? '已连接' : '连接中断'}</dd><dt>权限</dt><dd>{!state.connected ? '重连后核对权限' : state.readOnly ? '仅可查看' : mobile ? '手机查看布局' : showPreview ? '演示中，仅查看' : '可编辑'}</dd><dt>未确认内容</dt><dd>{state.recovery ? '有待处理的未确认草稿' : state.pending > 0 ? `${state.pending} 项待服务器确认` : client.hasUnconfirmedWork() || draftDirty ? '本页有未提交或未确认内容' : '全部修改已获服务器确认'}</dd></dl></section>
    {(error || leaseError || state.error) && <p role="alert">{error || leaseError || state.error}</p>}
    {state.auxiliaryError && <p role="status">{state.auxiliaryError}</p>}
    {state.readOnly && <p>当前连接仅可查看。编辑席位已满或当前账户没有编辑权限。</p>}
    {mobile && <p>手机端仅查看与演示，请使用电脑编辑。</p>}
    {stageId && stage.id !== stageId && <p>原阶段已被删除，已显示第一阶段；本地动作表单仍保留，请核对后再提交。</p>}
    {draft.stageId && draft.stageId !== stage.id && <p role="alert">原动作所属阶段已被删除，草稿仍属于原阶段。请导出或取消，不能提交到当前阶段。</p>}
    {(client.hasUnconfirmedWork() || draftDirty || state.pending > 0) && <aside className="tactic-recovery"><p>尚有未确认内容。导出副本不会将它标记为已保存。</p><button className="button" onClick={exportDraft}>导出恢复副本</button>{state.recovery && <button className="button" disabled={!state.connected} onClick={() => client.retry()}>按原操作重试</button>}<button className="button" onClick={() => setDiscardConfirm(true)}>放弃草稿并重载</button>{discardConfirm && <><p>确定放弃本页所有未确认内容？建议先导出。</p><button className="button" onClick={() => { cancelDrawing(); setDiscardConfirm(false); void client.discard(); }}>确认放弃</button><button className="button" onClick={() => setDiscardConfirm(false)}>取消</button></>}</aside>}</div></details><details className="editor-panel-menu" open={menu === 'panel'}><summary ref={panelSummary} onClick={event => { event.preventDefault(); toggleMenu('panel', event.currentTarget); }}>面板</summary><div className="tactic-map-toolbar editor-panel-toggles" aria-label="侧栏显示"><button className="button" onClick={closeMenu}>关闭面板菜单</button><button className="button" aria-expanded={!stagesCollapsed} aria-controls={`${sidebarId}-stages`} onClick={() => togglePanel('stage')}>{stagesCollapsed ? '展开阶段与角色' : '收起阶段与角色'}</button><button className="button" aria-expanded={!actionsCollapsed} aria-controls={`${sidebarId}-actions`} onClick={() => togglePanel('action')}>{actionsCollapsed ? '展开动作与说明' : '收起动作与说明'}</button></div></details></header>
    <div className={`tactic-editor-grid${stagesCollapsed ? ' is-stages-collapsed' : ''}${actionsCollapsed ? ' is-actions-collapsed' : ''}`}>
      <aside id={`${sidebarId}-stages`} hidden={stagesCollapsed} ref={stagePanel} className="tactic-editor-sidebar"><button className="button" onClick={() => togglePanel('stage')}>收起执行结构</button><h2>阶段与角色</h2><nav aria-label="编辑阶段">{projection.structure.stages.map((item, index) => <button className="button" key={item.id} aria-label={projection.fields[`stage.${item.id}.name`] || `阶段 ${index + 1}`} aria-pressed={stage.id === item.id} onClick={() => chooseStage(item.id)}><b>{String(index + 1).padStart(2, '0')}</b><span>{projection.fields[`stage.${item.id}.name`] || `阶段 ${index + 1}`}<small>{item.actions.length} 个动作</small></span></button>)}</nav>
        {!mobile && <details className="editor-stage-management"><summary>阶段管理与新增</summary><div className="tactic-map-toolbar"><button className="button" disabled={blocked || draftDirty} onClick={() => void run({ type: 'stage.add', after_stage_id: stage.id })}>新增阶段</button><button className="button" disabled={blocked || draftDirty} onClick={() => void run({ type: 'stage.copy', stage_id: stage.id })}>复制阶段</button><button className="button" disabled={blocked || draftDirty || stageIndex === 0} onClick={() => void run({ type: 'stage.reorder', stage_id: stage.id, to_index: stageIndex - 1 })}>阶段上移</button><button className="button" disabled={blocked || draftDirty || stageIndex === projection.structure.stages.length - 1} onClick={() => void run({ type: 'stage.reorder', stage_id: stage.id, to_index: stageIndex + 1 })}>阶段下移</button><button className="button" disabled={blocked || draftDirty || stageIndex === 0} onClick={() => setConfirmInherit(stage.id)}>重新继承前阶段站位</button><button className="button" disabled={blocked || draftDirty || projection.structure.stages.length <= 1} onClick={() => setConfirmDelete(stage.id)}>删除阶段</button><button className="button" disabled={blocked || projection.structure.roles.length >= 5} onClick={() => void run({ type: 'role.add' })}>新增角色</button></div></details>}
        <h3>地图角色</h3>{projection.structure.roles.map(role => <button className="editor-role-row button" key={role.id} aria-pressed={context === 'role' && selectedRole === role.id} onClick={() => { if (!allowSelection()) return; setRoleId(role.id); setContext('role'); setActionsCollapsed(false); if (matchMedia('(max-width: 820px)').matches) setStagesCollapsed(true); }}><b>{role.number}</b>{projection.fields[`role.${role.id}.name`]}</button>)}
        <h3>{state.connected ? '在线成员' : '最近连接成员（非实时）'}</h3>{state.participants.map(person => <p key={person.connection_id}>{person.name}{person.connection_id === state.connectionId ? '（本窗口）' : ''} · {projection.fields[`stage.${person.stage_id}.name`] || '查看阶段未记录'}{state.connected && person.field ? ' · 编辑文字' : ''}</p>)}
        <h3>正在编辑的对象</h3>{!state.connected ? <p>未连接，暂时无法确认对象占用。</p> : state.locks.length ? state.locks.map(lock => <p key={lock.object_id}>{lock.object_id.startsWith('position:') ? '角色站位' : '动作'}正在被编辑</p>) : <p>暂无占用</p>}
      </aside>
      <div className="tactic-editor-map"><button className="button" disabled={!showPreview && (client.hasUnconfirmedWork() || draftDirty)} onClick={() => setShowPreview(value => !value)}>{showPreview ? '返回地图编辑' : '准备演示已确认版本'}</button>
        {showPreview && state.confirmed ? <TacticPlaybackPanel confirmed={state.confirmed} revision={state.revision} epoch={state.epoch} hasUnconfirmedWork={client.hasUnconfirmedWork()} /> : <TacticMap onReadyChange={setMapReady} projection={projection} stageId={stage.id} selectedRoleId={selectedRole} readOnly={blocked} roleFilter={activeFilter} onRoleFilter={setFilter} onSelectRole={id => { if (!allowSelection()) return; setContext('role'); setActionsCollapsed(false); if (matchMedia('(max-width: 820px)').matches) setStagesCollapsed(true); setRoleId(id); mapSelection.current = id; client.presence(stage.id, undefined, undefined, { selectedObjectId: id }); }} onMapPoint={point} onActionTool={kind => { if (draftDirty) { setError('请先提交或取消当前动作绘制。'); return; } setDraft(value => ({ ...value, kind })); setActionId(''); setContext('action'); setActionsCollapsed(false); if (matchMedia('(max-width: 820px)').matches) setStagesCollapsed(true); }} drawing={draft.points} fullscreenControls={<section className="editor-fullscreen-draft" aria-label="全屏动作绘制"><p>{draft.editing ? '修改原动作' : '新增动作'} · {({ move: '移动', wait: '等待', observe: '观察', utility: '道具', text: '文字' })[draft.kind]} · 已选{draft.points.length}点</p><p>{draft.editing ? '沿用原动作说明。' : draft.description || '尚未填写说明：退出全屏后在动作属性填写，绘制会保留。'}</p>{(error || leaseError || state.error) && <p role="alert">{error || leaseError || state.error}</p>}<button className="button" disabled={blocked || !draftDirty || (draft.kind !== 'wait' && !mapReady)} onClick={() => void saveAction()}>提交当前动作</button><button className="button" disabled={!draftDirty} onClick={cancelDrawing}>取消当前绘制</button><button className="button" disabled={blocked || !draft.points.length} onClick={() => setDraft(value => ({ ...value, points: value.points.slice(0, -1) }))}>撤回当前末点</button><p>说明、角色与执行关系仍在原属性栏；退出全屏后可继续填写。提交仍等待真实保存确认。</p></section>}
          participants={state.connected ? state.participants : []} connectionId={state.connectionId}
          onMapPointer={position => { if (position) client.presence(stage.id, undefined, undefined, { selectedObjectId: mapSelection.current, pointer: position }); else { mapSelection.current = undefined; client.presence(stage.id); } }}
          positionPreviews={Object.fromEntries(projection.structure.roles.flatMap(role => { const value = state.previews[`position:${stage.id}:${role.id}`]; return value ? [[role.id, value]] : []; }))}
          onRoleDragStart={async id => {
            if (draftDirty || blocked) return false;
            try { await client.acquire(`position:${stage.id}:${id}`, stage.initial_positions[id].version); setLeaseError(''); client.setLocalDraft({ stage_id: stage.id, role_id: id, dragging: true }, 'drag'); return true; }
            catch (err) { setLeaseError(err instanceof Error ? err.message : '无法取得编辑权。'); return false; }
          }}
          onRoleDragMove={(id, position) => { client.setLocalDraft({ stage_id: stage.id, role_id: id, position }, 'drag'); client.preview(`position:${stage.id}:${id}`, position); }}
          onRoleDragEnd={(id, position) => {
            if (position && client.canWrite()) void run({ type: 'position.set', stage_id: stage.id, role_id: id, position }, { id: `position:${stage.id}:${id}`, version: stage.initial_positions[id].version }).then(success => { if (success) client.setLocalDraft(null, 'drag'); });
            else { client.release(`position:${stage.id}:${id}`); if (!position) client.setLocalDraft(null, 'drag'); }
          }} />}
        <section className="editor-action-strip" aria-label="本阶段动作"><h3>本阶段动作 <button className="button" disabled={blocked || draftDirty} onClick={() => { setActionId(''); setContext('action'); setActionsCollapsed(false); if (matchMedia('(max-width: 820px)').matches) setStagesCollapsed(true); }}>新增动作</button></h3><div>{stage.actions.map((action, index) => <button className="button" key={action.id} aria-pressed={context === 'action' && actionId === action.id} onClick={() => { if (!allowSelection()) return; setActionId(action.id); setContext('action'); setActionsCollapsed(false); if (matchMedia('(max-width: 820px)').matches) setStagesCollapsed(true); }}><b>{index + 1} · {({ move: '移动', wait: '等待', observe: '观察', utility: '道具', text: '文字' })[action.kind]}</b><span>{projection.fields[`role.${action.kind === 'utility' ? action.thrower_role_id : action.role_id}.name`] || '全队'} · {index > 0 && action.group_id === stage.actions[index - 1].group_id ? '同时执行' : '接着执行'}</span><small>{projection.fields[`action.${action.id}.description`] || '尚未填写说明'}</small></button>)}</div></section>
      </div>
      <aside id={`${sidebarId}-actions`} hidden={actionsCollapsed} ref={actionPanel} className="tactic-editor-actions"><button className="button" onClick={() => togglePanel('action')}>收起属性</button><h2>{context === 'stage' ? '阶段属性' : context === 'role' ? '角色属性' : '动作属性'}</h2><button className="button" onClick={event => { infoTrigger.current = event.currentTarget; setInfoOpen(true); }}>战术说明与基本信息</button><div className="editor-context-fields" hidden={context !== 'role'}>{missingRole && <p role="status">原角色已删除，请重新选择角色。</p>}{projection.structure.roles.map(role => <div key={role.id} hidden={role.id !== selectedRole}>{text(`role.${role.id}.name`, `角色 ${role.number} 名称`)}<p>阵营：{projection.structure.side}（继承战术）</p><p>站位：{stage.initial_positions[role.id]?.x.toFixed(1)} / {stage.initial_positions[role.id]?.y.toFixed(1)}</p>{!blocked && mapReady && <p>点击地图“设置站位”后在地图选择位置。</p>}<button className="button" onClick={() => setFilter(role.id)}>筛选此角色</button><button className="button" onClick={() => setFilter(null)}>查看全部角色</button></div>)}</div><details className="editor-undo-tools"><summary>撤销与重做</summary><div className="tactic-map-toolbar"><button className="button" disabled={!writable} onClick={() => client.undo?.undo()}>撤销文字</button><button className="button" disabled={!writable} onClick={() => client.undo?.redo()}>重做文字</button></div>
        <div className="tactic-map-toolbar"><button className="button" disabled={blocked || draftDirty || client.hasUnconfirmedWork() || !state.objectUndo} onClick={() => void undoObject()}>{state.objectUndo?.is_delete ? '撤销删除并恢复为副本' : '撤销对象'}</button></div>
        <p>对象撤销仅限本窗口最近一次已保存的对象修改，只有一次机会，没有对象重做。后续对象变化（包括队友修改其他对象）会使本次机会失效；断线或重连后不保留。撤销删除会恢复为新副本。</p>
        </details><div hidden={context !== 'stage'}>{text(`stage.${stage.id}.name`, '阶段名称')}{text(`stage.${stage.id}.description`, '阶段说明')}{text(`stage.${stage.id}.condition`, '进入条件')}
        <details><summary>管理关联教学（{stage.lineup_ids.length}）</summary><StageTeachingReferences client={client} stage={stage} revision={state.revision} editable={writable && !busy && state.pending === 0} /></details>
        </div><div hidden={context !== 'action'}><h3>动作</h3>{missingAction && <p role="status">原动作已删除，请重新选择动作；未提交草稿仍保留，可取消或导出。</p>}{stage.actions.filter(action => !activeFilter || (action.kind === 'utility' ? action.thrower_role_id : action.role_id) === activeFilter || (action.kind === 'text' && action.role_id === null)).map(action => <article key={action.id} className="tactic-action-card" hidden={action.id !== actionId}><h4>{({ move: '移动', wait: '等待', observe: '观察', utility: '道具', text: '文字' })[action.kind]}</h4><dl aria-label="动作摘要"><dt>执行角色</dt><dd>{projection.fields[`role.${action.kind === 'utility' ? action.thrower_role_id : action.role_id}.name`] || '全队'}</dd><dt>执行关系</dt><dd>{stage.actions.indexOf(action) > 0 && action.group_id === stage.actions[stage.actions.indexOf(action) - 1].group_id ? '同时执行' : '接着执行'}</dd>{action.kind === 'utility' && <><dt>道具类型</dt><dd>{({ smoke: '烟雾弹', flash: '闪光弹', molotov: '燃烧弹', he: '高爆手雷', decoy: '诱饵弹' })[action.utility_type]}</dd></>}{action.kind === 'move' && <><dt>路线</dt><dd>{action.path.length} 个节点</dd></>}{(action.kind === 'observe' || action.kind === 'utility') && <><dt>目标</dt><dd>{action.target.x.toFixed(1)} / {action.target.y.toFixed(1)}</dd></>}{action.kind === 'text' && <><dt>标注位置</dt><dd>{action.position.x.toFixed(1)} / {action.position.y.toFixed(1)}</dd></>}</dl>{text(`action.${action.id}.description`, '动作说明')}{!mobile && <div className="tactic-map-toolbar"><button className="button" disabled={blocked || draftDirty} onClick={() => void editAction(action)}>修改动作</button><button className="button" disabled={blocked || draftDirty || stage.actions.indexOf(action) === 0} onClick={() => void run({ type: 'action.reorder', stage_id: stage.id, action_id: action.id, to_index: stage.actions.indexOf(action) - 1, group_mode: 'after' }, { id: `action:${action.id}`, version: action.version })}>动作上移</button><button className="button" disabled={blocked || draftDirty || stage.actions.indexOf(action) === stage.actions.length - 1} onClick={() => void run({ type: 'action.reorder', stage_id: stage.id, action_id: action.id, to_index: stage.actions.indexOf(action) + 1, group_mode: 'after' }, { id: `action:${action.id}`, version: action.version })}>动作下移</button><button className="button" disabled={blocked || draftDirty} onClick={() => setConfirmDelete(action.id)}>删除动作</button><label>动作编组<select disabled={blocked || draftDirty} value={new Set(stage.actions.slice(0, stage.actions.indexOf(action) + 1).map(value => value.group_id)).size - 1} onChange={event => void run({ type: 'action.group', stage_id: stage.id, action_id: action.id, group_index: Number(event.target.value) }, { id: `action:${action.id}`, version: action.version })}>{[...new Set(stage.actions.map(value => value.group_id)), 'new'].map((group, groupIndex) => <option key={group} value={groupIndex}>{group === 'new' ? '新编组' : `编组 ${groupIndex + 1}`}</option>)}</select></label></div>}</article>)}
        {!mobile && <form hidden={Boolean(actionId && !draft.editing && !draftDirty)} className="admin-form" onSubmit={event => void submitAction(event)}><fieldset disabled={blocked}><legend>{draft.editing ? '修改选中动作' : '添加动作'}</legend><label>动作类型<select disabled={Boolean(draft.editing)} value={draft.kind} onChange={event => setDraft(value => ({ ...value, kind: event.target.value as ActionDraft['kind'], points: [] }))}><option value="move">移动路线</option><option value="wait">等待</option><option value="observe">观察方向</option><option value="utility">道具</option><option value="text">文字提示</option></select></label><label>执行角色<select value={draft.role || (draft.kind === 'text' ? '' : selectedRole)} onChange={event => setDraft(value => ({ ...value, role: event.target.value, points: [] }))}>{draft.kind === 'text' && <option value="">全队提示</option>}{projection.structure.roles.map(role => <option key={role.id} value={role.id}>{projection.fields[`role.${role.id}.name`]}</option>)}</select></label>
          {draft.kind === 'utility' && <label>道具<select value={draft.utility} onChange={event => setDraft(value => ({ ...value, utility: event.target.value as ActionDraft['utility'] }))}><option value="smoke">烟雾弹</option><option value="flash">闪光弹</option><option value="molotov">燃烧弹</option><option value="he">高爆手雷</option><option value="decoy">诱饵弹</option></select></label>}
          {!draft.editing && <><label>与上一动作<select value={draft.group} onChange={event => setDraft(value => ({ ...value, group: event.target.value as 'after' | 'parallel', points: [] }))}><option value="after">接着执行</option><option value="parallel">同时执行</option></select></label><label>新动作说明<textarea required maxLength={2000} value={draft.description} onChange={event => setDraft(value => ({ ...value, description: event.target.value }))} /></label></>}
          <p>{draft.kind === 'move' ? `在地图上点选路线，最多50个点；已选${draft.points.length}点。` : draft.kind === 'wait' ? '等待动作不需要地图目标。' : '在地图上点选目标。'}</p><button className="button" type="submit">{draft.editing ? '提交动作修改' : '添加到阶段'}</button><button className="button" type="button" onClick={() => setDraft(value => ({ ...value, points: [] }))}>重新绘制</button><button className="button" type="button" onClick={() => setDraft(value => ({ ...value, points: value.points.slice(0, -1) }))}>撤回末点</button><button className="button" type="button" onClick={cancelDrawing}>取消动作绘制</button></fieldset></form>}
        </div><details><summary>可恢复副本</summary><label>动作恢复目标<select value={restoreTarget} onChange={event => setRestoreTarget(event.target.value)}><option value="">请选择目标阶段</option>{projection.structure.stages.map(item => <option value={item.id} key={item.id}>{projection.fields[`stage.${item.id}.name`]}</option>)}</select></label>{state.recoveries.map(recovery => <p key={recovery.id}>{recovery.kind === 'stage' ? '已删除阶段' : '已删除动作'} · {new Date(recovery.expires_at).toLocaleDateString()}到期<button className="button" disabled={blocked || (recovery.kind === 'action' && !restoreTarget)} onClick={() => void run(recovery.kind === 'action' ? { type: 'recovery.restore', recovery_id: recovery.id, stage_id: restoreTarget } : { type: 'recovery.restore', recovery_id: recovery.id, after_stage_id: stage.id })}>恢复为新副本</button></p>)}
      </details></aside>
    </div>
    <WorkshopDialog title="战术信息" className="editor-info-dialog" returnFocus={infoTrigger} suspended={!infoOpen} onClose={() => setInfoOpen(false)}><p>字段即时同步；关闭不会撤销已提交修改。</p>{text('tactic.title', '战术名称', 100)}{text('tactic.purpose', '战术目的')}{text('tactic.applicability', '适用情况')}
        <label>阵营<select disabled={blocked} value={projection.structure.side} onChange={event => void run({ type: 'tactic.side.set', side: event.target.value })}><option value="T">T</option><option value="CT">CT</option></select></label>
        </WorkshopDialog>
    {confirmInherit && <WorkshopDialog title="确认重新继承站位" onClose={() => setConfirmInherit(null)}><p>将替换“{projection.fields[`stage.${confirmInherit}.name`] || '原阶段'}”的初始站位，并重新派生该阶段后续移动路线的起点，保留其他路径点。其他阶段的独立站位不会被改写。</p>{(stage.id !== confirmInherit || projection.structure.stages.findIndex(item => item.id === confirmInherit) <= 0) && <p role="alert">原阶段已变化，请取消后重新选择阶段。</p>}<button className="button" disabled={blocked || draftDirty || stage.id !== confirmInherit || projection.structure.stages.findIndex(item => item.id === confirmInherit) <= 0} onClick={() => { if (stage.id === confirmInherit && projection.structure.stages.findIndex(item => item.id === confirmInherit) > 0) void run({ type: 'stage.reinherit', stage_id: confirmInherit }).then(success => { if (success) setConfirmInherit(null); }); }}>确认重新继承</button><button className="button" onClick={() => setConfirmInherit(null)}>取消继承</button></WorkshopDialog>}
    {confirmDelete && <WorkshopDialog title="确认删除" onClose={() => setConfirmDelete(null)}><p>确定删除{confirmDelete === stage.id ? '阶段及其动作' : '动作'}？可在30天内从恢复副本中恢复。</p>{!deleteTargetValid && <p role="alert">原删除目标已消失，请关闭后重新选择。</p>}<button className="button" disabled={blocked || !deleteTargetValid} onClick={() => {
      if (!deleteTargetValid) return;
      const action = stage.actions.find(item => item.id === confirmDelete);
      void run(action ? { type: 'action.delete', stage_id: stage.id, action_id: action.id } : { type: 'stage.delete', stage_id: confirmDelete }, action ? { id: `action:${action.id}`, version: action.version } : undefined).then(success => { if (success) setConfirmDelete(null); });
    }}>确认删除</button><button className="button" onClick={() => setConfirmDelete(null)}>取消</button></WorkshopDialog>}
  </section>;
}
