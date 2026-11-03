import { useEffect, useId, useRef, useState, type CSSProperties, type PointerEvent, type ReactNode } from 'react';
import { mapPointFromRect } from '../../../collaboration/src/stage-board-interactions.mjs';
import type { PlaybackFrame, Point, TacticAction, TacticProjection, TacticStage } from './editorTypes';
import type { Participant } from './collaborationClient';
import './editor.css';

interface Props {
  projection: TacticProjection; stageId: string; selectedRoleId?: string; frame?: PlaybackFrame;
  onActionTool?: (kind: TacticAction['kind']) => void;
  readOnly: boolean; drawing?: Point[]; onSelectRole?: (id: string) => void;
  onMapPoint?: (point: Point, tool: 'select' | 'position') => void;
  children?: ReactNode;
  fullscreenControls?: ReactNode;
  onReadyChange?: (ready: boolean) => void;
  roleFilter?: string | null;
  onRoleFilter?: (id: string | null) => void;
  positionPreviews?: Record<string, Point>;
  onRoleDragStart?: (id: string) => Promise<boolean>;
  onRoleDragMove?: (id: string, point: Point) => void;
  onRoleDragEnd?: (id: string, point: Point | null) => void;
  onMapPointer?: (point: Point | null) => void;
  participants?: Participant[];
  connectionId?: string;
}
const utilities = { smoke: '烟雾弹', flash: '闪光弹', molotov: '燃烧弹', he: '高爆手雷', decoy: '诱饵弹' };
const symbols = { smoke: '烟', flash: '闪', molotov: '火', he: '雷', decoy: '诱' };
function beforeAction(stage: TacticStage, actionId: string, roleId: string): Point {
  const positions: Record<string, Point> = { ...stage.initial_positions };
  for (const action of stage.actions) {
    if (action.id === actionId) break;
    if (action.kind === 'move') positions[action.role_id] = action.path.at(-1)!;
  }
  return positions[roleId] ?? { x: 50, y: 50 };
}
export default function TacticMap({ projection, stageId, selectedRoleId, frame, readOnly, onActionTool, drawing, onSelectRole, onMapPoint, children, fullscreenControls, onReadyChange, roleFilter = null, onRoleFilter, positionPreviews, onRoleDragStart, onRoleDragMove, onRoleDragEnd, onMapPointer, participants = [], connectionId }: Props) {
  const panel = useRef<HTMLElement>(null);
  const drawingLayer = useRef<HTMLDivElement>(null);
  const drag = useRef<{ pointer: number; x: number; y: number; originX: number; originY: number } | null>(null);
  const roleDrag = useRef<{ roleId: string; x: number; y: number; point: Point | null; ready: boolean; finished: boolean } | null>(null);
  const [dragPreview, setDragPreview] = useState<{ roleId: string; point: Point } | null>(null);
  const [view, setView] = useState({ zoom: 1, x: 0, y: 0 });
  const [tool, setTool] = useState<'select' | 'position' | 'pan'>('select');
  const [fullscreen, setFullscreen] = useState(false);
  const [error, setError] = useState('');
  const [imageStatus, setImageStatus] = useState<'loading' | 'ready' | 'error'>('loading');
  const [imageAttempt, setImageAttempt] = useState(0);
  const imageReady = useRef(false);
  useEffect(() => { onReadyChange?.(imageStatus === 'ready'); }, [imageStatus, onReadyChange]);
  useEffect(() => () => onReadyChange?.(false), [onReadyChange]);
  function resourceState(next: 'loading' | 'ready' | 'error') {
    imageReady.current = next === 'ready'; setImageStatus(next); onReadyChange?.(imageReady.current);
    if (next !== 'ready' && roleDrag.current) {
      const value = roleDrag.current; value.finished = true; value.point = null; roleDrag.current = null; setDragPreview(null);
      if (value.ready) onRoleDragEnd?.(value.roleId, null);
    }
  }
  const [mobile] = useState(() => matchMedia('(max-width: 700px) and (pointer: coarse) and (hover: none)').matches && !matchMedia('(any-pointer: fine)').matches);
  const arrowId = useId();
  const stage = projection.structure.stages.find(value => value.id === stageId) ?? projection.structure.stages[0];
  useEffect(() => {


    const full = () => setFullscreen(document.fullscreenElement === panel.current);
    document.addEventListener('fullscreenchange', full);
    return () => { document.removeEventListener('fullscreenchange', full); };
  }, []);
  const canEdit = !readOnly && !mobile && !frame;
  const editable = canEdit && imageStatus === 'ready';
  const visibleActions = stage?.actions.filter(action => !roleFilter || (action.kind === 'utility' ? action.thrower_role_id : action.role_id) === roleFilter || (action.kind === 'text' && action.role_id === null)) ?? [];
  if (!stage) return <p>暂无可显示的阶段。</p>;
  function pointerDown(event: PointerEvent<HTMLDivElement>) {
    if (tool !== 'pan' || event.button !== 0) return;
    drag.current = { pointer: event.pointerId, x: event.clientX, y: event.clientY, originX: view.x, originY: view.y };
    event.currentTarget.setPointerCapture(event.pointerId);
  }
  function pointerMove(event: PointerEvent<HTMLDivElement>) {
    if (drawingLayer.current && onMapPointer) {
      const rect = drawingLayer.current.getBoundingClientRect();
      onMapPointer(rect.width > 0 && rect.height > 0 && event.clientX >= rect.left && event.clientX <= rect.right && event.clientY >= rect.top && event.clientY <= rect.bottom ? mapPointFromRect(event.clientX, event.clientY, rect) : null);
    }
    const value = drag.current;
    if (!value || value.pointer !== event.pointerId) return;
    setView(previous => ({ ...previous, x: value.originX + event.clientX - value.x, y: value.originY + event.clientY - value.y }));
  }
  async function toggleFullscreen() {
    try { if (fullscreen) await document.exitFullscreen(); else await panel.current?.requestFullscreen(); }
    catch { setError('当前浏览器无法进入全屏。'); }
  }
  return <section className="tactic-map-panel" ref={panel} aria-label="阶段地图">
    <h2>{projection.fields[`stage.${stage.id}.name`] || '阶段地图'}</h2>
    {mobile && <p>手机端仅供查看与演示，请使用电脑编辑。</p>}
    {error && <p role="alert">{error}</p>}
    <div className="tactic-map-toolbar">
      {onRoleFilter && <label>按角色查看<select value={roleFilter ?? ''} onChange={event => onRoleFilter(event.target.value || null)}><option value="">全部角色</option>{projection.structure.roles.map(role => <option key={role.id} value={role.id}>{projection.fields[`role.${role.id}.name`] || `角色 ${role.number}`}</option>)}</select></label>}
      {canEdit && <><button className="button" disabled={!editable} aria-pressed={tool === 'select'} onClick={() => setTool('select')}>选择</button><button className="button" disabled={!editable} aria-pressed={tool === 'position'} onClick={() => setTool('position')}>设置站位</button></>}
      {canEdit && onActionTool && (['move', 'observe', 'wait', 'utility', 'text'] as const).map(kind => <button className="button" disabled={!editable} key={kind} onClick={() => { setTool('select'); onActionTool(kind); }}>{({ move: '移动', observe: '观察', wait: '等待', utility: '道具', text: '文字' })[kind]}</button>)}
      <button className="button" aria-pressed={tool === 'pan'} onClick={() => setTool(tool === 'pan' ? 'select' : 'pan')}>平移地图</button>
      <label>缩放<input type="range" min="1" max="4" step="0.1" value={view.zoom} onChange={event => setView(value => ({ ...value, zoom: Number(event.target.value) }))} /></label><span>{view.zoom.toFixed(1)}×</span>
      <button className="button" onClick={() => setView({ zoom: 1, x: 0, y: 0 })}>复位</button><button className="button" onClick={() => void toggleFullscreen()}>{fullscreen ? '退出全屏' : '全屏'}</button>
    </div>
    <div className={`tactic-map-viewport${tool === 'pan' ? ' is-panning' : ''}`} onPointerDown={pointerDown} onPointerMove={pointerMove} onPointerLeave={() => onMapPointer?.(null)} onBlur={event => { if (!event.currentTarget.contains(event.relatedTarget)) onMapPointer?.(null); }} onPointerUp={() => { drag.current = null; }} onPointerCancel={() => { drag.current = null; onMapPointer?.(null); }}>
      {imageStatus !== 'ready' && <div className={`tactic-map-resource ${imageStatus === 'error' ? 'is-error' : ''}`} role={imageStatus === 'error' ? 'alert' : 'status'}><p>{imageStatus === 'error' ? '地图加载失败，当前位置与绘制仍保留。' : '正在加载地图…'}</p>{imageStatus === 'error' && <button className="button" onClick={() => { resourceState('loading'); setImageAttempt(value => value + 1); }}>重试地图</button>}</div>}
      <div className="tactic-map-world" style={{ transform: `translate(${view.x}px, ${view.y}px)` }}><div ref={drawingLayer} className="tactic-map-drawing" style={{ transform: `scale(${view.zoom})`, '--inverse-zoom': 1 / view.zoom } as CSSProperties} onClick={event => {
        if (!editable || !imageReady.current || tool === 'pan' || !drawingLayer.current || (event.target as Element).closest('button')) return;
        onMapPoint?.(mapPointFromRect(event.clientX, event.clientY, drawingLayer.current.getBoundingClientRect()), tool);
      }}>
        <img src={`/assets/maps/mirage.svg${imageAttempt ? `?retry=${imageAttempt}` : ''}`} onLoad={() => resourceState('ready')} onError={() => resourceState('error')} alt="Mirage 战术地图底图" draggable={false} />
        <svg viewBox="0 0 100 100" preserveAspectRatio="none" aria-hidden="true"><defs><marker id={arrowId} viewBox="0 0 8 8" refX="7" refY="4" markerWidth="5" markerHeight="5" orient="auto"><path d="M0 0L8 4L0 8Z" fill="currentColor" /></marker></defs>
          {visibleActions.map(action => {
            const active = frame?.activeActionIds.includes(action.id);
            if (action.kind === 'move') return <polyline key={action.id} className={active ? 'is-active' : ''} points={action.path.map(point => `${point.x},${point.y}`).join(' ')} />;
            if (action.kind !== 'observe' && action.kind !== 'utility') return null;
            const start = beforeAction(stage, action.id, action.kind === 'utility' ? action.thrower_role_id : action.role_id);
            let end = action.target;
            if (action.kind === 'observe' && active && frame) {
              const prior = stage.actions.slice(0, stage.actions.indexOf(action)).filter(value => value.kind === 'observe' && value.role_id === action.role_id).at(-1);
              const priorOrigin = prior?.kind === 'observe' ? beforeAction(stage, prior.id, prior.role_id) : start;
              const originAngle = prior?.kind === 'observe' ? Math.atan2(prior.target.y - priorOrigin.y, prior.target.x - priorOrigin.x) : 0;
              const targetAngle = Math.atan2(end.y - start.y, end.x - start.x);
              const delta = ((targetAngle - originAngle + Math.PI * 3) % (Math.PI * 2)) - Math.PI;
              const angle = originAngle + delta * frame.progress;
              const length = Math.hypot(end.x - start.x, end.y - start.y);
              end = { x: start.x + Math.cos(angle) * length, y: start.y + Math.sin(angle) * length };
            }
            return <line key={action.id} className={`${action.kind}${active ? ' is-active' : ''}`} x1={start.x} y1={start.y} x2={end.x} y2={end.y} markerEnd={action.kind === 'observe' ? `url(#${arrowId})` : undefined} />;
          })}
          {drawing && <polyline className="is-draft" points={drawing.map(point => `${point.x},${point.y}`).join(' ')} />}
        </svg>
        {drawing?.length === 1 && <span className="tactic-map-pin tactic-draft-target" role="img" aria-label="待提交目标位置" style={{ left: `${drawing[0].x}%`, top: `${drawing[0].y}%` }}>◎</span>}
        {visibleActions.map(action => {
          if (!['utility', 'text', 'observe'].includes(action.kind)) return null;
          const point = action.kind === 'text' ? action.position : action.kind === 'utility' || action.kind === 'observe' ? action.target : null;
          if (!point) return null;
          const label = action.kind === 'utility' ? utilities[action.utility_type] : action.kind === 'text' ? '文字提示' : '观察方向';
          return <span key={action.id} className={`tactic-map-pin ${action.kind}${frame?.activeActionIds.includes(action.id) ? ' is-active' : ''}`} style={{ left: `${point.x}%`, top: `${point.y}%` }} title={`${label}：${projection.fields[`action.${action.id}.description`] ?? ''}`}>{action.kind === 'utility' ? symbols[action.utility_type] : action.kind === 'text' ? 'T' : '↗'}</span>;
        })}
        {projection.structure.roles.filter(role => !roleFilter || role.id === roleFilter).map(role => {
          const point = dragPreview?.roleId === role.id ? dragPreview.point : frame?.positions[role.id] ?? positionPreviews?.[role.id] ?? stage.initial_positions[role.id];
          if (!point) return null;
          const name = projection.fields[`role.${role.id}.name`] || `角色 ${role.number}`;
          return <div className="tactic-role-anchor" key={role.id} style={{ left: `${point.x}%`, top: `${point.y}%` }}><button className={`tactic-role-marker${selectedRoleId === role.id ? ' is-selected' : ''}`} style={{ '--role-color': role.color } as CSSProperties} aria-label={`选择角色 ${role.number}：${name}`} onClick={() => onSelectRole?.(role.id)}
            onPointerDown={event => {
              if (!editable || !imageReady.current || !onRoleDragStart || tool === 'pan' || drawing?.length) return;
              event.stopPropagation(); event.currentTarget.setPointerCapture(event.pointerId); onSelectRole?.(role.id);
              const value = { roleId: role.id, x: event.clientX, y: event.clientY, point: null as Point | null, ready: false, finished: false }; roleDrag.current = value;
              void onRoleDragStart(role.id).then(ready => { value.ready = ready; if (value.finished) onRoleDragEnd?.(role.id, ready && imageReady.current ? value.point : null); });
            }}
            onPointerMove={event => {
              const value = roleDrag.current;
              if (!value?.ready || !editable || !imageReady.current || !drawingLayer.current) return;
              if (Math.hypot(event.clientX - value.x, event.clientY - value.y) < 3 && !value.point) return;
              const next = mapPointFromRect(event.clientX, event.clientY, drawingLayer.current.getBoundingClientRect());
              value.point = next; setDragPreview({ roleId: role.id, point: next }); onRoleDragMove?.(role.id, next);
            }}
            onPointerUp={event => {
              event.stopPropagation(); const value = roleDrag.current; if (!value) return;
              value.finished = true; roleDrag.current = null; setDragPreview(null);
              if (value.ready) onRoleDragEnd?.(role.id, editable && imageReady.current ? value.point : null);
            }}
            onPointerCancel={() => { const value = roleDrag.current; if (value) { value.finished = true; value.point = null; if (value.ready) onRoleDragEnd?.(role.id, null); } roleDrag.current = null; setDragPreview(null); }}
          ><span>{role.number}</span><small>{name}</small></button></div>;
        })}
        {participants.filter(peer => peer.connection_id !== connectionId && peer.stage_id === stage.id).map(peer => {
          const color = /^#[0-9a-f]{6}$/i.test(peer.color) ? peer.color : '#315d57';
          const role = projection.structure.roles.find(item => item.id === peer.selected_object_id && (!roleFilter || item.id === roleFilter));
          const action = visibleActions.find(item => item.id === peer.selected_object_id);
          const selectedPoint = role ? frame?.positions[role.id] ?? positionPreviews?.[role.id] ?? stage.initial_positions[role.id]
            : action?.kind === 'move' ? action.path.at(-1) : action?.kind === 'utility' || action?.kind === 'observe' ? action.target : action?.kind === 'text' ? action.position : action?.kind === 'wait' ? stage.initial_positions[action.role_id] : undefined;
          const pointer = peer.pointer && [peer.pointer.x, peer.pointer.y].every(value => Number.isFinite(value) && value >= 0 && value <= 100) ? peer.pointer : null;
          return <div key={peer.connection_id} className="tactic-map-peer" style={{ '--peer-color': color } as CSSProperties}>
            {selectedPoint && <span className="tactic-peer-selection" role="img" aria-label={`${peer.name}选中的${role ? '角色' : '动作'}`} style={{ left: `${selectedPoint.x}%`, top: `${selectedPoint.y}%` }}><span>{peer.name} · 选中{role ? '角色' : '动作'}</span></span>}
            {pointer && <span className="tactic-peer-pointer" role="img" aria-label={`${peer.name}的地图指针`} style={{ left: `${pointer.x}%`, top: `${pointer.y}%` }}>↖<span>{peer.name}</span></span>}
          </div>;
        })}
      </div></div>
    </div>
    <p className="tactic-map-description">{projection.fields[`stage.${stage.id}.description`] || '尚未填写阶段说明'}</p>
    <div className="tactic-map-content">{children}</div>
    {fullscreen && fullscreenControls && <div className="tactic-fullscreen-controls">{fullscreenControls}</div>}
  </section>;
}
