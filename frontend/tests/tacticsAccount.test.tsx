import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, expect, it, vi } from 'vitest';
import App from '../src/app/App';

const session = { user: { id: 'user-a', username: 'player', display_name: '玩家' }, csrf_token: 'session-token' };
function response(payload: unknown, status = 200) { return { ok: status < 400, status, headers: new Headers(), json: async () => payload }; }
afterEach(() => vi.unstubAllGlobals());
it('labels each anonymous account task accurately while changing modes without submitting credentials', async () => {
  const fetcher = vi.fn(async (url: string) => url.endsWith('/features') ? response({ tactics_enabled: true }) : response({ error: { message: '未登录' } }, 401));
  vi.stubGlobal('fetch', fetcher);
  render(<MemoryRouter initialEntries={['/account']}><App /></MemoryRouter>);
  await screen.findByRole('button', { name: '注册' });
  expect(screen.getByRole('heading', { level: 1, name: '欢迎回来' })).toBeVisible();
  fireEvent.click(screen.getByRole('button', { name: '注册' }));
  expect(screen.getByRole('heading', { level: 1, name: '创建账户' })).toBeVisible();
  expect(screen.getByLabelText('昵称')).toBeVisible();
  fireEvent.click(screen.getByRole('button', { name: '找回密码' }));
  expect(screen.getByRole('heading', { level: 1, name: '恢复账号' })).toBeVisible();
  expect(screen.getByLabelText('恢复码')).toBeVisible();
  fireEvent.click(within(screen.getByRole('navigation', { name: '账户操作' })).getByRole('button', { name: '登录' }));
  expect(screen.getByRole('heading', { level: 1, name: '欢迎回来' })).toBeVisible();
  expect(screen.queryByLabelText('恢复码')).not.toBeInTheDocument();
  expect(fetcher.mock.calls.every(([url]) => url.endsWith('/features') || url.endsWith('/auth/session'))).toBe(true);
});
it('retains a rejected nickname for an explicit corrected-service retry without repeating the write', async () => {
  let writes = 0;
  const updated = { ...session, user: { ...session.user, display_name: '新昵称' } };
  const fetcher = vi.fn(async (url: string, options?: RequestInit) => {
    if (url.endsWith('/features')) return response({ tactics_enabled: true });
    if (url.endsWith('/auth/session')) return response(writes > 1 ? updated : session);
    if (url.endsWith('/notebooks/personal')) return response({ notebook: { id: 'book', kind: 'personal', user_id: session.user.id, team_id: null } });
    if (url.endsWith('/auth/profile')) {
      writes += 1;
      expect(options?.body).toBe(JSON.stringify({ display_name: '新昵称' }));
      return writes === 1 ? response({ error: { code: 'VALIDATION_ERROR', message: '昵称暂未通过校验' } }, 422) : response({ user: updated.user });
    }
    throw Error('unexpected request');
  });
  vi.stubGlobal('fetch', fetcher);
  render(<MemoryRouter initialEntries={['/account']}><App /></MemoryRouter>);
  const nickname = await screen.findByLabelText('昵称');
  fireEvent.change(nickname, { target: { value: '新昵称' } });
  const save = screen.getByRole('button', { name: '保存昵称' });
  fireEvent.submit(save.closest('form')!);
  expect(await screen.findByRole('alert')).toHaveTextContent('昵称暂未通过校验');
  expect(nickname).toHaveValue('新昵称');
  expect(writes).toBe(1);
  fireEvent.submit(save.closest('form')!);
  expect(await screen.findByRole('status')).toHaveTextContent('操作成功');
  expect(writes).toBe(2);
});

it('keeps a profile input when switching account tasks without submitting it', async () => {
  const fetcher = vi.fn(async (url: string) => response(url.endsWith('/features') ? { tactics_enabled: true } : url.endsWith('/auth/session') ? session : { notebook: { id: 'book', kind: 'personal', user_id: session.user.id, team_id: null } }));
  vi.stubGlobal('fetch', fetcher);
  render(<MemoryRouter initialEntries={['/account']}><App /></MemoryRouter>);
  const nickname = await screen.findByLabelText('昵称');
  fireEvent.change(nickname, { target: { value: '未提交昵称' } });
  fireEvent.click(screen.getByRole('button', { name: /^账户安全$/ }));
  expect(nickname).not.toBeVisible();
  expect(nickname.closest('section')).toHaveAttribute('hidden');
  expect(screen.queryByRole('textbox', { name: '昵称' })).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole('button', { name: /^个人资料$/ }));
  expect(screen.getByLabelText('昵称')).toBe(nickname);
  expect(nickname).toHaveValue('未提交昵称');
  expect(fetcher.mock.calls.some(([url]) => url.endsWith('/auth/profile'))).toBe(false);
});

