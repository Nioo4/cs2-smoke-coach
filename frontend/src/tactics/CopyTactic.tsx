import { useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import { accountRequest, type Notebook, type Session } from './api';
import { useTeamWrite, type Team } from './teamApi';
interface Attribution { tactic_id: string; title: string; author_display_name: string; copied_at: string }
export function SourceAttribution({ tacticId }: { tacticId: string }) {
  const [attempt, setAttempt] = useState(0);
  const key = JSON.stringify([tacticId, attempt]);
  const [result, setResult] = useState<{ key: string; source?: Attribution | null; error?: string }>({ key: '' });
  useEffect(() => {
    const controller = new AbortController();
    accountRequest<{ tactic: { source: Attribution | null } }>(`tactics/${tacticId}`, 'GET', undefined, undefined, controller.signal).then(value => { if (!controller.signal.aborted) setResult({ key, source: value.tactic?.source ?? null }); }).catch(() => { if (!controller.signal.aborted) setResult({ key, error: '暂时无法读取来源署名。' }); });
    return () => controller.abort();
  }, [tacticId, key]);
  if (result.key !== key) return <p role="status">正在读取来源署名…</p>;
  return result.error ? <section><p role="alert">{result.error}</p><button className="button" onClick={() => setAttempt(value => value + 1)}>重试读取来源署名</button></section> : result.source ? <p>来源：{result.source.author_display_name}的“{result.source.title}”，复制于{new Date(result.source.copied_at).toLocaleDateString()}。此副本独立保存。</p> : null;
}
export default function CopyTactic({ tacticId, session }: { tacticId: string; session: Session }) {
  const [open, setOpen] = useState(false); const [attempt, setAttempt] = useState(0); const [target, setTarget] = useState(''); const [copied, setCopied] = useState<string | null>(null);
  const [catalog, setCatalog] = useState<{ attempt: number; targets?: { id: string; label: string }[]; error?: string }>({ attempt: -1 });
  const write = useTeamWrite(session.csrf_token, value => setCopied((value as { id: string }).id));
  useEffect(() => {
    if (!open) return;
    const controller = new AbortController();
    async function load() {
      try {
        const personal = await accountRequest<{ notebook: Notebook }>('notebooks/personal', 'GET', undefined, undefined, controller.signal);
        const all: Team[] = []; let cursor: string | null = null;
        do {
          const params = new URLSearchParams({ limit: '50' }); if (cursor) params.set('cursor', cursor);
          const page: { items: Team[]; next_cursor: string | null } = await accountRequest(`teams?${params}`, 'GET', undefined, undefined, controller.signal); all.push(...page.items); cursor = page.next_cursor;
        } while (cursor && !controller.signal.aborted);
        if (!controller.signal.aborted) setCatalog({ attempt, targets: [{ id: personal.notebook.id, label: '我的个人战术本' }, ...all.filter(team => team.my_role === 'owner' || team.my_role === 'editor').map(team => ({ id: team.notebook_id, label: team.name }))] });
      } catch { if (!controller.signal.aborted) setCatalog({ attempt, error: '暂时无法读取可复制的战术本，请确认登录有效后重试。' }); }
    }
    void load(); return () => controller.abort();
  }, [open, attempt]);
  return <section aria-label="复制战术"><button className="button" onClick={() => { if (!open) { setAttempt(value => value + 1); setTarget(''); } setOpen(value => !value); }}>{open ? '收起复制选项' : '复制已保存战术'}</button>{open && <>
    <p>复制当前已确认版本，生成独立副本并保留来源署名，不会复制尚未确认的编辑。</p>{write.error && <p role="alert">{write.error}</p>}{write.uncertain && <button className="button" disabled={write.busy} onClick={write.retry}>按原请求核对副本</button>}
    {catalog.attempt !== attempt ? <p role="status">正在读取可复制的战术本…</p> : catalog.error ? <><p role="alert">{catalog.error}</p><button className="button" onClick={() => { setTarget(''); setAttempt(value => value + 1); }}>重试读取战术本</button></> : <><label>目标战术本<select disabled={write.busy || write.uncertain} value={target} onChange={event => setTarget(event.target.value)}><option value="">请选择目标</option>{catalog.targets?.map(item => <option key={item.id} value={item.id}>{item.label}</option>)}</select></label><button className="button" disabled={!target || write.busy || write.uncertain} onClick={() => write.write(`tactics/${tacticId}/copy`, 'POST', { target_notebook_id: target })}>确认复制已保存版本</button></>}
    {copied && <p>副本已创建。<Link to={`/tactics/${copied}`}>打开新副本</Link></p>}
  </>}</section>;
}
