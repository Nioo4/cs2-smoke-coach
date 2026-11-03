import { useEffect, useRef, useState, type FormEvent, type RefObject } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import { ApiError } from '../api/client';
import { accountRequest, type Notebook as NotebookInfo, type Session } from './api';
import type { NotebookPermissions } from './teamApi';
import CopyTactic from './CopyTactic';
import ShareManagement from './ShareManagement';
import WorkshopDialog from './WorkshopDialog';
import TacticPlaybackPanel from './TacticPlaybackPanel';
import type { TacticProjection } from './editorTypes';

interface TacticSummary {
  id: string; notebook_id: string; title: string; side: 'T' | 'CT'; map: string;
  revision: number; updated_at: string; archived_at: string | null;
  permissions?: { read: boolean; edit: boolean; manage: boolean; publish: boolean };
}
interface TacticView {
  tactic: TacticSummary; epoch: string;
  projection: TacticProjection;
  permissions: { read: boolean; edit: boolean; manage: boolean; publish: boolean };
}
function explain(err: unknown) {
  if (!(err instanceof ApiError)) return '未收到完整结果，操作可能已成功。请刷新列表核对后再操作，避免重复创建。';
  if (err.status === 401) return '登录已失效，请重新登录。';
  if (err.status === 404) return '找不到该战术，可能已被移入已归档或不属于当前账户。';
  if (err.status === 403) return '当前账户没有执行此操作的权限，请重新登录后确认。';
  if (err.status === 503) return '战术服务暂时不可用，请稍后刷新查看。';
  return err.message;
}