it('replaces failed personal notebook loading with an explicit safe read retry', async () => {
  let reads = 0;
  const fetcher = vi.fn(async (url: string) => {
    if (url.endsWith('/features')) return response({ tactics_enabled: true });
    if (url.endsWith('/auth/session')) return response(session);
    if (url.endsWith('/notebooks/personal')) {
      if (++reads === 1) throw TypeError('temporary read failure');
      return response({ notebook: { id: 'book', kind: 'personal', user_id: session.user.id, team_id: null } });
    }
    if (url.includes('/notebooks/book/tactics?')) return response({ items: [], next_cursor: null, permissions: { read: true, create: true, manage: true, publish: true } });
    throw Error('unexpected request');
  });
  vi.stubGlobal('fetch', fetcher);
  render(<MemoryRouter initialEntries={['/tactics']}><App /></MemoryRouter>);
  await screen.findByRole('alert');
  expect(screen.queryByText('正在加载个人战术本…')).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole('button', { name: '重试读取个人战术本' }));
  expect(await screen.findByText('还没有战术，创建第一份战术吧。')).toBeVisible();
  expect(screen.getByRole('button', { name: '新建战术' })).toBeVisible();
  expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  expect(reads).toBe(2);
});

it.each(['password', 'recovery-code'])('preserves session and CSRF after invalid current password for %s', async action => {
  let attempts = 0;
  const fetcher = vi.fn(async (url: string, options?: RequestInit) => {
    if (url.endsWith('/features')) return response({ tactics_enabled: true });
    if (url.endsWith('/auth/session')) return response(session);
    if (url.endsWith('/notebooks/personal')) return response({ notebook: { id: 'book', kind: 'personal', user_id: 'user-a', team_id: null } });
    if (url.endsWith(`/auth/${action}`)) {
      attempts += 1;
      expect(options?.headers).toMatchObject({ 'X-CSRF-Token': session.csrf_token });
      return response({ error: { code: 'USER_AUTH_INVALID', message: '当前密码不正确' } }, 401);
    }
    throw Error('unexpected request');
  });
  vi.stubGlobal('fetch', fetcher);
  render(<MemoryRouter initialEntries={['/account']}><App /></MemoryRouter>);
  fireEvent.click(await screen.findByRole('button', { name: action === 'password' ? /^账户安全$/ : /^恢复码$/ }));
  const button = await screen.findByRole('button', { name: action === 'password' ? '修改密码' : '生成新恢复码' });
  const form = within(button.closest('form')!);
  fireEvent.change(form.getByLabelText('当前密码'), { target: { value: 'incorrect-secret-password' } });
  if (action === 'password') fireEvent.change(form.getByLabelText('新密码'), { target: { value: 'new-secret-password' } });
  fireEvent.submit(button.closest('form')!);
  expect(await screen.findByRole('alert')).toHaveTextContent('当前密码不正确');
  expect(form.getByLabelText('当前密码')).toHaveValue('');
  if (action === 'password') expect(form.getByLabelText('新密码')).toHaveValue('');
  expect(screen.getByRole('heading', { name: '账户设置' })).toBeVisible();
  expect(screen.queryByRole('button', { name: '登录' })).not.toBeInTheDocument();
  fireEvent.submit(button.closest('form')!);
  await waitFor(() => expect(attempts).toBe(2));
});

it('retains received recovery code and reports known write success when session refresh fails', async () => {
  let sessionReads = 0;
  vi.stubGlobal('fetch', vi.fn(async (url: string) => {
    if (url.endsWith('/features')) return response({ tactics_enabled: true });
    if (url.endsWith('/auth/session')) {
      if (++sessionReads > 1) throw new TypeError('session read failed');
      return response(session);
    }
    if (url.endsWith('/notebooks/personal')) return response({ notebook: { id: 'book', kind: 'personal', user_id: 'user-a', team_id: null } });
    if (url.endsWith('/auth/recovery-code')) return response({ recovery_code: 'received-new-code' });
    throw Error('unexpected request');
  }));
  render(<MemoryRouter initialEntries={['/account']}><App /></MemoryRouter>);
  fireEvent.click(await screen.findByRole('button', { name: /^恢复码$/ }));
  fireEvent.submit((await screen.findByRole('button', { name: '生成新恢复码' })).closest('form')!);
  expect(await screen.findByRole('alert')).toHaveTextContent('操作已成功');
  expect(screen.getByRole('alert')).not.toHaveTextContent('操作可能已成功');
  expect(screen.getByText('received-new-code')).toBeVisible();
  expect(screen.getByRole('button', { name: '已保存，继续' })).toBeDisabled();
});

