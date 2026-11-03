import { useEffect, useState, type FormEvent } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import { accountRequest, type Session } from './api';
import Notebook from './Notebook';
import { teamError, useTeamWrite, type Invite, type IssuedInvite, type Team, type TeamView } from './teamApi';

export default function TeamsPage({ session, teamId }: { session: Session; teamId?: string }) {
  return <div className="workshop-team-layout"><aside className="workshop-team-sidebar"><TeamDirectory session={session} selectedTeamId={teamId} compact={Boolean(teamId)} /></aside>{teamId ? <div className="workshop-team-detail"><TeamDetail key={teamId} teamId={teamId} session={session} /></div> : <section className="workshop-team-detail"><h2>选择你的队伍</h2><p>从左侧选择队伍，查看共享战术本、成员、邀请及队伍设置。新建队伍后会进入该队伍。</p></section>}</div>;
}
function TeamDirectory({ session, compact = false, selectedTeamId }: { session: Session; compact?: boolean; selectedTeamId?: string }) {
  const navigate = useNavigate();
  const [archived, setArchived] = useState(false); const [cursor, setCursor] = useState<string | null>(null); const [refresh, setRefresh] = useState(0);
  const key = JSON.stringify([archived, cursor, refresh]);
  const [result, setResult] = useState<{ key: string; items?: Team[]; next?: string | null; error?: string }>({ key: '' });
  const write = useTeamWrite(session.csrf_token, (value, path) => {
    if (path === 'teams') navigate(`/teams/${(value as { team: Team }).team.id}`);
    else { setCursor(null); setRefresh(count => count + 1); }
  });
  useEffect(() => {
    const controller = new AbortController(); const params = new URLSearchParams({ limit: '20' }); if (cursor) params.set('cursor', cursor);
    accountRequest<{ items: Team[]; next_cursor: string | null }>(`teams${archived ? '/archived' : ''}?${params}`, 'GET', undefined, undefined, controller.signal)
      .then(value => { if (!controller.signal.aborted) setResult({ key, items: value.items, next: value.next_cursor }); })
      .catch(error => { if (!controller.signal.aborted) setResult({ key, error: teamError(error) }); });
    return () => controller.abort();
  }, [archived, cursor, refresh, key]);
  function create(event: FormEvent<HTMLFormElement>) { event.preventDefault(); write.write('teams', 'POST', { name: String(new FormData(event.currentTarget).get('name') ?? '').trim() }); }
  const blocked = write.busy || write.uncertain;
  return <section aria-label="队伍目录"><h2>{archived ? '已归档队伍' : '我的队伍'}</h2><nav><button className="button" disabled={blocked} aria-pressed={!archived} onClick={() => { setArchived(false); setCursor(null); }}>活跃队伍</button><button className="button" disabled={blocked} aria-pressed={archived} onClick={() => { setArchived(true); setCursor(null); }}>已归档队伍</button></nav>
    <WriteStatus write={write} />
    {!archived && <details open={!compact}><summary>创建队伍</summary><form className="admin-form" onSubmit={create}><fieldset disabled={blocked}><legend>创建队伍</legend><label>队伍名称<input name="name" required maxLength={100} /></label><button className="button">创建队伍</button></fieldset></form></details>}
    {result.key !== key ? <p role="status">正在读取队伍…</p> : result.error ? <p role="alert">{result.error}</p> : <><ul>{result.items?.map(team => <li key={team.id}>{archived ? <><span>{team.name}</span><button className="button" disabled={blocked} onClick={() => write.write(`teams/${team.id}/restore`, 'POST', {})}>恢复 {team.name}</button></> : <Link aria-current={team.id === selectedTeamId ? 'page' : undefined} to={`/teams/${team.id}`}>{team.name} · {roleLabel(team.my_role)}</Link>}</li>)}</ul>{!result.items?.length && <p>{archived ? '没有可恢复的归档队伍。' : '你还没有加入队伍。'}</p>}{result.next && <button className="button" disabled={blocked} onClick={() => setCursor(result.next ?? null)}>下一页</button>}{cursor && <button className="button" disabled={blocked} onClick={() => setCursor(null)}>返回第一页</button>}</>}
    {archived && <p>只有原队长可在30天内恢复；原邀请和分享不会自动重新开放。</p>}<button className="button" disabled={blocked} onClick={() => setRefresh(value => value + 1)}>刷新队伍</button>
  </section>;
}
function roleLabel(role: Team['my_role']) { return ({ owner: '队长', editor: '编辑', viewer: '只读成员' })[role]; }
function WriteStatus({ write }: { write: ReturnType<typeof useTeamWrite> }) { return <>{write.error && <p role="alert">{write.error}</p>}{write.uncertain && <button className="button" disabled={write.busy} onClick={write.retry}>核对后按原请求重试</button>}</>; }
function TeamDetail({ session, teamId }: { session: Session; teamId: string }) {
  const [tab, setTab] = useState<'notebook' | 'members' | 'invites' | 'settings'>('notebook');
  const navigate = useNavigate(); const [refresh, setRefresh] = useState(0);
  const [result, setResult] = useState<{ revision: number; value?: TeamView; error?: string }>({ revision: -1 });
  const [confirm, setConfirm] = useState<{ label: string; path: string; body?: object; method: string } | null>(null);
  const write = useTeamWrite(session.csrf_token, (_, path) => {
    setConfirm(null);
    if (path.endsWith('/archive') || path === `teams/${teamId}/members/${session.user.id}`) navigate('/teams');
    else setRefresh(count => count + 1);
  });
  useEffect(() => {
    const controller = new AbortController();
    accountRequest<TeamView>(`teams/${teamId}`, 'GET', undefined, undefined, controller.signal).then(value => { if (!controller.signal.aborted) setResult({ revision: refresh, value }); }).catch(error => { if (!controller.signal.aborted) setResult({ revision: refresh, error: teamError(error) }); });
    return () => controller.abort();
  }, [teamId, refresh]);
  const view = result.revision === refresh ? result.value : undefined;
  const blocked = write.busy || write.uncertain;
  return <section aria-label="队伍详情"><Link to="/teams">返回队伍目录</Link><WriteStatus write={write} />{result.revision !== refresh ? <p role="status">正在读取队伍…</p> : result.error ? <p role="alert">{result.error}</p> : view && <>
    <h2>{view.team.name}</h2><p>我的身份：{roleLabel(view.my_role)}</p>
    <nav aria-label="队伍任务">{(['notebook', 'members', ...(view.permissions.manage ? ['invites'] as const : []), 'settings'] as const).map(item => <button className="button" key={item} aria-pressed={tab === item} onClick={() => setTab(item)}>{({ notebook: '共享战术本', members: '成员与角色', invites: '邀请', settings: '队伍设置' })[item]}</button>)}</nav>
    <div hidden={tab !== 'settings'} className="workshop-team-task-layout">
    <section aria-label="队伍资料"><h3>队伍资料</h3>{view.permissions.manage ? <form className="admin-form" onSubmit={event => { event.preventDefault(); write.write(`teams/${teamId}`, 'PATCH', { name: String(new FormData(event.currentTarget).get('name') ?? '').trim() }); }}><label>队伍名称<input name="name" required maxLength={100} defaultValue={view.team.name} /></label><p>修改显示名称，不改变已保存战术内容的归属。</p><button className="button button-primary" disabled={blocked}>保存队伍名称</button></form> : <><p>队伍名称：{view.team.name}</p><p>只有队长可以修改队伍资料。</p></>}</section>
    <aside aria-label="队伍低频管理"><h3>低频管理</h3><dl><div><dt>队伍成员</dt><dd>{view.members.length} 人</dd></div><div><dt>当前身份</dt><dd>{roleLabel(view.my_role)}</dd></div></dl><p>成员与角色在独立标签中管理。</p>{view.permissions.manage ? <><button className="button" disabled={blocked} onClick={() => setConfirm({ label: '归档队伍并撤销现有邀请和分享，30天内可恢复', path: `teams/${teamId}/archive`, method: 'POST', body: {} })}>归档队伍</button><p>队长退出前需要先转让，或归档队伍。</p></> : <button className="button" disabled={blocked} onClick={() => setConfirm({ label: '退出队伍；已使用过的旧邀请不能让你重新加入', path: `teams/${teamId}/members/${session.user.id}`, method: 'DELETE' })}>退出队伍</button>}</aside>
    </div><div hidden={tab !== 'members'}><h3>队伍成员</h3><ul className="workshop-team-members" aria-label="队伍成员列表">{view.members.map(member => {
      const owner = member.user_id === view.team.owner_id;
      return <li key={member.user_id}><div className="workshop-team-member-identity"><strong>{member.display_name}</strong><span>{owner ? '队长' : roleLabel(member.role)}</span></div>{view.permissions.manage && !owner && <details className="workshop-team-member-management"><summary>管理 {member.display_name}</summary><div className="workshop-team-member-controls"><label>成员权限：{member.display_name}<select disabled={blocked} value={member.role} onChange={event => write.write(`teams/${teamId}/members/${member.user_id}`, 'PATCH', { role: event.target.value })}><option value="viewer">只读成员</option><option value="editor">编辑</option></select></label><button className="button" disabled={blocked} onClick={() => setConfirm({ label: `移除成员 ${member.display_name}`, path: `teams/${teamId}/members/${member.user_id}`, method: 'DELETE' })}>移除 {member.display_name}</button><button className="button" disabled={blocked} onClick={() => setConfirm({ label: `将队长转让给 ${member.display_name}，你将保留编辑身份`, path: `teams/${teamId}/ownership`, method: 'POST', body: { user_id: member.user_id } })}>转让给 {member.display_name}</button></div></details>}</li>;
    })}</ul></div>
    {view.permissions.manage && <div hidden={tab !== 'invites'}><Invites key={teamId} teamId={teamId} session={session} /></div>}
    <div hidden={tab !== 'notebook'}><Notebook key={`${view.notebook.id}:${refresh}`} notebook={view.notebook} session={session} /></div>
  </>}
  {confirm && <section role="alertdialog" aria-label="确认队伍操作"><p>{confirm.label}？</p><button className="button" disabled={blocked} onClick={() => write.write(confirm.path, confirm.method, confirm.body)}>确认操作</button><button className="button" disabled={write.busy} onClick={() => setConfirm(null)}>取消</button></section>}
  <button className="button" disabled={blocked} onClick={() => setRefresh(count => count + 1)}>刷新队伍详情</button>
  </section>;
}
function Invites({ teamId, session }: { teamId: string; session: Session }) {
  const [cursor, setCursor] = useState<string | null>(null); const [refresh, setRefresh] = useState(0); const [issued, setIssued] = useState<IssuedInvite | null>(null);
  const key = JSON.stringify([cursor, refresh]);
  const [result, setResult] = useState<{ key: string; items?: Invite[]; next?: string | null; error?: string }>({ key: '' });
  const write = useTeamWrite(session.csrf_token, (value, path) => { if (path.endsWith('/invites')) setIssued(value as IssuedInvite); else if (issued && path.includes(issued.invite_id)) setIssued(null); setRefresh(count => count + 1); });
  useEffect(() => {
    const controller = new AbortController(); const params = new URLSearchParams({ limit: '20' }); if (cursor) params.set('cursor', cursor);
    accountRequest<{ items: Invite[]; next_cursor: string | null }>(`teams/${teamId}/invites?${params}`, 'GET', undefined, undefined, controller.signal).then(value => { if (!controller.signal.aborted) setResult({ key, items: value.items, next: value.next_cursor }); }).catch(error => { if (!controller.signal.aborted) setResult({ key, error: teamError(error) }); });
    return () => controller.abort();
  }, [teamId, cursor, refresh, key]);
  const blocked = write.busy || write.uncertain;
  const link = issued?.token_available && issued.token ? `${location.origin}/join#token=${issued.token}` : '';
  return <section aria-label="队伍邀请管理" className="workshop-team-task-layout"><section aria-label="邀请队友"><h3>邀请队友</h3><p>邀请后成为队伍成员，不是只读战术分享。</p><p>邀请7天有效、最多20次接受；新成员默认只读，队长可调整权限。</p><WriteStatus write={write} /><button className="button button-primary" disabled={blocked || Boolean(issued)} onClick={() => write.write(`teams/${teamId}/invites`, 'POST', {})}>创建邀请</button>
    {issued && (link ? <div><p>邀请链接只显示本次，请自行保存后分享。</p><label>邀请链接<input readOnly value={link} onFocus={event => event.currentTarget.select()} /></label><button className="button" onClick={() => setIssued(null)}>已保存链接</button></div> : <div><p>此邀请已创建，但链接明文无法再次获取。请撤销该邀请后新建。</p><button className="button" disabled={blocked} onClick={() => write.write(`teams/${teamId}/invites/${issued.invite_id}/revoke`, 'POST', {})}>撤销无法取回的邀请</button></div>)}
    </section><aside aria-label="现有邀请"><h3>现有邀请</h3>{result.key !== key ? <p role="status">正在读取邀请…</p> : result.error ? <p role="alert">{result.error}</p> : <><ul>{result.items?.map(invite => <li key={invite.invite_id}>已接受 {invite.used_count}/{invite.max_uses} · {new Date(invite.expires_at).toLocaleDateString()}到期 · {invite.revoked_at ? '已撤销' : '未撤销'}{!invite.revoked_at && <button className="button" disabled={blocked} onClick={() => write.write(`teams/${teamId}/invites/${invite.invite_id}/revoke`, 'POST', {})}>撤销邀请</button>}</li>)}</ul>{!result.items?.length && <p>没有已创建的邀请。</p>}{result.next && <button className="button" disabled={blocked} onClick={() => setCursor(result.next ?? null)}>下一页邀请</button>}{cursor && <button className="button" onClick={() => setCursor(null)}>邀请第一页</button>}</>}</aside>
  </section>;
}
