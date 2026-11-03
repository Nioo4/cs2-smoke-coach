import { useEffect, useRef, useState } from 'react';
import { Link } from 'react-router-dom';
import { accountRequest, type Session } from './api';
import { teamError, useTeamWrite } from './teamApi';
interface Preview { team_id: string; team_name: string; expires_at: string; already_member: boolean }
export default function JoinTeam({ token, session }: { token: string | null; session: Session }) {
  const [preview, setPreview] = useState<Preview | null>(null); const [busy, setBusy] = useState(false); const [error, setError] = useState('');
  const [accepted, setAccepted] = useState<{ team_id: string; member_role: string } | null>(null);
  const generation = useRef(0);
  useEffect(() => () => { generation.current += 1; }, []);
  const write = useTeamWrite(session.csrf_token, value => setAccepted(value as { team_id: string; member_role: string }));
  async function inspect() {
    if (!token || busy) return; const attempt = ++generation.current; setBusy(true); setError('');
    try { const value = await accountRequest<Preview>('invites/preview', 'POST', { token }, session.csrf_token); if (attempt === generation.current) setPreview(value); }
    catch (failure) { if (attempt === generation.current) { setPreview(null); setError(teamError(failure)); } }
    finally { if (attempt === generation.current) setBusy(false); }
  }
  return <section aria-label="加入队伍"><h2>加入队伍</h2><div className="workshop-invite-layout"><div>{!token ? <p>没有有效邀请。请重新打开队长提供的完整邀请链接；刷新后不会保留链接中的令牌。</p> : <>
    <p>打开链接不会自动加入队伍。先查看邀请，再明确接受。</p>{!accepted && <button className="button" disabled={busy || write.busy || write.uncertain} onClick={() => void inspect()}>查看邀请</button>}
    {error && <p role="alert">{error}</p>}{write.error && <p role="alert">{write.error}</p>}{write.uncertain && <button className="button" disabled={write.busy} onClick={write.retry}>核对后按原请求重试接受</button>}
    {accepted ? <><p>已确认成员身份。</p><Link to={`/teams/${accepted.team_id}`}>打开队伍</Link></> : preview && <><h3>{preview.team_name}</h3><p>{new Date(preview.expires_at).toLocaleDateString()}到期。{preview.already_member ? '你已是成员，确认不会降低已有权限。' : '接受后默认成为只读成员。'}</p><button className="button button-primary" disabled={busy || write.busy || write.uncertain} onClick={() => write.write('invites/accept', 'POST', { token })}>确认接受邀请</button></>}
  </>}
  </div><aside><h3>与战术分享有什么区别？</h3><h4>队伍邀请</h4><p>接受后成为队伍成员，在获准范围内查看或共同编辑共享战术。新加入成员默认只读，队长可以调整权限。</p><h4>只读分享</h4><p>观看作者主动发布的固定版本，不获得队伍成员身份。</p><p>请先确认真实队伍名称，再决定是否接受。打开链接不会自动加入。</p></aside></div>
  </section>;
}