export default function Notebook({ notebook, session, tacticId }: { notebook: NotebookInfo; session: Session; tacticId?: string }) {
  const navigate = useNavigate();
  const activeLabel = notebook.kind === 'team' ? '共享战术' : '我的战术';
  const [deleted, setDeleted] = useState(false);
  const [query, setQuery] = useState('');
  const [cursor, setCursor] = useState<string | null>(null);
  const [nextCursor, setNextCursor] = useState<string | null>(null);
  const [items, setItems] = useState<TacticSummary[]>([]);
  const [permissions, setPermissions] = useState<NotebookPermissions | null>(null);
  const [view, setView] = useState<TacticView | null>(null);
  const [loadedKey, setLoadedKey] = useState('');
  const [busy, setBusy] = useState(false);
  const [uncertain, setUncertain] = useState(false);
  const [error, setError] = useState('');
  const [refresh, setRefresh] = useState(0);
  const [confirmArchive, setConfirmArchive] = useState<string | null>(null);
  const [archivedId, setArchivedId] = useState<string | null>(null);
  const [createOpen, setCreateOpen] = useState(false);
  const createTrigger = useRef<HTMLButtonElement>(null);
  const archiveTrigger = useRef<HTMLButtonElement>(null);
  const requestKey = JSON.stringify([notebook.id, tacticId, query, cursor, deleted, refresh]);
  const loading = loadedKey !== requestKey;
  const alive = useRef(false);
  const inFlight = useRef(false);
  useEffect(() => { alive.current = true; return () => { alive.current = false; }; }, []);
  useEffect(() => {
    const controller = new AbortController();
    const params = new URLSearchParams({ deleted: String(deleted), q: query, limit: '20' });
    if (cursor) params.set('cursor', cursor);
    const path = tacticId ? `tactics/${encodeURIComponent(tacticId)}` : `notebooks/${notebook.id}/tactics?${params}`;
    accountRequest<TacticView | { items: TacticSummary[]; next_cursor: string | null; permissions?: NotebookPermissions }>(path, 'GET', undefined, undefined, controller.signal)
      .then(result => {
        if (controller.signal.aborted) return;
        setUncertain(false);
        setError('');
        if ('tactic' in result) setView(result);
        else { setItems(result.items); setNextCursor(result.next_cursor); setPermissions(result.permissions ?? null); }
      })
      .catch((err: unknown) => {
        if (!controller.signal.aborted) { setItems([]); setView(null); setNextCursor(null); setPermissions(null); setError(explain(err)); }
      })
      .finally(() => { if (!controller.signal.aborted) setLoadedKey(requestKey); });
    return () => controller.abort();
  }, [notebook.id, tacticId, query, cursor, deleted, refresh, requestKey]);

  async function mutate(path: string, body: object, create = false) {
    if (inFlight.current || uncertain) return;
    inFlight.current = true; setBusy(true); setError('');
    try {
      const result = await accountRequest<TacticView | TacticSummary>(path, 'POST', body, session.csrf_token, undefined, crypto.randomUUID());
      if (!alive.current) return;
      setConfirmArchive(null);
      if (create && 'tactic' in result) navigate(`/tactics/${result.tactic.id}`);
      else { setCursor(null); setRefresh(value => value + 1); }
    } catch (err) {
      if (!alive.current) return;
      setError(explain(err));
      if (!(err instanceof ApiError) || err.status >= 500) setUncertain(true);
    } finally { inFlight.current = false; if (alive.current) setBusy(false); }
  }
  function create(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const values = new FormData(event.currentTarget);
    void mutate(`notebooks/${notebook.id}/tactics`, { title: values.get('title'), side: values.get('side') }, true);
  }
  const blocked = busy || uncertain;
  const createVisible = !loading && !deleted && permissions?.create && createOpen;
  return <section aria-label={notebook.kind === 'team' ? '队伍战术目录' : '个人战术目录'}>
    {archivedId && <ArchivedTactic key={archivedId} id={archivedId} onClose={() => setArchivedId(null)} returnFocus={archiveTrigger} />}
    <header className="workshop-notebook-heading"><h2>{tacticId ? view?.tactic.title ?? '战术详情' : deleted ? '已归档' : activeLabel}</h2><div>
    {!tacticId && !deleted && permissions?.create && <button ref={createTrigger} className="button button-primary" disabled={blocked || loading} onClick={() => setCreateOpen(true)}>新建战术</button>}
    <button className="button" disabled={busy} onClick={() => { setRefresh(value => value + 1); }}>刷新{tacticId ? '详情' : '列表'}</button>
    </div></header>
    {error && !loading && !createVisible && <p role="alert">{error}</p>}
    {uncertain && <p>写入结果待确认；请刷新核对现有战术，再继续操作。</p>}
    {loading ? <p role="status">正在加载战术…</p> : tacticId ? <>
      <Link to="/tactics">返回战术目录</Link>
      {view && <p><Link to={`/tactics/${view.tactic.id}/present`}>演示已保存版本</Link></p>}
      {view && <><p>{view.tactic.side} · Mirage · 已保存版本 {view.tactic.revision}</p><p>此处只读查看已保存内容，可刷新详情或打开已保存版本演示。</p><h3>战术目的</h3><p>{view.projection.fields['tactic.purpose'] || '尚未填写'}</p><h3>适用情况</h3><p>{view.projection.fields['tactic.applicability'] || '尚未填写'}</p>{Object.entries(view.projection.fields).filter(([key]) => /^stage\.[^.]+\.name$/.test(key)).map(([key, name]) => <section key={key}><h3>{name || '未命名阶段'}</h3><p>{view.projection.fields[key.replace(/name$/, 'description')] || '尚未填写阶段说明'}</p><p>{view.projection.fields[key.replace(/name$/, 'condition')] || '尚未填写进入条件'}</p></section>)}</>}
    </> : <>
      {permissions?.publish && <ShareManagement notebookId={notebook.id} session={session} />}
      <div className="workshop-notebook-toolbar"><form className="admin-form workshop-notebook-search" onSubmit={event => { event.preventDefault(); setQuery(String(new FormData(event.currentTarget).get('q') ?? '')); setCursor(null); }}><label>搜索战术名称<input name="q" maxLength={100} defaultValue={query} /></label><button className="button" disabled={busy}>搜索</button></form>
      <nav aria-label="目录分类"><button className="button" disabled={busy} aria-pressed={!deleted} onClick={() => { setDeleted(false); setCursor(null); }}>{activeLabel}</button>{permissions?.manage && <button className="button" disabled={busy} aria-pressed={deleted} onClick={() => { setDeleted(true); setCursor(null); }}>已归档</button>}</nav></div>
      {deleted ? <p>归档的战术可在30天内恢复。恢复后不会自动重新开放旧分享。</p> : permissions?.create && createOpen && <WorkshopDialog title="新建战术" returnFocus={createTrigger} onClose={() => { if (!blocked) setCreateOpen(false); }}><p>归属：{notebook.kind === 'personal' ? '我的个人战术本' : '当前队伍共享战术本'}。地图：Mirage。</p><form className="admin-form" onSubmit={create}><fieldset disabled={blocked}><legend>创建战术</legend><label>战术名称<input name="title" required maxLength={100} /></label><label>阵营<select name="side" defaultValue="T"><option value="T">T</option><option value="CT">CT</option></select></label><button className="button button-primary">创建战术</button></fieldset></form>{error && <p role="alert">{error}</p>}{uncertain && <p>创建结果尚未确认，请先刷新目录核对，避免重复创建。</p>}<button className="button" disabled={busy} onClick={() => setRefresh(value => value + 1)}>核对目录</button></WorkshopDialog>}
      {!items.length && !error && <p>{query ? '没有匹配的战术。' : deleted ? '已归档为空。' : permissions?.create ? '还没有战术，创建第一份战术吧。' : '还没有可查看的战术。'}</p>}
      <ul className="tactic-list">{items.map(item => <li key={item.id}>
        <p>{item.side} · Mirage · 已保存版本 {item.revision}</p><h3>{deleted ? item.title : <Link to={`/tactics/${item.id}`}>{item.title}</Link>}</h3>{item.updated_at && <time dateTime={item.updated_at}>更新于{new Date(item.updated_at).toLocaleString()}</time>}
        {!deleted && <Link className="button button-primary" to={`/tactics/${item.id}`}>{item.permissions?.edit ? '打开编辑' : '查看战术'}</Link>}
        {deleted && item.permissions?.manage && <button className="button" onClick={event => { archiveTrigger.current = event.currentTarget; setArchivedId(item.id); }}>查看归档：{item.title}</button>}
        {item.permissions?.manage && (deleted ? <button className="button" disabled={blocked} onClick={() => void mutate(`tactics/${item.id}/restore`, {})}>恢复 {item.title}</button> : confirmArchive === item.id ? <div><p>将“{item.title}”归档？30天内可恢复。</p><button className="button" disabled={blocked} onClick={() => void mutate(`tactics/${item.id}/archive`, { expected_revision: item.revision })}>确认归档</button><button className="button" disabled={busy} onClick={() => setConfirmArchive(null)}>取消</button></div> : <button className="button" disabled={blocked} onClick={() => setConfirmArchive(item.id)}>归档 {item.title}</button>)}
        {!deleted && item.permissions?.read && <CopyTactic tacticId={item.id} session={session} />}
      </li>)}</ul>
      <nav aria-label="目录分页">{cursor && <button className="button" disabled={busy} onClick={() => setCursor(null)}>返回第一页</button>}{nextCursor && <button className="button" disabled={busy} onClick={() => setCursor(nextCursor)}>下一页</button>}</nav>
    </>}
  </section>;
}

