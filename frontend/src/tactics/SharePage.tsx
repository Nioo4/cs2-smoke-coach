import { useCallback, useEffect, useRef, useState, type FormEvent } from 'react';
import { Link } from 'react-router-dom';
import { ApiError } from '../api/client';
import { accountRequest, type Session } from './api';
import TacticPlaybackPanel from './TacticPlaybackPanel';
import { publicShareRequest, shareError, type Manifest } from './shareApi';
import { useTeamWrite } from './teamApi';
import type { TacticProjection } from './editorTypes';
import type { Reference } from './StageTeachingReferences';
import WorkshopHeader from '../components/WorkshopHeader';
import './workshop-business.css';
import './shared-presentation.css';

interface ItemPage { version_id: string; items: { item_id: string; projection: TacticProjection }[]; next_cursor: string | null }
interface Access { grant: string; manifest: Manifest; cached: ItemPage['items']; cursor: string | null; selected: number }
export default function SharePage({ token }: { token: string | null }) {
  const [attempt, setAttempt] = useState(0);
  const [result, setResult] = useState<{ attempt: number; access?: Access; error?: string }>({ attempt: -1 });
  const [copyAccess, setCopyAccess] = useState<{ grant: string; manifest: Manifest } | null>(null);
  const copyTarget = useRef<HTMLDivElement>(null);
  function revealCopy() { copyTarget.current?.scrollIntoView({ block: 'start' }); copyTarget.current?.focus({ preventScroll: true }); }
  useEffect(() => {
    if (!token) return;
    const controller = new AbortController();
    async function load() {
      try {
        const issued = await publicShareRequest<{ read_grant: string }>('exchange', { token }, controller.signal);
        const manifest = await publicShareRequest<Manifest>('read', { read_grant: issued.read_grant }, controller.signal);
        const first = await publicShareRequest<ItemPage>('items', { read_grant: issued.read_grant, version_id: manifest.version_id, cursor: null }, controller.signal);
        if (first.version_id !== manifest.version_id || first.items.length > 1 || (first.items[0] && first.items[0].item_id !== manifest.items[0]?.item_id)) throw Error('invalid_page');
        if (!controller.signal.aborted) { setResult({ attempt, access: { grant: issued.read_grant, manifest, cached: first.items, cursor: first.next_cursor, selected: 0 } }); setCopyAccess({ grant: issued.read_grant, manifest }); }
      } catch (error) { if (!controller.signal.aborted) setResult({ attempt, error: shareError(error) }); }
    }
    void load(); return () => controller.abort();
  }, [token, attempt]);
  const refreshAccess = token ? () => setAttempt(value => value + 1) : undefined;
  const readable = Boolean(token && result.attempt === attempt && !result.error && result.access);
  return <><WorkshopHeader /><main className="page-shell tactics-account tactics-presentation-page workshop-business shared-presentation">
    {!readable && <ShareHeader onRefresh={refreshAccess} />}
    {!token ? <p>没有有效分享令牌。请重新打开作者提供的完整分享链接；刷新后不会保留已移除的令牌。</p> : result.attempt !== attempt ? <p role="status">正在读取发布版本…</p> : result.error ? <p role="alert">{result.error}</p> : result.access && <ShareReader key={`${attempt}:${result.access.manifest.version_id}`} initial={result.access} onRefresh={refreshAccess} onCopy={revealCopy} onUnavailable={error => setResult({ attempt, error })} />}
    {copyAccess && <div id="shared-copy" ref={copyTarget} tabIndex={-1} role="region" aria-label="复制发布版本区域" className="shared-copy"><ShareCopy grant={copyAccess.grant} manifest={copyAccess.manifest} active={result.attempt === attempt && Boolean(result.access)} /></div>}
  </main></>;
}
function ShareHeader({ title = '分享战术演示', revision, copyAllowed = false, onRefresh, onCopy }: { title?: string; revision?: number; copyAllowed?: boolean; onRefresh?: () => void; onCopy?: () => void }) {
  return <header className="shared-presentation-header"><div className="shared-presentation-identity"><p className="eyebrow">READ-ONLY SHARED TACTIC · 只读分享</p><div className="shared-presentation-title"><h1 title={title}>{title}</h1>{revision !== undefined && <span className="shared-presentation-version">作者发布的保存版本 {revision}</span>}</div><p>这是作者手动发布的只读版本，草稿修改不会自动改变本页。</p></div>
    <div className="shared-presentation-actions">{copyAllowed && <button className="button" onClick={onCopy}>复制为独立战术</button>}{onRefresh && <button className="button" onClick={onRefresh}>刷新访问凭据和发布版本</button>}<Link className="button" to="/">返回教学页</Link></div>
  </header>;
}
function ShareReader({ initial, onUnavailable, onRefresh, onCopy }: { initial: Access; onUnavailable: (message: string) => void; onRefresh?: () => void; onCopy: () => void }) {
  const [access, setAccess] = useState(initial); const [busy, setBusy] = useState(false); const [error, setError] = useState(''); const [stale, setStale] = useState(false);
  const alive = useRef(false); useEffect(() => { alive.current = true; return () => { alive.current = false; }; }, []);
  async function next() {
    if (!access.cursor || busy || stale) return; setBusy(true); setError('');
    try {
      const page = await publicShareRequest<ItemPage>('items', { read_grant: access.grant, version_id: access.manifest.version_id, cursor: access.cursor });
      if (!alive.current) return;
      if (page.version_id !== access.manifest.version_id || page.items.length !== 1 || page.items[0].item_id !== access.manifest.items[access.cached.length]?.item_id) throw Error('invalid_page');
      setAccess(current => ({ ...current, cached: [...current.cached, ...page.items], cursor: page.next_cursor, selected: current.cached.length }));
    } catch (failure) {
      if (!alive.current) return;
      if (failure instanceof ApiError && failure.code === 'SHARE_UNAVAILABLE') onUnavailable(shareError(failure));
      else { setError(shareError(failure)); if (failure instanceof ApiError && failure.code === 'SHARE_VERSION_CHANGED') setStale(true); }
    } finally { if (alive.current) setBusy(false); }
  }
  const item = access.cached[access.selected];
  const metadata = access.manifest.items[access.selected];
  const resolver = useCallback(async (lineupId: string, signal?: AbortSignal) => {
    try { return await publicShareRequest<Reference>('lineups', { read_grant: access.grant, version_id: access.manifest.version_id, item_id: item.item_id, lineup_id: lineupId }, signal); }
    catch (failure) {
      if (failure instanceof ApiError && failure.code === 'SHARE_UNAVAILABLE' && !signal?.aborted) onUnavailable(shareError(failure));
      throw failure;
    }
  }, [access.grant, access.manifest.version_id, item, onUnavailable]);
  return <section aria-label="匿名分享内容"><ShareHeader title={metadata?.title} revision={metadata?.source_revision} copyAllowed={access.manifest.allow_copy} onRefresh={onRefresh} onCopy={onCopy} />
    <div className="shared-items"><p>发布于{new Date(access.manifest.published_at).toLocaleString()} · {access.manifest.items.length}份战术{!access.manifest.allow_copy && ' · 仅可查看'}</p><nav aria-label="已读取的分享战术">{access.cached.map((entry, index) => <button className="button" key={entry.item_id} aria-pressed={access.selected === index} onClick={() => setAccess(current => ({ ...current, selected: index }))}>{access.manifest.items[index]?.title}</button>)}</nav>
      {access.cursor && <button className="button" disabled={busy || stale} onClick={() => void next()}>{busy ? '正在读取下一项…' : '读取下一份战术'}</button>}
    </div>{error && <p role="alert">{error}</p>}
    {item && metadata ? <TacticPlaybackPanel key={item.item_id} confirmed={item.projection} revision={metadata.source_revision} epoch={access.manifest.version_id} hasUnconfirmedWork={false} resolveTeaching={resolver} teachingScope={`${access.manifest.version_id}:${item.item_id}`} /> : <p>发布清单没有可演示的正文。</p>}
  </section>;
}
function ShareCopy({ grant, manifest, active }: { grant: string; manifest: Manifest; active: boolean }) {
  const [session, setSession] = useState<Session | null>(null); const [login, setLogin] = useState(false); const [error, setError] = useState(''); const [busy, setBusy] = useState(false); const [copies, setCopies] = useState<{ id: string; title: string }[] | null>(null);
  const write = useTeamWrite(session?.csrf_token ?? '', value => { setError(''); setCopies((value as { tactics: { id: string; title: string }[] }).tactics); }, failure => { setError(shareError(failure)); if (failure instanceof ApiError && failure.status === 401) { setSession(null); setLogin(true); } });
  const alive = useRef(false); useEffect(() => { alive.current = true; return () => { alive.current = false; }; }, []);
  async function checkLogin() {
    setBusy(true); setError('');
    try { const current = await accountRequest<Session>('auth/session'); if (alive.current) setSession(current); }
    catch (failure) { if (alive.current) { if (failure instanceof ApiError && failure.status === 401) setLogin(true); else setError('暂时无法确认登录状态，请重试。'); } }
    finally { if (alive.current) setBusy(false); }
  }
  if (!active) return write.uncertain ? <p>复制结果仍待核对；请先刷新访问凭据，再按原请求确认。不会自动重新复制。</p> : null;
  if (!manifest.allow_copy && !write.uncertain) return <p>作者未允许复制此发布版本。</p>;
  return <section aria-label="复制分享"><h3>复制发布版本</h3><p>将全部{manifest.items.length}份战术复制到你的个人战术本，生成独立副本。配额不足时不会部分复制。</p>{error && <p role="alert">{error}</p>}
    {!session && <button className="button" disabled={busy} onClick={() => void checkLogin()}>确认登录以继续复制</button>}
    {login && !session && <InlineShareLogin onSession={value => { setSession(value); setLogin(false); }} />}
    {session && <><p>当前账户：{session.user.display_name}</p><button className="button" disabled={!manifest.allow_copy || write.busy || write.uncertain} onClick={() => write.write('shares/copy', 'POST', { read_grant: grant, expected_version_id: manifest.version_id })}>确认复制这个发布版本</button></>}
    {write.error && !error && <p role="alert">{write.error}</p>}{write.uncertain && <button className="button" disabled={write.busy || !session} onClick={() => write.retryShareGrant(grant)}>按原请求核对副本</button>}{copies && <ul>{copies.map(copy => <li key={copy.id}><Link to={`/tactics/${copy.id}`}>{copy.title}</Link></li>)}</ul>}
  </section>;
}
function InlineShareLogin({ onSession }: { onSession: (value: Session) => void }) {
  const [busy, setBusy] = useState(false); const [error, setError] = useState(''); const alive = useRef(false);
  useEffect(() => { alive.current = true; return () => { alive.current = false; }; }, []);
  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); if (busy) return; const form = event.currentTarget; const body = Object.fromEntries(new FormData(form).entries()); setBusy(true); setError('');
    try {
      let csrf: string | undefined;
      try { csrf = (await accountRequest<Session>('auth/session')).csrf_token; } catch (failure) { if (!(failure instanceof ApiError && failure.status === 401)) throw failure; }
      await accountRequest('auth/login', 'POST', body, csrf);
      const current = await accountRequest<Session>('auth/session'); if (alive.current) { form.reset(); onSession(current); }
    } catch (failure) { if (alive.current) { form.reset(); setError(failure instanceof ApiError ? failure.message : '暂时无法确认登录结果，请重试检查登录状态。'); } }
    finally { if (alive.current) setBusy(false); }
  }
  return <form className="admin-form" onSubmit={event => void submit(event)}><fieldset disabled={busy}><legend>在本页登录，保留分享访问</legend>{error && <p role="alert">{error}</p>}<label>用户名<input name="username" required minLength={3} maxLength={32} autoComplete="username" /></label><label>密码<input name="password" type="password" required minLength={12} maxLength={128} autoComplete="current-password" /></label><button className="button">登录并返回复制确认</button><Link to="/account" target="_blank" rel="noopener">在另一页注册或找回密码</Link></fieldset></form>;
}
