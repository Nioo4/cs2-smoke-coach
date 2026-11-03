import { useEffect, useState } from 'react';
import { accountRequest, type Session } from './api';
import ShareManagement from './ShareManagement';
import TacticHistory from './TacticHistory';
export default function TacticPublicationTools({ tacticId, session, hasUnconfirmedWork, connected, connectionId, readOnly, onNotebookId, contextRefresh = 0 }: { tacticId: string; session: Session; hasUnconfirmedWork: boolean; connected: boolean; connectionId: string; readOnly: boolean; onNotebookId?: (id: string | null) => void; contextRefresh?: number }) {
  const [attempt, setAttempt] = useState(0);
  const scope = JSON.stringify([tacticId, session.user.id]);
  const key = JSON.stringify([tacticId, session.user.id, connected, connectionId, readOnly, attempt, contextRefresh]);
  const [result, setResult] = useState<{ key: string; value?: { scope: string; notebookId: string; manage: boolean; publish: boolean }; error?: string }>({ key: '' });
  useEffect(() => {
    if (!connected) return;
    onNotebookId?.(null);
    const controller = new AbortController();
    accountRequest<{ tactic: { notebook_id: string }; permissions: { manage: boolean; publish: boolean } }>(`tactics/${tacticId}`, 'GET', undefined, undefined, controller.signal).then(value => { if (!controller.signal.aborted) { const id = value.tactic.notebook_id; if (typeof id !== 'string' || !id) throw Error('invalid_notebook'); setResult({ key, value: { scope, notebookId: id, manage: value.permissions.manage, publish: value.permissions.publish } }); onNotebookId?.(id); } }).catch(() => { if (!controller.signal.aborted) { onNotebookId?.(null); setResult(previous => ({ key, value: previous.value?.scope === scope ? previous.value : undefined, error: '暂时无法读取发布与历史权限，请重试。' })); } });
    return () => controller.abort();
  }, [tacticId, connected, key, scope, onNotebookId]);
  const loaded = result.key === key;
  const available = connected && loaded && !result.error;
  const value = result.value?.scope === scope ? result.value : undefined;
  return <section>{connected && !loaded && <p role="status">正在核对发布与历史权限…</p>}{connected && loaded && result.error && <><p role="alert">{result.error}</p><button className="button" onClick={() => setAttempt(value => value + 1)}>重试读取发布与历史权限</button></>}
    {/* Keep same-account one-time links and uncertain writes mounted during a transient recheck. */}
    <div hidden={!available}>{value?.publish && <>{hasUnconfirmedWork && <p>请先处理未确认内容，再准备发布。</p>}<ShareManagement notebookId={value.notebookId} tacticId={tacticId} session={session} disabled={hasUnconfirmedWork || !available} suspended={!available} /></>}{value?.manage && <TacticHistory tacticId={tacticId} session={session} suspended={!available} />}</div></section>;
}
