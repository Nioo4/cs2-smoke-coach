import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { createBrowserRouter, MemoryRouter, RouterProvider } from 'react-router-dom';
import { afterEach, expect, it, vi } from 'vitest';
import TeamsPage from '../src/tactics/TeamsPage';
import JoinTeam from '../src/tactics/JoinTeam';
import CopyTactic, { SourceAttribution } from '../src/tactics/CopyTactic';
import App from '../src/app/App';
const session = { user: { id: 'owner', username: 'player', display_name: '队长甲' }, csrf_token: 'csrf' };
const token = 'A'.repeat(43);
const team = { id: 'team', name: '测试队伍', owner_id: 'owner', notebook_id: 'team-book', my_role: 'owner', created_at: '', updated_at: '', archived_at: null };
const permissions = { read: true, create: true, manage: true, publish: true };
function response(value: unknown, status = 200) { return { ok: status < 400, status, headers: new Headers(), json: async () => value }; }
it.each([true, false])('retries failed attribution reads without copying, with source=%s', async hasSource => {
  const fetcher = vi.fn().mockResolvedValueOnce(response({ error: { code: 'TEMPORARY', message: 'unavailable' } }, 503)).mockResolvedValueOnce(response({ tactic: { source: hasSource ? { tactic_id: 'source', title: '原始战术', author_display_name: '作者', copied_at: '2026-10-30' } : null } }));
  vi.stubGlobal('fetch', fetcher);
  render(<SourceAttribution tacticId="copy" />);
  await screen.findByText(/暂时无法读取来源署名/);
  expect(screen.queryByText('正在读取来源署名…')).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole('button', { name: '重试读取来源署名' }));
  await waitFor(() => expect(fetcher).toHaveBeenCalledTimes(2));
  await waitFor(() => expect(screen.queryByText('正在读取来源署名…')).not.toBeInTheDocument());
  expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  expect(Boolean(screen.queryByText(/作者的“原始战术”/))).toBe(hasSource);
  for (const [url, options] of fetcher.mock.calls) {
    expect(url).toBe('/api/v1/tactics/copy');
    expect(options.method).toBe('GET');
  }
});
function detail(viewer = false) { return { team, my_role: viewer ? 'viewer' : 'owner', notebook: { id: 'team-book', kind: 'team', user_id: null, team_id: 'team' }, members: [{ user_id: 'owner', display_name: '队长甲', role: 'editor', joined_at: '' }, { user_id: 'member', display_name: '成员乙', role: 'viewer', joined_at: '' }], permissions: viewer ? { read: true, create: false, manage: false, publish: false } : permissions }; }
function read(url: string, viewer = false) {
  if (url === '/api/v1/teams/team') return response(detail(viewer));
  if (url.includes('/teams/team/invites?')) return response({ items: [], next_cursor: null });
  if (url.includes('/notebooks/team-book/tactics?')) return response({ items: [], next_cursor: null, permissions: detail(viewer).permissions });
  return response({ items: [team], next_cursor: null });
}
afterEach(() => { vi.unstubAllGlobals(); window.history.replaceState(null, '', '/'); });
it('marks only the routed team as current without replacing other directory links', async () => {
  const other = { ...team, id: 'other', name: '另一队伍' };
  vi.stubGlobal('fetch', vi.fn(async (url: string) => url.includes('/teams?') ? response({ items: [team, other], next_cursor: null }) : url === '/api/v1/teams/other' ? response({ ...detail(), team: other }) : read(url)));
  const view = render(<MemoryRouter><TeamsPage session={session} teamId="team" /></MemoryRouter>);
  const first = await screen.findByRole('link', { name: '测试队伍 · 队长' });
  const second = screen.getByRole('link', { name: '另一队伍 · 队长' });
  expect(first).toHaveAttribute('aria-current', 'page');
  expect(second).not.toHaveAttribute('aria-current');
  view.rerender(<MemoryRouter><TeamsPage session={session} teamId="other" /></MemoryRouter>);
  expect(first).not.toHaveAttribute('aria-current');
  expect(second).toHaveAttribute('aria-current', 'page');
  expect(first).toHaveAttribute('href', '/teams/team');
  expect(second).toHaveAttribute('href', '/teams/other');
});
it('groups team settings and invitations into real task and supporting regions while keeping inputs mounted', async () => {
  const fetcher = vi.fn(async (url: string) => read(url)); vi.stubGlobal('fetch', fetcher);
  render(<MemoryRouter><TeamsPage session={session} teamId="team" /></MemoryRouter>);
  fireEvent.click(await screen.findByRole('button', { name: '队伍设置' }));
  const profile = screen.getByRole('region', { name: '队伍资料' });
  const management = screen.getByRole('complementary', { name: '队伍低频管理' });
  const name = within(profile).getByLabelText('队伍名称');
  fireEvent.change(name, { target: { value: '未提交队伍名' } });
  expect(within(management).getByText('2 人')).toBeVisible();
  expect(within(management).getByRole('button', { name: '归档队伍' })).toBeVisible();
  fireEvent.click(screen.getByRole('button', { name: /^邀请$/ }));
  expect(screen.queryByRole('region', { name: '队伍资料' })).not.toBeInTheDocument();
  expect(within(screen.getByRole('region', { name: '邀请队友' })).getByRole('button', { name: '创建邀请' })).toBeVisible();
  expect(await within(screen.getByRole('complementary', { name: '现有邀请' })).findByText('没有已创建的邀请。')).toBeVisible();
  fireEvent.click(screen.getByRole('button', { name: '队伍设置' }));
  expect(within(screen.getByRole('region', { name: '队伍资料' })).getByLabelText('队伍名称')).toBe(name);
  expect(name).toHaveValue('未提交队伍名');
  expect(fetcher.mock.calls.every(([, options]: [string, RequestInit?]) => !options?.method || options.method === 'GET')).toBe(true);
});
it('shows server-effective viewer permissions without owner or notebook-write controls', async () => {
  const fetcher = vi.fn(async (url: string) => read(url, true)); vi.stubGlobal('fetch', fetcher);
  render(<MemoryRouter><TeamsPage session={{ ...session, user: { ...session.user, id: 'member' } }} teamId="team" /></MemoryRouter>);
  expect(await screen.findByText('我的身份：只读成员')).toBeVisible();
  await screen.findByText('还没有可查看的战术。');
  expect(screen.queryByRole('button', { name: '创建邀请' })).not.toBeInTheDocument();
  expect(screen.queryByRole('button', { name: '创建战术' })).not.toBeInTheDocument();
  expect(screen.queryByRole('button', { name: '归档队伍' })).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole('button', { name: '成员与角色' }));
  const members = screen.getByRole('list', { name: '队伍成员列表' });
  expect(within(members).queryByText('管理 成员乙')).not.toBeInTheDocument();
  expect(within(members).queryByRole('combobox')).not.toBeInTheDocument();
  expect(members.querySelector('summary')).toBeNull();
  fireEvent.click(screen.getByRole('button', { name: '队伍设置' }));
  expect(screen.getByRole('button', { name: '退出队伍' })).toBeVisible();
  expect(screen.queryByRole('button', { name: '搜索' })).not.toBeInTheDocument();
  expect(screen.getByText('还没有可查看的战术。').closest('[hidden]')).toHaveAttribute('hidden');
  expect(fetcher.mock.calls.some(([url]) => url.includes('/invites'))).toBe(false);
});
it('keeps each member identity visible while disclosing only authorized non-owner management', async () => {
  const fetcher = vi.fn(async (url: string) => read(url)); vi.stubGlobal('fetch', fetcher);
  render(<MemoryRouter><TeamsPage session={session} teamId="team" /></MemoryRouter>);
  fireEvent.click(await screen.findByRole('button', { name: '成员与角色' }));
  const list = screen.getByRole('list', { name: '队伍成员列表' });
  const rows = within(list).getAllByRole('listitem');
  expect(rows).toHaveLength(2);
  expect(within(rows[0]).getByText('队长甲')).toBeVisible();
  expect(within(rows[0]).getByText('队长')).toBeVisible();
  expect(within(rows[0]).queryByText(/管理/)).not.toBeInTheDocument();
  expect(within(rows[1]).getByText('成员乙')).toBeVisible();
  const manage = within(rows[1]).getByText('管理 成员乙');
  const disclosure = manage.closest('details')!;
  expect(disclosure).not.toHaveAttribute('open');
  expect(within(rows[1]).getByLabelText('成员权限：成员乙')).not.toBeVisible();
  manage.focus();
  fireEvent.click(manage);
  expect(disclosure).toHaveAttribute('open');
  expect(within(rows[1]).getByLabelText('成员权限：成员乙')).toHaveValue('viewer');
  expect(within(rows[1]).getByRole('button', { name: '移除 成员乙' })).toBeVisible();
  fireEvent.click(manage);
  expect(within(rows[1]).getByLabelText('成员权限：成员乙')).not.toBeVisible();
  expect(document.activeElement).toBe(manage);
  expect(fetcher.mock.calls.every(([, options]: [string, RequestInit?]) => !options?.method || options.method === 'GET')).toBe(true);
});
it('uses explicit ownership transfer confirmation and CSRF/idempotency without owner-leave shortcut', async () => {
  const fetcher = vi.fn(async (url: string, options?: RequestInit) => options?.method === 'POST' ? response({ team }) : read(url)); vi.stubGlobal('fetch', fetcher);
  render(<MemoryRouter><TeamsPage session={session} teamId="team" /></MemoryRouter>);
  fireEvent.click(await screen.findByRole('button', { name: '成员与角色' }));
  fireEvent.click(await screen.findByText('管理 成员乙'));
  fireEvent.click(await screen.findByRole('button', { name: '转让给 成员乙' }));
  expect(fetcher.mock.calls.some(([, options]) => options?.method === 'POST')).toBe(false);
  fireEvent.click(screen.getByRole('button', { name: '确认操作' }));
  await waitFor(() => expect(fetcher.mock.calls.some(([url]) => url.endsWith('/ownership'))).toBe(true));
  const mutation = fetcher.mock.calls.find(([url]) => url.endsWith('/ownership'))![1]!;
  expect(JSON.parse(mutation.body as string)).toEqual({ user_id: 'member' });
  expect(mutation.headers).toMatchObject({ 'X-CSRF-Token': 'csrf', 'Idempotency-Key': expect.stringMatching(/^[0-9a-f-]{36}$/) });
  expect(screen.queryByRole('button', { name: '退出队伍' })).not.toBeInTheDocument();
});
it('retries a lost invitation response with exactly the same key and explains unrecoverable token', async () => {
  let count = 0;
  const fetcher = vi.fn(async (url: string, options?: RequestInit) => {
    if (url.endsWith('/teams/team/invites') && options?.method === 'POST') {
      if (++count === 1) throw new TypeError('response lost');
      return response({ invite_id: 'invite', token: null, token_available: false, expires_at: '', max_uses: 20 });
    }
    return read(url);
  }); vi.stubGlobal('fetch', fetcher);
  render(<MemoryRouter><TeamsPage session={session} teamId="team" /></MemoryRouter>);
  fireEvent.click(await screen.findByRole('button', { name: /^邀请$/ }));
  fireEvent.click(await screen.findByRole('button', { name: '创建邀请' }));
  fireEvent.click(await screen.findByRole('button', { name: '核对后按原请求重试' }));
  expect(await screen.findByText(/链接明文无法再次获取/)).toBeVisible();
  const writes = fetcher.mock.calls.filter(([, options]) => options?.method === 'POST');
  expect(writes).toHaveLength(2); expect(writes[0][1]?.headers).toEqual(writes[1][1]?.headers); expect(writes[0][1]?.body).toEqual(writes[1][1]?.body);
  expect(screen.getByRole('button', { name: '撤销无法取回的邀请' })).toBeVisible();
});
it('does not consume an invitation on open or preview and accepts only after explicit confirmation', async () => {
  const fetcher = vi.fn(async (url: string) => url.endsWith('/preview') ? response({ team_id: 'team', team_name: '目标队伍', expires_at: '2026-11-06', already_member: false }) : response({ team_id: 'team', member_role: 'viewer' })); vi.stubGlobal('fetch', fetcher);
  render(<MemoryRouter><JoinTeam session={session} token={token} /></MemoryRouter>);
  expect(fetcher).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole('button', { name: '查看邀请' }));
  expect(await screen.findByText('目标队伍')).toBeVisible();
  expect(fetcher).toHaveBeenCalledTimes(1);
  fireEvent.click(screen.getByRole('button', { name: '确认接受邀请' }));
  expect(await screen.findByRole('link', { name: '打开队伍' })).toHaveAttribute('href', '/teams/team');
  expect(fetcher.mock.calls.some(([url]) => url.includes(token))).toBe(false);
});
it('keeps fragment token only in the join component through login and clears the visible fragment', async () => {
  window.history.replaceState(null, '', `/join#token=${token}`);
  let loggedIn = false;
  const fetcher = vi.fn(async (url: string) => {
    if (url.endsWith('/features')) return response({ tactics_enabled: true });
    if (url.endsWith('/auth/login')) { loggedIn = true; return response(session); }
    if (url.endsWith('/auth/session')) return loggedIn ? response(session) : response({ error: { code: 'USER_AUTH_REQUIRED', message: '未登录' } }, 401);
    if (url.endsWith('/notebooks/personal')) return response({ notebook: { id: 'personal', kind: 'personal', user_id: 'owner', team_id: null } });
    if (url.endsWith('/invites/preview')) return response({ team_id: 'team', team_name: '目标队伍', expires_at: '2026-11-06', already_member: false });
    throw Error('unexpected request');
  }); vi.stubGlobal('fetch', fetcher);
  render(<MemoryRouter initialEntries={[`/join#token=${token}`]}><App /></MemoryRouter>);
  fireEvent.submit((await screen.findByLabelText('用户名')).closest('form')!);
  fireEvent.click(await screen.findByRole('button', { name: '查看邀请' }));
  expect(await screen.findByText('目标队伍')).toBeVisible();
  expect(window.location.hash).toBe('');
  expect(fetcher.mock.calls.some(([url]) => url.includes(token))).toBe(false);
  expect(fetcher.mock.calls.some(([url]) => url.endsWith('/accept'))).toBe(false);
});
it('copies only into personal or editable team targets and opens the returned independent ID', async () => {
  const fetcher = vi.fn(async (url: string, options?: RequestInit) => {
    if (url.endsWith('/notebooks/personal')) return response({ notebook: { id: 'personal' } });
    if (url.includes('/teams?')) return response({ items: [team, { ...team, id: 'viewer', name: '只读队伍', notebook_id: 'viewer-book', my_role: 'viewer' }], next_cursor: null });
    if (options?.method === 'POST') return response({ id: 'independent-copy' });
    throw Error('unexpected request');
  }); vi.stubGlobal('fetch', fetcher);
  render(<MemoryRouter><CopyTactic tacticId="source" session={session} /></MemoryRouter>);
  fireEvent.click(screen.getByRole('button', { name: '复制已保存战术' }));
  await screen.findByRole('option', { name: '测试队伍' });
  expect(screen.queryByRole('option', { name: '只读队伍' })).not.toBeInTheDocument();
  fireEvent.change(screen.getByLabelText('目标战术本'), { target: { value: 'team-book' } });
  fireEvent.click(screen.getByRole('button', { name: '确认复制已保存版本' }));
  expect(await screen.findByRole('link', { name: '打开新副本' })).toHaveAttribute('href', '/tactics/independent-copy');
  const mutation = fetcher.mock.calls.find(([url]) => url.endsWith('/copy'))![1]!;
  expect(JSON.parse(mutation.body as string)).toEqual({ target_notebook_id: 'team-book' });
});
it('separates copy-target loading failure from loading and supports explicit retry without a write', async () => {
  let fail = true;
  const fetcher = vi.fn(async (url: string) => {
    if (fail) throw new TypeError('catalog offline');
    return url.endsWith('/notebooks/personal') ? response({ notebook: { id: 'personal' } }) : response({ items: [], next_cursor: null });
  }); vi.stubGlobal('fetch', fetcher);
  render(<MemoryRouter><CopyTactic tacticId="source" session={session} /></MemoryRouter>);
  fireEvent.click(screen.getByRole('button', { name: '复制已保存战术' }));
  expect(await screen.findByRole('alert')).toBeVisible();
  expect(screen.queryByText('正在读取可复制的战术本…')).not.toBeInTheDocument();
  expect(screen.queryByRole('button', { name: '确认复制已保存版本' })).not.toBeInTheDocument();
  fail = false; fireEvent.click(screen.getByRole('button', { name: '重试读取战术本' }));
  expect(await screen.findByRole('option', { name: '我的个人战术本' })).toBeVisible();
  expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  expect(fetcher.mock.calls.some(([url]) => url.endsWith('/copy'))).toBe(false);
});
it('resets a same-tab invite preview for a new hash and never accepts either invitation automatically', async () => {
  const second = 'B'.repeat(43);
  const fetcher = vi.fn(async (url: string, options?: RequestInit) => {
    if (url.endsWith('/features')) return response({ tactics_enabled: true });
    if (url.endsWith('/auth/session')) return response(session);
    if (url.endsWith('/notebooks/personal')) return response({ notebook: { id: 'personal', kind: 'personal', user_id: 'owner', team_id: null } });
    if (url.endsWith('/invites/preview')) return response({ team_id: 'team', team_name: JSON.parse(options?.body as string).token === token ? '邀请A队伍' : '邀请B队伍', expires_at: '2026-11-06', already_member: false });
    throw Error('unexpected accept or private call');
  }); vi.stubGlobal('fetch', fetcher);
  window.history.replaceState(null, '', `/join#token=${token}`); const router = createBrowserRouter([{ path: '*', element: <App /> }]); render(<RouterProvider router={router} />);
  fireEvent.click(await screen.findByRole('button', { name: '查看邀请' })); expect(await screen.findByText('邀请A队伍')).toBeVisible();
  await act(async () => { await router.navigate(`/join#token=${second}`); });
  expect(screen.queryByText('邀请A队伍')).not.toBeInTheDocument(); expect(window.location.hash).toBe('');
  fireEvent.click(await screen.findByRole('button', { name: '查看邀请' })); expect(await screen.findByText('邀请B队伍')).toBeVisible();
  const previews = fetcher.mock.calls.filter(([url]) => url.endsWith('/preview')).map(([, options]) => JSON.parse(options?.body as string).token);
  expect(previews).toEqual([token, second]); expect(fetcher.mock.calls.some(([url]) => url.endsWith('/accept'))).toBe(false);
  router.dispose();
});
