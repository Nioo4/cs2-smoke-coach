import { useEffect, useRef, useState } from 'react';
import { ApiError } from '../api/client';
import { accountRequest, type Session } from './api';
import { useTeamWrite } from './teamApi';
import { shareError, type IssuedShare, type ShareSummary } from './shareApi';
import WorkshopDialog from './WorkshopDialog';
interface Source { id: string; title: string; revision: number }
interface Intent { shareId?: string; shareLabel?: string; tacticId: string | null; allowCopy: boolean }
function shareScope(share: ShareSummary, current: string | null) {
  return share.tactic_id === null ? '整本分享' : current ? share.tactic_id === current ? '当前战术' : '本战术本其他战术' : '本战术本单战术';
}
function shareLabel(share: ShareSummary, current: string | null) { return `${shareScope(share, current)} · 分享编号 ${share.id}`; }
export default function ShareManagement({ notebookId, tacticId = null, session, disabled = false, suspended = false }: { notebookId: string; tacticId?: string | null; session: Session; disabled?: boolean; suspended?: boolean }) {
  const [active, setActive] = useState(false);
  const trigger = useRef<HTMLButtonElement>(null);
  const [refresh, setRefresh] = useState(0); const [cursor, setCursor] = useState<string | null>(null);
  const key = JSON.stringify([notebookId, refresh, cursor]);
  const [list, setList] = useState<{ key: string; items?: ShareSummary[]; next?: string | null; error?: string }>({ key: '' });
  const [review, setReview] = useState<{ sources: Source[]; intent: Intent } | null>(null); const [loading, setLoading] = useState(false); const [error, setError] = useState(''); const [issued, setIssued] = useState<IssuedShare | null>(null); const [revoke, setRevoke] = useState<{ id: string; label: string } | null>(null);
  const alive = useRef(false); const attempt = useRef(0);
  useEffect(() => { alive.current = true; return () => { alive.current = false; }; }, []);
  const write = useTeamWrite(session.csrf_token, (value, path) => {
    setReview(null); setRevoke(null); setRefresh(count => count + 1);
    if (path === 'shares') setIssued(value as IssuedShare);
    else if (issued && path.endsWith(`${issued.id}/revoke`)) setIssued(null);
  }, failure => { if (failure instanceof ApiError && failure.code === 'SHARE_SOURCE_CHANGED') setReview(null); });
  useEffect(() => {
    if (!active) return;
    const controller = new AbortController(); const params = new URLSearchParams({ limit: '20' }); if (cursor) params.set('cursor', cursor);
    accountRequest<{ items: ShareSummary[]; next_cursor: string | null }>(`notebooks/${notebookId}/shares?${params}`, 'GET', undefined, undefined, controller.signal)
      .then(value => { if (!controller.signal.aborted) setList({ key, items: value.items, next: value.next_cursor }); }).catch(failure => { if (!controller.signal.aborted) setList({ key, error: shareError(failure) }); });
    return () => controller.abort();
  }, [notebookId, refresh, cursor, key, active]);
  async function prepare(intent: Intent) {
    if (loading || disabled) return; const request = ++attempt.current; setLoading(true); setReview(null); setError('');
    try {
      const sources: Source[] = [];
      if (intent.tacticId) {
        const value = await accountRequest<{ tactic: Source & { notebook_id: string }; permissions: { publish: boolean } }>(`tactics/${intent.tacticId}`);
        if (value.tactic.notebook_id !== notebookId || !value.permissions.publish) throw Error('no_permission'); sources.push(value.tactic);
      } else {
        let next: string | null = null;
        do {
          const params = new URLSearchParams({ deleted: 'false', limit: '50' }); if (next) params.set('cursor', next);
          const page: { items: Source[]; next_cursor: string | null; permissions: { publish: boolean } } = await accountRequest(`notebooks/${notebookId}/tactics?${params}`);
          if (!page.permissions.publish) throw Error('no_permission'); sources.push(...page.items); next = page.next_cursor;
        } while (next && alive.current && request === attempt.current);
      }
      if (!alive.current || request !== attempt.current) return;
      if (!sources.length) { setError('战术本为空，不能发布。'); return; }
      setReview({ sources, intent });
    } catch (failure) { if (alive.current && request === attempt.current) setError(failure instanceof ApiError ? shareError(failure) : '暂时无法确认发布权限或清单，请刷新后重试。'); }
    finally { if (alive.current && request === attempt.current) setLoading(false); }
  }
  function publish() {
    if (!review || disabled) return;
    const body = { allow_copy: review.intent.allowCopy, expected_revisions: review.sources.map(source => ({ id: source.id, revision: source.revision })) };
    write.write(review.intent.shareId ? `shares/${review.intent.shareId}/publish` : 'shares', 'POST', review.intent.shareId ? body : { ...body, notebook_id: notebookId, tactic_id: review.intent.tacticId });
  }
  const blocked = write.busy || write.uncertain || loading || disabled;
  const url = issued?.token_available && issued.token ? `${location.origin}/share#token=${issued.token}` : '';
  return <><button ref={trigger} className="button" disabled={disabled} onClick={() => setActive(true)}>打开发布分享管理</button>{active && <WorkshopDialog title="发布与固定版本分享" returnFocus={trigger} suspended={suspended} onClose={() => setActive(false)}><section className="workshop-publication" aria-label="发布分享管理"><h3>发布分享</h3><p>仅发布已保存内容。新草稿不会自动改变分享；默认不允许复制。</p>
    {review?.intent.shareLabel && <p role="status">正在准备更新：{review.intent.shareLabel}。请核对下方实际来源和保存版本。</p>}
    <div className="workshop-publish-layout"><div>
    <button className="button" disabled={blocked || Boolean(issued)} onClick={() => void prepare({ tacticId, allowCopy: false })}>{tacticId ? '准备发布此战术' : '准备发布整本战术'}</button>
    {(error || write.error) && <p role="alert">{error || write.error}</p>}{write.uncertain && <button className="button" disabled={write.busy} onClick={write.retry}>按原请求核对发布结果</button>}{loading && <p role="status">正在读取全部已保存发布清单…</p>}
    {review && <section aria-label="发布确认清单"><h4>请核对即将公开的保存版本</h4><ul>{review.sources.map(source => <li key={source.id}>{source.title} · 版本 {source.revision}</li>)}</ul><label><input type="checkbox" disabled={blocked} checked={review.intent.allowCopy} onChange={event => setReview(value => value && ({ ...value, intent: { ...value.intent, allowCopy: event.target.checked } }))} />允许登录用户复制独立副本</label><button className="button button-primary" disabled={blocked} onClick={publish}>确认发布这些保存版本</button><button className="button" disabled={write.busy} onClick={() => setReview(null)}>取消发布</button></section>}
    {issued && (url ? <section><p>分享链接只显示这一次，请自行保存后分享。</p><label>分享链接<input readOnly value={url} onFocus={event => event.currentTarget.select()} /></label><button className="button" onClick={() => setIssued(null)}>已保存分享链接</button></section> : <section><p>分享已创建，但原链接不能再次取回。请撤销后创建新链接。</p><button className="button" disabled={blocked} onClick={() => setRevoke({ id: issued.id, label: shareLabel(issued, tacticId) })}>撤销无法取回链接的分享</button></section>)}
    </div><aside><h3>已发布的分享</h3><p>这里管理本战术本的全部分享，包含其他战术和整本分享。请核对范围、时间与编号后操作。</p>{list.key !== key ? <p role="status">正在读取已发布分享…</p> : list.error ? <p role="alert">{list.error}</p> : <ul>{list.items?.map(share => <li key={share.id}>
      <strong>{shareScope(share, tacticId)}</strong><p>分享编号：{share.id}</p>
      {share.created_at && <p>创建于 <time dateTime={share.created_at}>{new Date(share.created_at).toLocaleString()}</time></p>}
      {share.updated_at && <p>更新于 <time dateTime={share.updated_at}>{new Date(share.updated_at).toLocaleString()}</time></p>}
      <p>{share.allow_copy ? '允许复制' : '禁止复制'} · {share.revoked_at ? '已撤销' : '未撤销'}</p>
      {!share.revoked_at && <><button className="button" aria-label={`准备更新：${shareLabel(share, tacticId)}`} disabled={blocked || Boolean(issued)} onClick={() => void prepare({ shareId: share.id, shareLabel: shareLabel(share, tacticId), tacticId: share.tactic_id, allowCopy: share.allow_copy })}>准备更新此分享</button><button className="button" aria-label={`撤销：${shareLabel(share, tacticId)}`} disabled={blocked} onClick={() => setRevoke({ id: share.id, label: shareLabel(share, tacticId) })}>撤销此分享</button></>}
    </li>)}</ul>}
    {list.next && <button className="button" disabled={blocked} onClick={() => setCursor(list.next ?? null)}>下一页分享</button>}{cursor && <button className="button" disabled={blocked} onClick={() => setCursor(null)}>分享第一页</button>}<button className="button" disabled={blocked} onClick={() => setRefresh(count => count + 1)}>刷新分享列表</button>
    </aside></div>
    {revoke && <section role="alertdialog" aria-label="确认撤销分享"><p>即将撤销：{revoke.label}</p><p>撤销后不能继续访问或复制，已经下载或复制的内容无法收回。</p><button className="button" disabled={blocked} onClick={() => write.write(`shares/${revoke.id}/revoke`, 'POST', {})}>确认撤销分享</button><button className="button" disabled={write.busy} onClick={() => setRevoke(null)}>取消</button></section>}
  </section></WorkshopDialog>}</>;
}