function ArchivedTactic({ id, onClose, returnFocus }: { id: string; onClose: () => void; returnFocus: RefObject<HTMLElement | null> }) {
  const [attempt, setAttempt] = useState(0);
  const [result, setResult] = useState<{ attempt: number; view?: TacticView; error?: string }>({ attempt: -1 });
  useEffect(() => {
    const controller = new AbortController();
    accountRequest<TacticView>(`tactics/${encodeURIComponent(id)}/archived`, 'GET', undefined, undefined, controller.signal)
      .then(view => { if (!controller.signal.aborted) setResult({ attempt, view }); })
      .catch(error => { if (!controller.signal.aborted) setResult({ attempt, error: explain(error) }); });
    return () => controller.abort();
  }, [id, attempt]);
  return <WorkshopDialog title="归档战术只读详情" onClose={onClose} returnFocus={returnFocus}>
    <p>归档内容仅供查看，恢复后才能重新编辑；原分享不会自动恢复。</p>
    {result.attempt !== attempt ? <p role="status">正在读取归档战术…</p> : result.error ? <><p role="alert">{result.error}</p><button className="button" onClick={() => setAttempt(value => value + 1)}>重试读取归档战术</button></> : result.view && <>
      <h3>{result.view.tactic.title} · 已保存版本 {result.view.tactic.revision}</h3>
      <p>{result.view.projection.fields['tactic.purpose'] || '尚未填写目的'}</p>
      <p>{result.view.projection.fields['tactic.applicability'] || '尚未填写适用情况'}</p>
      <TacticPlaybackPanel confirmed={result.view.projection} revision={result.view.tactic.revision} epoch={result.view.epoch} hasUnconfirmedWork={false} />
    </>}
  </WorkshopDialog>;
}