it('requires saved recovery-code acknowledgement and uses session CSRF in memory', async () => {
  let loggedIn = false;
  const fetcher = vi.fn(async (url: string, options?: RequestInit) => {
    if (url.endsWith('/features')) return response({ tactics_enabled: true });
    if (url.endsWith('/auth/register')) { loggedIn = true; return response({ ...session, recovery_code: 'one-time-code' }, 201); }
    if (url.endsWith('/auth/session')) return loggedIn ? response(session) : response({ error: { message: '未登录' } }, 401);
    if (url.endsWith('/notebooks/personal')) return response({ notebook: { id: 'book', kind: 'personal', user_id: 'user-a', team_id: null } });
    if (url.includes('/notebooks/book/tactics?')) return response({ items: [], next_cursor: null, permissions: { read: true, create: true, manage: true, publish: true } });
    if (url.endsWith('/auth/logout')) { expect(options?.headers).toMatchObject({ 'X-CSRF-Token': 'session-token' }); return response({ ok: true }); }
    throw Error('unexpected request');
  });
  vi.stubGlobal('fetch', fetcher);
  render(<MemoryRouter initialEntries={['/tactics']}><App /></MemoryRouter>);
  fireEvent.click(await screen.findByRole('button', { name: '注册' }));
  fireEvent.change(screen.getByLabelText('用户名'), { target: { value: 'player' } });
  fireEvent.change(screen.getByLabelText('昵称'), { target: { value: '玩家' } });
  fireEvent.change(screen.getByLabelText('密码'), { target: { value: ' password1234 ' } });
  fireEvent.submit(screen.getByRole('button', { name: '注册并生成恢复码' }).closest('form')!);
  expect(await screen.findByText('one-time-code')).toBeVisible();
  expect(screen.getByRole('button', { name: '已保存，继续' })).toBeDisabled();
  expect(screen.queryByRole('link', { name: '账户设置' })).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole('checkbox'));
  await waitFor(() => expect(screen.getByRole('button', { name: '已保存，继续' })).toBeEnabled());
  fireEvent.click(screen.getByRole('button', { name: '已保存，继续' }));
  expect(await screen.findByText('还没有战术，创建第一份战术吧。')).toBeVisible();
  expect(screen.getByRole('button', { name: '新建战术' })).toBeVisible();
  expect(fetcher.mock.calls.find(([url]) => url.endsWith('/auth/register'))?.[1]?.body).toContain(' password1234 ');
  fireEvent.submit(screen.getByRole('button', { name: '退出登录' }).closest('form')!);
  expect(await screen.findByText('已退出登录。')).toBeVisible();
  expect(fetcher.mock.calls.every(([, options]) => options?.credentials === 'same-origin')).toBe(true);
});

it('does not retry a lost registration response or permit a blind second mutation', async () => {
  const fetcher = vi.fn(async (url: string) => {
    if (url.endsWith('/features')) return response({ tactics_enabled: true });
    if (url.endsWith('/auth/session')) return response({ error: { message: '未登录' } }, 401);
    throw new TypeError('network response lost');
  });
  vi.stubGlobal('fetch', fetcher);
  render(<MemoryRouter initialEntries={['/tactics']}><App /></MemoryRouter>);
  fireEvent.click(await screen.findByRole('button', { name: '注册' }));
  fireEvent.submit(screen.getByRole('button', { name: '注册并生成恢复码' }).closest('form')!);
  expect(await screen.findByRole('alert')).toHaveTextContent('操作可能已成功');
  expect(screen.queryByRole('button', { name: '注册并生成恢复码' })).not.toBeInTheDocument();
  expect(fetcher).toHaveBeenCalledTimes(3);
});

it('rotates a recovery code with current password and blocks navigation until acknowledgement', async () => {
  const fetcher = vi.fn(async (url: string, options?: RequestInit) => {
    if (url.endsWith('/features')) return response({ tactics_enabled: true });
    if (url.endsWith('/auth/session')) return response(session);
    if (url.endsWith('/notebooks/personal')) return response({ notebook: { id: 'book', kind: 'personal', user_id: 'user-a', team_id: null } });
    if (url.endsWith('/auth/recovery-code')) {
      expect(options?.headers).toMatchObject({ 'X-CSRF-Token': session.csrf_token });
      expect(JSON.parse(options?.body as string)).toEqual({ current_password: '  password1234  ' });
      return response({ recovery_code: 'rotated-code' });
    }
    throw Error('unexpected request');
  });
  vi.stubGlobal('fetch', fetcher);
  render(<MemoryRouter initialEntries={['/account']}><App /></MemoryRouter>);
  fireEvent.click(await screen.findByRole('button', { name: /^恢复码$/ }));
  const button = await screen.findByRole('button', { name: '生成新恢复码' });
  const form = button.closest('form')!;
  fireEvent.change(form.querySelector('input')!, { target: { value: '  password1234  ' } });
  fireEvent.submit(form);
  expect(await screen.findByText('rotated-code')).toBeVisible();
  expect(screen.queryByRole('link')).not.toBeInTheDocument();
  expect(screen.getByRole('button', { name: '已保存，继续' })).toBeDisabled();
});
