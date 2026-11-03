import { useState } from 'react';
import TacticMap from './TacticMap';
import { useTacticPlayback } from './useTacticPlayback';
import type { TacticProjection } from './editorTypes';
import { SavedTeachingReferences, type TeachingResolver } from './StageTeachingReferences';
import './workshop-playback.css';

// The formal editor supplies its confirmed snapshot; this panel never writes or claims an ACK.
export default function TacticPlaybackPanel({ confirmed, revision, epoch, hasUnconfirmedWork, resolveTeaching, teachingScope }: { confirmed: TacticProjection; revision: number; epoch: string; hasUnconfirmedWork: boolean; resolveTeaching?: TeachingResolver; teachingScope?: string }) {
  const [selectedStage, setSelectedStage] = useState(confirmed.structure.stages[0]?.id ?? '');
  const [roleFilter, setRoleFilter] = useState<string | null>(null);
  const playback = useTacticPlayback(confirmed, revision, epoch, hasUnconfirmedWork);
  const projection = playback.play?.snapshot ?? confirmed;
  const stageId = playback.play ? projection.structure.stages[playback.play.stageIndex].id : selectedStage;
  const stage = projection.structure.stages.find(value => value.id === stageId) ?? projection.structure.stages[0];
  const activeRoleFilter = projection.structure.roles.some(role => role.id === roleFilter) ? roleFilter : null;
  const missingStage = !playback.play && !confirmed.structure.stages.some(value => value.id === selectedStage);
  return <section className="workshop-playback" aria-label="战术演示">
    {playback.changed && <p role="status">预览期间收到新修改，退出预览后查看。</p>}
    {missingStage && <p role="status">此前选择的阶段已被删除，已显示当前第一阶段。</p>}
    {hasUnconfirmedWork && !playback.play && <p>请先处理未确认编辑，再开始演示。</p>}
    {!playback.play && <p>演示准备：当前查看所选的已确认内容；开始自动或分阶段演示后固定快照。</p>}
    <nav aria-label="演示阶段">{projection.structure.stages.map((item, index) => <button className="button" key={item.id} aria-pressed={stage?.id === item.id} onClick={() => {
      if (playback.play) playback.navigate(index - playback.play.stageIndex); else setSelectedStage(item.id);
    }}>{projection.fields[`stage.${item.id}.name`] || `阶段 ${index + 1}`}</button>)}</nav>
    <TacticMap projection={projection} stageId={stage?.id ?? ''} frame={playback.play?.frame} readOnly roleFilter={activeRoleFilter} onRoleFilter={setRoleFilter}>
    <div className="tactic-map-toolbar">{playback.play ? <>
      <button className="button" disabled={!playback.canPause} onClick={playback.togglePause}>{playback.play.paused ? '继续' : '暂停'}</button>
      <button className="button" onClick={playback.replay}>重播本阶段</button>
      <button className="button" disabled={playback.play.stageIndex === 0} onClick={() => playback.navigate(-1)}>上一阶段</button>
      <button className="button" disabled={playback.play.stageIndex >= projection.structure.stages.length - 1} onClick={() => playback.navigate(1)}>下一阶段</button>
      <button className="button" onClick={() => { setSelectedStage(stage?.id ?? ''); playback.exit(); }}>退出预览</button>
    </> : <><button className="button" disabled={hasUnconfirmedWork} onClick={() => playback.start('auto', stageId)}>自动演示</button><button className="button" disabled={hasUnconfirmedWork} onClick={() => playback.start('stage', stageId)}>分阶段演示</button></>}</div>
    {stage && <><h3>进入条件</h3><p>{projection.fields[`stage.${stage.id}.condition`] || '尚未填写'}</p><ol>{stage.actions.filter(action => !activeRoleFilter || (action.kind === 'utility' ? action.thrower_role_id : action.role_id) === activeRoleFilter || (action.kind === 'text' && action.role_id === null)).map(action => <li key={action.id} aria-current={playback.play?.frame.activeActionIds.includes(action.id) ? 'step' : undefined}>{projection.fields[`action.${action.id}.description`] || '动作说明'}</li>)}</ol></>}
    {stage && <SavedTeachingReferences tacticId={projection.structure.document_id} ids={stage.lineup_ids} revision={playback.play?.revision ?? revision} resolver={resolveTeaching} scope={teachingScope} />}
    </TacticMap>
  </section>;
}
