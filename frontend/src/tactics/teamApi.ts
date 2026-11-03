import { useEffect, useRef, useState } from 'react';
import { ApiError } from '../api/client';
import { accountRequest, type Notebook } from './api';
export interface NotebookPermissions { read: boolean; create: boolean; manage: boolean; publish: boolean }
export interface Team { id: string; name: string; owner_id: string; notebook_id: string; my_role: 'owner' | 'editor' | 'viewer'; created_at: string; updated_at: string; archived_at: string | null }
export interface Member { user_id: string; display_name: string; role: 'owner' | 'editor' | 'viewer'; joined_at: string }
export interface TeamView { team: Team; my_role: Team['my_role']; notebook: Notebook; members: Member[]; permissions: NotebookPermissions }
export interface Invite { invite_id: string; created_at: string; expires_at: string; max_uses: number; used_count: number; revoked_at: string | null }
export interface IssuedInvite { invite_id: string; token: string | null; token_available: boolean; expires_at: string; max_uses: number }
export function teamError(error: unknown) {
  if (!(error instanceof ApiError)) return '未收到完整结果，操作可能已成功。可核对后按原请求重试，不会创建重复资源。';
  return ({ INVITE_UNAVAILABLE: '邀请已不可用，请向队长索取新邀请。', TEAM_MEMBER_LIMIT_REACHED: '队伍人数已满，请联系队长。', TEAM_OWNER_REQUIRED: '队长需要先转让队伍，才能退出。', USER_FORBIDDEN: '当前账户没有此操作权限。', USER_AUTH_REQUIRED: '登录已失效，请重新登录。' })[error.code] ?? (error.status === 404 ? '找不到队伍，可能已归档或你已不是成员。' : error.message);
}
interface Write { path: string; method: string; body?: object; key: string }
export function useTeamWrite(csrf: string, onSuccess: (value: unknown, path: string) => void, onFailure?: (failure: unknown, path: string) => void) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [uncertain, setUncertain] = useState<Write | null>(null);
  const alive = useRef(false); const running = useRef(false);
  useEffect(() => { alive.current = true; return () => { alive.current = false; }; }, []);
  async function perform(operation: Write) {
    if (running.current) return;
    running.current = true; setBusy(true); setError('');
    try {
      const value = await accountRequest<unknown>(operation.path, operation.method, operation.body, csrf, undefined, operation.key);
      if (!alive.current) return;
      setUncertain(null); onSuccess(value, operation.path);
    } catch (failure) {
      if (!alive.current) return;
      setError(teamError(failure));
      onFailure?.(failure, operation.path);
      if (!(failure instanceof ApiError) || failure.status >= 500) setUncertain(operation);
    } finally { running.current = false; if (alive.current) setBusy(false); }
  }
  return { busy, error, uncertain: Boolean(uncertain), pendingBody: uncertain?.body, write: (path: string, method: string, body?: object) => { if (!uncertain) void perform({ path, method, body, key: crypto.randomUUID() }); }, retry: () => { if (uncertain) void perform(uncertain); }, retryShareGrant: (grant: string) => {
    // T4 fingerprints a share/version, so a refreshed read grant may reconcile this same copy key.
    if (uncertain?.path === 'shares/copy') void perform({ ...uncertain, body: { ...uncertain.body, read_grant: grant } });
  } };
}
