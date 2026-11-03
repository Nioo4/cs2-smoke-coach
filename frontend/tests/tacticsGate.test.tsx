import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { createBrowserRouter, createMemoryRouter, MemoryRouter, RouterProvider } from 'react-router-dom';
import { afterEach, expect, it, vi } from 'vitest';
import App from '../src/app/App';
afterEach(() => { vi.unstubAllGlobals(); window.history.replaceState(null, '', '/'); });
it.each(['/share', '/join'])('scrubs repeated native same-token entries and keeps router state synchronized at %s', async path => {
  const token = 'A'.repeat(43);
  const fetcher = vi.fn(async () => ({ ok: true, json: async () => ({ tactics_enabled: false }) }));
  vi.stubGlobal('fetch', fetcher);
  window.history.replaceState(null, '', `${path}#token=${token}`);
  const router = createBrowserRouter([{ path: '*', element: <App /> }]);
  const { container } = render(<RouterProvider router={router} />);
  try {
    await screen.findByText('战术本暂未开放，请稍后再来。');
    expect(window.location.hash).toBe('');
    for (let entry = 0; entry < 2; entry++) {
      await act(async () => { window.location.hash = `token=${token}`; });
      await waitFor(() => expect(window.location.hash).toBe(''));
    }
    expect(router.state.location.hash).toBe('');
    expect(container.innerHTML).not.toContain(token);
    expect(fetcher).toHaveBeenCalledTimes(1); // No registration, exchange or invite acceptance.
  } finally { router.dispose(); }
});
it.each(['/share', '/join'])('retains an enabled entry in memory and resets an identical native link at %s without accepting', async path => {
  const token = 'A'.repeat(43);
  const response = (payload: unknown) => ({ ok: true, status: 200, headers: new Headers(), json: async () => payload });
  const fetcher = vi.fn(async (url: string, options?: RequestInit) => {
    if (url.endsWith('/features')) return response({ tactics_enabled: true });
    if (url.endsWith('/auth/session')) return response({ user: { id: 'u', username: 'player', display_name: '玩家' }, csrf_token: 'csrf' });
    if (url.endsWith('/notebooks/personal')) return response({ notebook: { id: 'book', kind: 'personal', user_id: 'u', team_id: null } });
    if (url.endsWith('/invites/preview')) return response({ team_id: 'team', team_name: '明确查看的邀请', expires_at: '2026-11-06', already_member: false });
    if (url.endsWith('/shares/exchange')) {
      expect(JSON.parse(options?.body as string)).toEqual({ token });
      return response({ read_grant: 'grant', expires_in: 300 });
    }
    if (url.endsWith('/shares/read')) return response({ id: 'share', version_id: 'version', published_at: '2026-10-30', allow_copy: false, items: [] });
    if (url.endsWith('/shares/items')) return response({ version_id: 'version', items: [], next_cursor: null });
    throw Error('Unexpected private mutation');
  });
  vi.stubGlobal('fetch', fetcher);
  window.history.replaceState(null, '', `${path}#token=${token}`);
  const router = createBrowserRouter([{ path: '*', element: <App /> }]);
  const { container } = render(<RouterProvider router={router} />);
  try {
    for (let entry = 0; entry < 3; entry++) {
      if (entry) await act(async () => { window.location.hash = `token=${token}`; });
      await waitFor(() => expect(window.location.hash).toBe(''));
      if (path === '/join') {
        expect(screen.queryByRole('heading', { name: '明确查看的邀请' })).not.toBeInTheDocument();
        fireEvent.click(await screen.findByRole('button', { name: '查看邀请' }));
        expect(await screen.findByRole('heading', { name: '明确查看的邀请' })).toBeVisible();
      } else await waitFor(() => expect(fetcher.mock.calls.filter(([url]) => url.endsWith('/exchange'))).toHaveLength(entry + 1));
      expect(router.state.location.hash).toBe('');
    }
    if (path === '/share') {
      fireEvent.click(screen.getByRole('button', { name: '刷新访问凭据和发布版本' }));
      await waitFor(() => expect(fetcher.mock.calls.filter(([url]) => url.endsWith('/exchange'))).toHaveLength(4));
    }
    expect(fetcher.mock.calls.some(([url]) => url.endsWith('/accept') || url.includes(token))).toBe(false);
    expect(container.innerHTML).not.toContain(token);
  } finally { router.dispose(); }
});
it.each(['/tactics', '/account', '/tactics/example', '/tactics/example/present'])('does not mount account requests when disabled at %s', async path => {
  const fetcher = vi.fn(async () => ({ ok: true, json: async () => ({ tactics_enabled: false }) }));
  vi.stubGlobal('fetch', fetcher);
  render(<MemoryRouter initialEntries={[path]}><App /></MemoryRouter>);
  expect(await screen.findByText('战术本暂未开放，请稍后再来。')).toBeVisible();
  expect(screen.queryByRole('button', { name: '注册' })).not.toBeInTheDocument();
  expect(fetcher).toHaveBeenCalledTimes(1);
  expect(fetcher).toHaveBeenCalledWith('/api/v1/features', expect.objectContaining({ cache: 'no-store', credentials: 'same-origin' }));
});
it('fails closed on malformed capability response and explicitly retries', async () => {
  const fetcher = vi.fn().mockResolvedValueOnce({ ok: true, json: async () => ({ tactics_enabled: 'true' }) }).mockResolvedValueOnce({ ok: true, json: async () => ({ tactics_enabled: false }) });
  vi.stubGlobal('fetch', fetcher);
  render(<MemoryRouter initialEntries={['/tactics']}><App /></MemoryRouter>);
  expect(await screen.findByRole('alert')).toHaveTextContent('请重试');
  fireEvent.click(screen.getByRole('button', { name: '重试' }));
  expect(await screen.findByText('战术本暂未开放，请稍后再来。')).toBeVisible();
  expect(fetcher).toHaveBeenCalledTimes(2);
});
it('keeps teaching independent before first entering the lazy tactical route', async () => {
  const fetcher = vi.fn(async (url: string) => ({ ok: true, status: 200, headers: new Headers(), json: async () => url.endsWith('/features') ? { tactics_enabled: false } : { map: { slug: 'mirage', name_zh: 'Mirage', asset_url: '/assets/maps/mirage.svg', asset_status: 'placeholder' }, published_lineup_count: 0, targets: [] } }));
  vi.stubGlobal('fetch', fetcher);
  const router = createMemoryRouter([{ path: '*', element: <App /> }], { initialEntries: ['/'] });
  render(<RouterProvider router={router} />);
  expect(await screen.findByText('0 条可用路线')).toBeVisible();
  expect(fetcher.mock.calls.some(([url]) => url.includes('/features') || url.includes('/auth/'))).toBe(false);
  await act(async () => { await router.navigate('/tactics'); });
  expect(await screen.findByText('战术本暂未开放，请稍后再来。')).toBeVisible();
  await act(async () => { await router.navigate('/'); });
  expect(await screen.findByRole('heading', { name: '找到烟点，执行下一回合。' })).toBeVisible();
  expect(fetcher.mock.calls.filter(([url]) => url.endsWith('/features'))).toHaveLength(1);
  router.dispose();
});
