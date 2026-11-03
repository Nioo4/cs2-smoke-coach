import { useEffect, useRef, useState, type FormEvent } from 'react';
import { Link, useLocation, useParams } from 'react-router-dom';
import { ApiError } from '../api/client';
import { accountRequest, type Session, type Notebook } from './api';
import './account.css';
import './workshop-business.css';
import NotebookContents from './Notebook';
import TacticPresentation from './TacticPresentation';
import TacticEditor from './TacticEditor';
import TeamsPage from './TeamsPage';
import JoinTeam from './JoinTeam';
import WorkshopHeader from '../components/WorkshopHeader';

type Mode = 'login' | 'register' | 'recover';
export default function AccountPage({ inviteToken = null }: { inviteToken?: string | null }) {
  const location = useLocation();
  const { tacticId, teamId } = useParams();
  const [session, setSession] = useState<Session | null>(null);
  const [notebookAttempt, setNotebookAttempt] = useState(0);
  const [notebookRead, setNotebookRead] = useState<{ key: string; notebook?: Notebook; error?: string }>({ key: '' });
  const [mode, setMode] = useState<Mode>('login');
  const [accountTab, setAccountTab] = useState<'profile' | 'security' | 'recovery'>('profile');
  const [ready, setReady] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [code, setCode] = useState('');
  const [saved, setSaved] = useState(false);
  const alive = useRef(false);
  const inFlight = useRef(false);
  const settings = location.pathname === '/account';
  const present = location.pathname.endsWith('/present');
  const teams = location.pathname.startsWith('/teams');
  const join = location.pathname === '/join';
  const notebookKey = JSON.stringify([session?.user.id, notebookAttempt, Boolean(code)]);
  const notebook = notebookRead.key === notebookKey ? notebookRead.notebook : null;
  const notebookError = notebookRead.key === notebookKey ? notebookRead.error : '';

  useEffect(() => {
    alive.current = true;
    const controller = new AbortController();
    accountRequest<Session>('auth/session', 'GET', undefined, undefined, controller.signal)
      .then(result => { if (!controller.signal.aborted) setSession(result); })
      .catch((err: unknown) => {
        if (!controller.signal.aborted && !(err instanceof ApiError && err.status === 401)) setError('暂时无法读取账户，请重新加载。');
      })
      .finally(() => { if (!controller.signal.aborted) setReady(true); });
    return () => { alive.current = false; controller.abort(); };
  }, []);

  useEffect(() => {
    if (!session || code) return;
    const controller = new AbortController();
    accountRequest<{ notebook: Notebook }>('notebooks/personal', 'GET', undefined, undefined, controller.signal)
      .then(result => { if (!controller.signal.aborted) setNotebookRead({ key: notebookKey, notebook: result.notebook }); })
      .catch((err: unknown) => {
        if (controller.signal.aborted) return;
        if (err instanceof ApiError && err.code === 'USER_AUTH_REQUIRED') { setSession(null); setError(err.message); return; }
        setNotebookRead({ key: notebookKey, error: err instanceof ApiError ? err.message : '暂时无法加载个人战术本，请重试。' });
      });
    return () => controller.abort();
  }, [session, code, notebookKey]);

  useEffect(() => {
    if (!code && !busy) return;
    const warn = (event: BeforeUnloadEvent) => { event.preventDefault(); };
    window.addEventListener('beforeunload', warn);
    return () => window.removeEventListener('beforeunload', warn);
  }, [code, busy]);

  async function submit(event: FormEvent<HTMLFormElement>, action: string) {
    event.preventDefault();
    if (inFlight.current || !ready) return;
    inFlight.current = true;
    const form = event.currentTarget;
    const body = Object.fromEntries(new FormData(form).entries());
    setBusy(true); setError(''); setNotice('');
    let writeSucceeded = false;
    try {
      const result = await accountRequest<{ recovery_code?: string }>(`auth/${action}`, action === 'profile' ? 'PATCH' : 'POST', body, session?.csrf_token);
      writeSucceeded = true;
      if (!alive.current) return;
      form.reset();
      if (result.recovery_code) { setCode(result.recovery_code); setSaved(false); }
      if (action === 'recover' || action === 'logout') { setSession(null); setNotebookRead({ key: '' }); setMode('login'); }
      else {
        // Always obtain CSRF from the session endpoint, including after password rotation.
        const next = await accountRequest<Session>('auth/session');
        if (!alive.current) return;
        setSession(next);
      }
      setNotice(action === 'recover' ? '密码已重置。保存新恢复码后，请使用新密码登录。' : action === 'logout' ? '已退出登录。' : '操作成功。');
    } catch (err) {
      if (!alive.current) return;
      if (action !== 'profile') form.reset();
      if (err instanceof ApiError && err.code === 'USER_AUTH_REQUIRED') { setSession(null); setNotebookRead({ key: '' }); }
      if (writeSucceeded) {
        // A failed session refresh cannot undo an acknowledged write or lose its new code.
        setError('操作已成功，但暂时无法读取更新后的账户。请先保存当前显示的恢复码（如有），再重新加载；无需重复提交刚才的操作。');
        setReady(false);
        return;
      }
      const uncertain = !(err instanceof ApiError);
      setError(uncertain
        ? (['register', 'recover', 'recovery-code'].includes(action)
          ? '未收到完整结果，操作可能已成功，恢复码无法再次显示。请勿直接重复提交；重新加载后尝试登录，再用当前密码生成新的恢复码。若无法登录，请保留原恢复码尝试恢复。'
          : '未收到完整结果，操作可能已成功。请重新加载并检查账户状态后再操作。')
        : err.message);
      // An uncertain write is never retried automatically.
      if (uncertain) setReady(false);
    } finally {
      inFlight.current = false;
      if (alive.current) setBusy(false);
    }
  }

  if (session && ready && tacticId && !present && !code) return <><WorkshopHeader><form onSubmit={event => void submit(event, 'logout')}><button className="button" disabled={busy}>退出登录</button></form></WorkshopHeader><main className="workshop-editor-shell"><TacticEditor key={`${session.user.id}:${tacticId}`} tacticId={tacticId} session={session} /></main></>;
  if (session && ready && tacticId && present && !code) return <><WorkshopHeader /><main className="page-shell tactics-account tactics-presentation-page workshop-business"><TacticPresentation key={`${session.user.id}:${tacticId}`} tacticId={tacticId} userId={session.user.id} /></main></>;
  const password = (name: string, label: string, current = false) => <label>{label}<input name={name} type="password" minLength={12} maxLength={128} required autoComplete={current ? 'current-password' : 'new-password'} /></label>;
  return <>{!tacticId && !code && <div inert={busy}><WorkshopHeader /></div>}<main className={`page-shell compact-page tactics-account workshop-business${session && !tacticId && !code ? ' workshop-ordinary' : ''}${!session ? ` workshop-auth${mode === 'recover' ? ' workshop-auth-recover' : ''}` : ''}${present || tacticId ? ' tactics-presentation-page' : ''}`}>
    <p className="eyebrow">CS2 Workshop · 战术本</p>
    <h1>{code ? '请保存恢复码' : session ? settings ? '账户设置' : teams ? '我的队伍' : join ? '确认队伍邀请' : tacticId ? present ? '演示已保存版本' : '战术编辑' : `${session.user.display_name}的个人战术本` : ({ login: '欢迎回来', register: '创建账户', recover: '恢复账号' })[mode]}</h1>
    {error && <p role="alert">{error}</p>}
    {notice && <p role="status">{notice}</p>}
    {!ready && !error && <p role="status">正在读取账户…</p>}
    {!ready && error && <button className="button" onClick={() => window.location.reload()}>重新加载</button>}
    {code ? <section aria-label="一次性恢复码">
      <p>恢复码只显示这一次。忘记密码时需要它；请保存在自己安全的位置，不要发给他人。新码已替代旧码。</p>
      <pre className="recovery-code">{code}</pre>
      <label className="recovery-saved"><input type="checkbox" checked={saved} onChange={event => setSaved(event.target.checked)} />我已安全保存恢复码</label>
      <button className="button button-primary" disabled={!saved || busy} onClick={() => { setCode(''); setSaved(false); }}>已保存，继续</button>
    </section> : <>
      {ready && !session && <>
        <nav aria-label="账户操作">{(['login', 'register', 'recover'] as Mode[]).map(item => <button key={item} className="button" disabled={busy} aria-pressed={mode === item} onClick={() => { setMode(item); setError(''); setNotice(''); }}>{({ login: '登录', register: '注册', recover: '找回密码' })[item]}</button>)}</nav>
        <form className="admin-form" onSubmit={event => void submit(event, mode)}>
          <fieldset disabled={busy}>
            <legend>{({ login: '登录账户', register: '创建账户', recover: '通过恢复码重置密码' })[mode]}</legend>
            <label>用户名<input name="username" required minLength={3} maxLength={32} pattern="[A-Za-z0-9_]{3,32}" autoComplete="username" /></label>
            <p>用户名为3–32位英文字母、数字或下划线。</p>
            {mode === 'register' && <label>昵称<input name="display_name" required maxLength={32} autoComplete="nickname" /></label>}
            {mode === 'recover' && <label>恢复码<input name="recovery_code" required autoComplete="off" spellCheck={false} /></label>}
            {password(mode === 'recover' ? 'new_password' : 'password', mode === 'recover' ? '新密码' : '密码', mode === 'login')}
            <p>密码为12–128个字符，空格也属于密码。</p>
            <button className="button button-primary" type="submit">{busy ? '处理中…' : ({ login: '登录', register: '注册并生成恢复码', recover: '重置密码并生成新恢复码' })[mode]}</button>
          </fieldset>
        </form>
        {mode === 'recover' && <aside className="workshop-recovery-explainer"><h2>恢复码等同于钥匙</h2><p>请勿把恢复码发给队友或其他人。恢复成功后旧码失效，必须安全保存只显示一次的新码，再使用新密码登录。</p><p>没有恢复码时，本页不提供邮箱、短信或第三方找回服务。</p></aside>}
      </>}
      {session && ready && <>
        {settings ? <>
          <div className="workshop-settings-layout"><nav className="workshop-task-tabs" aria-label="账户设置任务">{(['profile', 'security', 'recovery'] as const).map(tab => <button className="button" key={tab} disabled={busy} aria-pressed={accountTab === tab} onClick={() => setAccountTab(tab)}>{({ profile: '个人资料', security: '账户安全', recovery: '恢复码' })[tab]}</button>)}</nav><div className="workshop-settings-panel">
          <section hidden={accountTab !== 'profile'}><h2>个人资料</h2><p>用户名：{session.user.username}</p><form className="admin-form" onSubmit={event => void submit(event, 'profile')}><fieldset disabled={busy}><legend>修改昵称</legend><label>昵称<input name="display_name" required maxLength={32} defaultValue={session.user.display_name} /></label><button className="button button-primary">保存昵称</button></fieldset></form></section>
          <section hidden={accountTab !== 'security'}><h2>账户安全</h2><form className="admin-form" onSubmit={event => void submit(event, 'password')}><fieldset disabled={busy}><legend>修改密码</legend><p>修改后，其他窗口和设备需要重新登录。</p>{password('current_password', '当前密码', true)}{password('new_password', '新密码')}<button className="button button-primary">修改密码</button></fieldset></form></section>
          <section hidden={accountTab !== 'recovery'}><h2>恢复码</h2><p>旧恢复码不会再次显示。验证当前密码后可以生成新码。</p><form className="admin-form" onSubmit={event => void submit(event, 'recovery-code')}><fieldset disabled={busy}><legend>更换恢复码</legend><p>原恢复码立即失效，新码只显示一次。</p>{password('current_password', '当前密码', true)}<button className="button button-primary">生成新恢复码</button></fieldset></form></section></div></div>
        </> : teams ? <TeamsPage key={`${session.user.id}:${teamId ?? 'directory'}`} session={session} teamId={teamId} /> : join ? <JoinTeam key={session.user.id} session={session} token={inviteToken} /> : present && tacticId ? <TacticPresentation key={`${session.user.id}:${tacticId}`} tacticId={tacticId} userId={session.user.id} /> : tacticId ? <TacticEditor key={`${session.user.id}:${tacticId}`} tacticId={tacticId} session={session} /> : notebookError ? <section><p role="alert">{notebookError}</p><button className="button" onClick={() => setNotebookAttempt(value => value + 1)}>重试读取个人战术本</button></section> : notebook ? <NotebookContents notebook={notebook} session={session} /> : <p role="status">正在加载个人战术本…</p>}
        <form onSubmit={event => void submit(event, 'logout')}><button className="button" disabled={busy}>退出登录</button></form>
      </>}
      <Link aria-disabled={busy} onClick={event => { if (busy) event.preventDefault(); }} to="/">返回教学页</Link>
    </>}
  </main></>;
}
