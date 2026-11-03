import { useEffect, useRef, useState } from 'react';
import { ApiError, publicApi } from '../api/client';
import VideoResultPanel from '../components/VideoResultPanel';
import type { LineupDetail, LineupSummary, MapResponse, OriginChoice, TargetChoices } from '../types/api';
import { accountRequest } from './api';
import type { TacticCollaborationClient } from './collaborationClient';
import type { TacticStage } from './editorTypes';

export interface Reference { id: string; available: boolean; lineup: LineupDetail | null }
export type TeachingResolver = (id: string, signal?: AbortSignal) => Promise<Reference>;
function TeachingCard({ tacticId, id, revision, resolver, scope = '' }: { tacticId: string; id: string; revision: number; resolver?: TeachingResolver; scope?: string }) {
  const [attempt, setAttempt] = useState(0);
  const key = JSON.stringify([tacticId, id, revision, attempt, scope]);
  const [result, setResult] = useState<{ key: string; value?: Reference; error?: string }>({ key: '' });
  const [expanded, setExpanded] = useState(false);
  const video = useRef<HTMLVideoElement | null>(null);
  useEffect(() => {
    const controller = new AbortController();
    (resolver ? resolver(id, controller.signal) : accountRequest<Reference>(`tactics/${encodeURIComponent(tacticId)}/lineups/${encodeURIComponent(id)}`, 'GET', undefined, undefined, controller.signal))
      .then(value => { if (!controller.signal.aborted) setResult({ key, value }); })
      .catch((error: unknown) => {
        if (!controller.signal.aborted) setResult({ key, error: error instanceof ApiError && error.status === 404 ? '当前无法读取此引用，可能已被移除或没有访问权限。' : '暂时无法读取教学，请重试。' });
      });
    return () => controller.abort();
  }, [tacticId, id, revision, attempt, key, resolver]);
  if (result.key !== key) return <p role="status">正在读取关联教学…</p>;
  if (result.error) return <div><p role="alert">{result.error}</p><button className="button" onClick={() => setAttempt(value => value + 1)}>重试读取教学</button></div>;
  const detail = result.value?.available ? result.value.lineup : null;
  if (!detail) return <p>教学暂不可用，原引用仍保留。不会自动替换为其他教学。</p>;
  return <article className="tactic-teaching-card"><p>{detail.origin.name_zh} → {detail.target.name_zh}</p><button className="button" onClick={() => setExpanded(value => !value)}>{expanded ? '收起教学' : '查看关联教学'}</button><button className="button" onClick={() => setAttempt(value => value + 1)}>刷新教学状态</button>
    {expanded && <VideoResultPanel key={key} detail={{ ...detail, other_lineups: [] }} notices={[]} showBack={false} loading={false} videoRef={video} failedLineupIds={new Set()} onMediaFailed={() => {}} onBack={() => {}} onOpenOther={() => {}} onQuickSwitch={() => {}} />}
  </article>;
}
function TeachingPicker({ selected, onToggle }: { selected: string[]; onToggle: (id: string) => void }) {
  const [catalog, setCatalog] = useState<MapResponse | null>(null);
  const [error, setError] = useState('');
  const [target, setTarget] = useState('');
  const [choices, setChoices] = useState<{ target: string; value?: TargetChoices; error?: string }>({ target: '' });
  useEffect(() => {
    const controller = new AbortController();
    publicApi.map(controller.signal).then(value => { if (!controller.signal.aborted) setCatalog(value); }).catch(() => { if (!controller.signal.aborted) setError('暂时无法读取教学目录，请关闭后重新打开选择器。'); });
    return () => controller.abort();
  }, []);
  useEffect(() => {
    if (!target) return;
    const controller = new AbortController();
    publicApi.targetLineups(target, controller.signal)
      .then(value => { if (!controller.signal.aborted) setChoices({ target, value }); })
      .catch(() => { if (!controller.signal.aborted) setChoices({ target, error: '暂时无法读取此目标的教学，请重新选择。' }); });
    return () => controller.abort();
  }, [target]);
  return <section aria-label="公开教学选择器"><p>从本站已公开的烟雾教学中选择，其他动作类型不代表已有对应视频。最多关联10条。</p>{error && <p role="alert">{error}</p>}
    {!catalog && !error && <p role="status">正在读取公开教学目录…</p>}
    {catalog && catalog.targets.length === 0 && <p>当前没有可关联的公开教学。</p>}
    <label>教学目标<select value={target} onChange={event => setTarget(event.target.value)}><option value="">请选择目标</option>{catalog?.targets.map(item => <option key={item.slug} value={item.slug}>{item.name_zh}</option>)}</select></label>
    {target && choices.target !== target && <p role="status">正在读取教学目录…</p>}
    {choices.target === target && choices.error && <p role="alert">{choices.error}</p>}
    {choices.target === target && choices.value?.origins.map(choice => <OriginVariants key={choice.lineup.id} choice={choice} targetName={choices.value!.target.name_zh} selected={selected} onToggle={onToggle} />)}
  </section>;
}
function OriginVariants({ choice, targetName, selected, onToggle }: { choice: OriginChoice; targetName: string; selected: string[]; onToggle: (id: string) => void }) {
  const [open, setOpen] = useState(false); const [variants, setVariants] = useState<LineupSummary[] | null>(null); const [error, setError] = useState('');
  useEffect(() => {
    if (!open) return;
    const controller = new AbortController();
    publicApi.detail(choice.lineup.id, controller.signal).then(value => { if (!controller.signal.aborted) setVariants(value.other_lineups.filter(item => item.id !== choice.lineup.id)); }).catch(() => { if (!controller.signal.aborted) setError('暂时无法读取该起点其他路线，请收起后重试。'); });
    return () => controller.abort();
  }, [open, choice.lineup.id]);
  const checkbox = (lineup: LineupSummary) => <label key={lineup.id}><input type="checkbox" checked={selected.includes(lineup.id)} disabled={selected.length >= 10 && !selected.includes(lineup.id)} onChange={() => onToggle(lineup.id)} />{choice.origin.name_zh} → {targetName} · {lineup.variant_hint}</label>;
  return <div>{checkbox(choice.lineup)}<button className="button" onClick={() => { setError(''); setOpen(value => !value); }}>{open ? '收起其他路线' : `展开${choice.origin.name_zh}其他路线`}</button>{open && <>{error ? <p role="alert">{error}</p> : variants ? variants.length ? variants.map(checkbox) : <p>此起点没有其他公开路线。</p> : <p role="status">正在读取其他路线…</p>}</>}</div>;
}
export function SavedTeachingReferences({ tacticId, ids, revision, resolver, scope }: { tacticId: string; ids: string[]; revision: number; resolver?: TeachingResolver; scope?: string }) {
  return <section aria-label="阶段关联教学" onPlayCapture={event => {
    const playing = event.target;
    const region = event.currentTarget.closest('.tactic-editor') ?? event.currentTarget;
    for (const video of region.querySelectorAll('video')) if (video !== playing) video.pause();
  }}><h3>关联教学</h3>{ids.length ? ids.map((id, index) => <div key={id}><h4>教学 {index + 1}</h4><TeachingCard tacticId={tacticId} id={id} revision={revision} resolver={resolver} scope={scope} /></div>) : <p>此阶段尚未关联教学。</p>}</section>;
}
export default function StageTeachingReferences({ client, stage, revision, editable }: { client: TacticCollaborationClient; stage: TacticStage; revision: number; editable: boolean }) {
  const [draft, setDraft] = useState<{ stageId: string; ids: string[]; revision: number } | null>(null);
  const [picker, setPicker] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  useEffect(() => { client.setLocalDraft(draft ? { stage_id: draft.stageId, lineup_ids: draft.ids, expected_revision: draft.revision } : null, 'lineups'); }, [client, draft]);
  const selected = draft?.ids ?? stage.lineup_ids;
  const wrongStage = draft && draft.stageId !== stage.id;
  function update(ids: string[]) { setDraft(current => ({ stageId: current?.stageId ?? stage.id, revision: current?.revision ?? revision, ids })); }
  async function save() {
    if (!draft || wrongStage) return;
    setBusy(true); setError('');
    try { await client.command({ type: 'stage.lineups.set', stage_id: draft.stageId, lineup_ids: draft.ids }, undefined, draft.revision); setDraft(null); setPicker(false); }
    catch (failure) { setError(failure instanceof Error ? failure.message : '关联教学未保存，选择已保留。'); }
    finally { setBusy(false); }
  }
  return <section className="tactic-teaching-references"><SavedTeachingReferences tacticId={client.tacticId} ids={stage.lineup_ids} revision={revision} />
    {draft && <p>关联教学选择尚未确认保存。</p>}{wrongStage && <p role="alert">此选择仍属于原阶段，请返回原阶段提交或取消；不会自动关联到当前阶段。</p>}{error && <p role="alert">{error}</p>}
    {editable && <><button className="button" disabled={busy || Boolean(wrongStage)} onClick={() => setPicker(value => !value)}>{picker ? '关闭教学选择器' : '添加关联教学'}</button>
      {picker && !wrongStage && <TeachingPicker selected={selected} onToggle={id => update(selected.includes(id) ? selected.filter(value => value !== id) : [...selected, id])} />}
      <ol>{selected.map((id, index) => <li key={id}>教学 {index + 1}{draft && !stage.lineup_ids.includes(id) ? '（待新增）' : ''}<button className="button" disabled={busy || Boolean(wrongStage)} onClick={() => update(selected.filter(value => value !== id))}>移除教学 {index + 1}</button><button className="button" disabled={busy || Boolean(wrongStage) || index === 0} onClick={() => { const ids = [...selected]; [ids[index - 1], ids[index]] = [ids[index], ids[index - 1]]; update(ids); }}>教学上移</button></li>)}</ol>
      {draft && <><button className="button" disabled={busy || Boolean(wrongStage)} onClick={() => void save()}>保存关联教学</button><button className="button" disabled={busy} onClick={() => { setDraft(null); setPicker(false); setError(''); }}>取消关联选择</button></>}
    </>}
  </section>;
}
