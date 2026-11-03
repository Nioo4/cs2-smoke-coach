import { useEffect, useRef, useState } from 'react';
import { Link } from 'react-router-dom';
import { accountRequest, type Notebook, type Session } from './api';
import { shareError, type HistoryVersion } from './shareApi';
import { useTeamWrite, type Team } from './teamApi';
import TacticPlaybackPanel from './TacticPlaybackPanel';
import type { TacticProjection } from './editorTypes';
import WorkshopDialog from './WorkshopDialog';
export default function TacticHistory({ tacticId, session, suspended = false }: { tacticId: string; session: Session; suspended?: boolean }) {
  const trigger = useRef<HTMLButtonElement>(null);
  const [open, setOpen] = useState(false); const [cursor, setCursor] = useState<string | null>(null); const [attempt, setAttempt] = useState(0); const [selected, setSelected] = useState<string | null>(null);
  const key = JSON.stringify([tacticId, cursor, attempt]);
  const [result, setResult] = useState<{ key: string; items?: HistoryVersion[]; next?: string | null; error?: string }>({ key: '' });
  useEffect(() => {
    if (!open) return;
    const controller = new AbortController(); const params = new URLSearchParams({ limit: '20' }); if (cursor) params.set('cursor', cursor);
    accountRequest<{ items: HistoryVersion[]; next_cursor: string | null }>(`tactics/${tacticId}/versions?${params}`, 'GET', undefined, undefined, controller.signal).then(value => { if (!controller.signal.aborted) setResult({ key, items: value.items, next: value.next_cursor }); }).catch(failure => { if (!controller.signal.aborted) setResult({ key, error: shareError(failure) }); });
    return () => controller.abort();
  }, [open, tacticId, cursor, attempt, key]);
  return <section aria-label="战术历史"><button ref={trigger} className="button" onClick={() => { if (!open) setAttempt(count => count + 1); setOpen(value => !value); }}>{open ? '收起历史版本' : '查看历史版本'}</button>{open && <WorkshopDialog title="历史版本" returnFocus={trigger} suspended={suspended} onClose={() => setOpen(false)}><p>仅保留30天内、最多100份历史；恢复始终生成新副本，不覆盖当前正文。</p><div className="workshop-history-layout"><aside>{result.key !== key ? <p role="status">正在读取历史…</p> : result.error ? <p role="alert">{result.error}</p> : <><ul>{result.items?.map(version => <li key={version.id} className={selected === version.id ? 'is-selected' : undefined}>版本 {version.source_revision} · {({ archive: '归档前', publish: '发布前', periodic: '定期保存' })[version.reason]} · {new Date(version.created_at).toLocaleString()}<button className="button" aria-pressed={selected === version.id} onClick={() => setSelected(version.id)}>查看此历史快照</button></li>)}</ul>{!result.items?.length && <p>没有可访问的历史版本。</p>}{result.next && <button className="button" onClick={() => setCursor(result.next ?? null)}>下一页历史</button>}{cursor && <button className="button" onClick={() => setCursor(null)}>历史第一页</button>}</>}<button className="button" onClick={() => { setSelected(null); setAttempt(count => count + 1); }}>刷新历史</button></aside><div>{selected ? <HistoryDetail key={`${tacticId}:${selected}`} tacticId={tacticId} versionId={selected} session={session} /> : <p>选择左侧历史版本，只读查看后再决定是否恢复为新副本。</p>}</div></div></WorkshopDialog>}</section>;
}
function HistoryDetail({ tacticId, versionId, session }: { tacticId: string; versionId: string; session: Session }) {
  const [result, setResult] = useState<{ version?: HistoryVersion; projection?: TacticProjection; error?: string }>({}); const [targets, setTargets] = useState<{ id: string; label: string }[] | null>(null); const [target, setTarget] = useState(''); const [copied, setCopied] = useState<string | null>(null);
  const write = useTeamWrite(session.csrf_token, value => setCopied((value as { id: string }).id));
  useEffect(() => {
    const controller = new AbortController();
    async function load() {
      try {
        const detail = await accountRequest<{ version: HistoryVersion; projection: TacticProjection }>(`tactics/${tacticId}/versions/${versionId}`, 'GET', undefined, undefined, controller.signal);
        if (!controller.signal.aborted) setResult(detail);
        const personal = await accountRequest<{ notebook: Notebook }>('notebooks/personal', 'GET', undefined, undefined, controller.signal); const all: Team[] = []; let cursor: string | null = null;
        do { const params = new URLSearchParams({ limit: '50' }); if (cursor) params.set('cursor', cursor); const page: { items: Team[]; next_cursor: string | null } = await accountRequest(`teams?${params}`, 'GET', undefined, undefined, controller.signal); all.push(...page.items); cursor = page.next_cursor; } while (cursor && !controller.signal.aborted);
        if (!controller.signal.aborted) setTargets([{ id: personal.notebook.id, label: '我的个人战术本' }, ...all.filter(team => team.my_role !== 'viewer').map(team => ({ id: team.notebook_id, label: team.name }))]);
      } catch (failure) { if (!controller.signal.aborted) setResult({ error: shareError(failure) }); }
    }
    void load(); return () => controller.abort();
  }, [tacticId, versionId]);
  return <section aria-label="历史快照详情">{result.error ? <p role="alert">{result.error}</p> : result.version && result.projection ? <><h4>历史保存版本 {result.version.source_revision}</h4><TacticPlaybackPanel confirmed={result.projection} revision={result.version.source_revision} epoch={result.version.source_epoch} hasUnconfirmedWork={false} />{targets ? <><label>历史副本目标<select value={target} disabled={write.busy || write.uncertain} onChange={event => setTarget(event.target.value)}><option value="">请选择目标战术本</option>{targets.map(item => <option value={item.id} key={item.id}>{item.label}</option>)}</select></label><button className="button" disabled={!target || write.busy || write.uncertain} onClick={() => write.write(`tactics/${tacticId}/versions/${versionId}/restore-copy`, 'POST', { target_notebook_id: target })}>恢复为新战术副本</button></> : <p role="status">正在读取副本目标…</p>}</> : <p role="status">正在读取真实历史快照…</p>}{write.error && <p role="alert">{write.error}</p>}{write.uncertain && <button className="button" disabled={write.busy} onClick={write.retry}>按原请求核对历史副本</button>}{copied && <Link to={`/tactics/${copied}`}>打开恢复的新副本</Link>}</section>;
}
