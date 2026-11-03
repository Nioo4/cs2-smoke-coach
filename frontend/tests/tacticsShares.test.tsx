import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { createBrowserRouter, MemoryRouter, RouterProvider } from 'react-router-dom';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import App from '../src/app/App';
import ShareManagement from '../src/tactics/ShareManagement';
import TacticHistory from '../src/tactics/TacticHistory';
import type { TacticProjection } from '../src/tactics/editorTypes';
const token = 'A'.repeat(43);
const session = { user: { id: 'u', username: 'player', display_name: '玩家' }, csrf_token: 'csrf' };
const projection: TacticProjection = { structure: { schema_kind: 'stage-board', schema_version: 2, document_id: 'private-id', map: 'mirage', side: 'T', roles: [{ id: 'r', number: 1, color: '#cb5a42' }], stages: [{ id: 'stage', version: 1, initial_positions: { r: { x: 50, y: 50, version: 1 } }, actions: [], lineup_ids: [] }] }, fields: { 'stage.stage.name': '发布阶段', 'stage.stage.description': '发布时说明' } };
const manifest = { id: 'share', version_id: 'version-one', allow_copy: false, published_at: '2026-10-30', items: [{ item_id: 'item-one', title: '发布战术一', side: 'T', map: 'mirage', source_revision: 3 }, { item_id: 'item-two', title: '发布战术二', side: 'T', map: 'mirage', source_revision: 4 }] };
function response(value: unknown, status = 200) { return { ok: status < 400, status, headers: new Headers(), json: async () => value }; }
function publicRead(url: string, options?: RequestInit) {
  if (url.endsWith('/features')) return response({ tactics_enabled: true });
  if (url.endsWith('/shares/exchange')) return response({ read_grant: 'grant', expires_in: 300 });
  if (url.endsWith('/shares/read')) return response(manifest);
  if (url.endsWith('/shares/items')) { const body = JSON.parse(options?.body as string); return response({ version_id: 'version-one', items: [{ item_id: body.cursor ? 'item-two' : 'item-one', projection }], next_cursor: body.cursor ? null : 'next' }); }
  throw Error('unexpected private call');
}
beforeEach(() => vi.stubGlobal('matchMedia', vi.fn(() => ({ matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn() }))));
afterEach(() => { vi.unstubAllGlobals(); window.history.replaceState(null, '', '/'); });
function renderShare() { window.history.replaceState(null, '', `/share#token=${token}`); render(<MemoryRouter initialEntries={[`/share#token=${token}`]}><App /></MemoryRouter>); }
it('opens the original copy area without a new hash entry or losing inline login in the real browser router', async () => {
  const fetcher = vi.fn(async (url: string, options?: RequestInit) => {
    if (url.endsWith('/shares/read')) return response({ ...manifest, allow_copy: true });
    if (url.endsWith('/auth/session')) return response({ error: { code: 'USER_AUTH_REQUIRED' } }, 401);
    return publicRead(url, options);
  }); vi.stubGlobal('fetch', fetcher);
  window.history.replaceState(null, '', `/share#token=${token}`);
  const router = createBrowserRouter([{ path: '*', element: <App /> }]);
  const view = render(<RouterProvider router={router} />);
  try {
    await screen.findByRole('heading', { level: 1, name: '发布战术一' });
    fireEvent.click(screen.getByRole('button', { name: '确认登录以继续复制' }));
    const username = await screen.findByLabelText('用户名');
    fireEvent.change(username, { target: { value: 'fixture-user' } });
    const target = document.querySelector<HTMLElement>('#shared-copy')!;
    target.scrollIntoView = vi.fn();
    await act(async () => {
      fireEvent.click(screen.getByText('复制为独立战术'));
      await new Promise(resolve => setTimeout(resolve, 0)); // Native anchor navigation, if present, dispatches asynchronously.
    });
    expect(window.location.hash).toBe('');
    expect(screen.getByRole('heading', { level: 1, name: '发布战术一' })).toBeVisible();
    expect(screen.getByLabelText('用户名')).toBe(username);
    expect(username).toHaveValue('fixture-user');
    expect(target.scrollIntoView).toHaveBeenCalledWith({ block: 'start' });
    expect(document.activeElement).toBe(target);
    expect(fetcher.mock.calls.filter(([url]) => url.endsWith('/shares/exchange'))).toHaveLength(1);
    fireEvent.click(screen.getByRole('button', { name: '刷新访问凭据和发布版本' }));
    await waitFor(() => expect(fetcher.mock.calls.filter(([url]) => url.endsWith('/shares/exchange'))).toHaveLength(2));
    expect(await screen.findByRole('heading', { level: 1, name: '发布战术一' })).toBeVisible();
    const exchanges = fetcher.mock.calls.filter(([url]) => url.endsWith('/shares/exchange'));
    expect(exchanges.map(([, options]) => JSON.parse(options?.body as string).token)).toEqual([token, token]);
  } finally { view.unmount(); router.dispose(); }
});
it('distinguishes current, other and whole-notebook shares and confirms the exact revoke target', async () => {
  const rows = [
    { id: 'share-current', tactic_id: 'current', created_at: '2026-11-01T10:00:00Z', updated_at: '2026-11-02T10:00:00Z' },
    { id: 'share-other', tactic_id: 'other', created_at: '2026-10-30T10:00:00Z', updated_at: '2026-10-31T10:00:00Z' },
    { id: 'share-whole', tactic_id: null, created_at: '2026-10-29T10:00:00Z', updated_at: '2026-10-29T10:00:00Z' },
  ].map(row => ({ ...row, notebook_id: 'book', version_id: 'version', allow_copy: false, revoked_at: null }));
  const fetcher = vi.fn(async (url: string, options?: RequestInit) => {
    if (url.includes('/shares?')) return response({ items: rows, next_cursor: null });
    if (url.endsWith('/revoke') && options?.method === 'POST') return response({ ok: true });
    throw Error('unexpected request');
  });
  vi.stubGlobal('fetch', fetcher);
  render(<ShareManagement notebookId="book" tacticId="current" session={session} />);
  fireEvent.click(screen.getByRole('button', { name: '打开发布分享管理' }));
  const other = await screen.findByRole('button', { name: /撤销.*本战术本其他战术.*share-other/ });
  expect(screen.getByRole('button', { name: /准备更新.*当前战术.*share-current/ })).toBeVisible();
  expect(screen.getByRole('button', { name: /准备更新.*整本分享.*share-whole/ })).toBeVisible();
  expect(document.querySelectorAll('time[datetime]')).toHaveLength(6);
  fireEvent.click(other);
  expect(screen.getByRole('alertdialog')).toHaveTextContent('share-other');
  expect(screen.getByRole('alertdialog')).toHaveTextContent('本战术本其他战术');
  expect(fetcher.mock.calls.some(([, options]) => options?.method === 'POST')).toBe(false);
  fireEvent.click(screen.getByRole('button', { name: '确认撤销分享' }));
  await screen.findByRole('button', { name: /撤销.*本战术本其他战术.*share-other/ });
  expect(fetcher.mock.calls.find(([, options]) => options?.method === 'POST')?.[0]).toBe('/api/v1/shares/share-other/revoke');
});
it('reads anonymously, scrubs the fragment, loads only one item and caches explicitly requested next items', async () => {
  const fetcher = vi.fn(async (url: string, options?: RequestInit) => publicRead(url, options)); vi.stubGlobal('fetch', fetcher); renderShare();
  expect(await screen.findByRole('heading', { name: '发布战术一' })).toBeVisible();
  expect(screen.getByRole('heading', { level: 1, name: '发布战术一' })).toBeVisible();
  expect(screen.getAllByRole('heading', { level: 1 })).toHaveLength(1);
  const header = screen.getByRole('heading', { level: 1, name: '发布战术一' }).closest('header')!;
  expect(within(header).getByText('作者发布的保存版本 3')).toBeVisible();
  expect(within(header).getByText(/只读分享/)).toBeVisible();
  expect(within(header).getByRole('button', { name: '刷新访问凭据和发布版本' })).toBeEnabled();
  expect(within(header).getByRole('link', { name: '返回教学页' })).toHaveAttribute('href', '/');
  expect(screen.queryByRole('link', { name: '复制为独立战术' })).not.toBeInTheDocument();
  expect(screen.queryByRole('button', { name: '复制为独立战术' })).not.toBeInTheDocument();
  expect(screen.getByText('作者未允许复制此发布版本。')).toBeVisible();
  expect(window.location.hash).toBe(''); expect(fetcher.mock.calls.some(([url]) => url.includes('/auth/') || url.includes('/tactics/'))).toBe(false);
  expect(fetcher.mock.calls.filter(([url]) => url.endsWith('/items'))).toHaveLength(1);
  for (const [url, options] of fetcher.mock.calls) { expect(url).not.toContain(token); if (url.includes('/shares/')) expect(options?.headers).not.toHaveProperty('X-CSRF-Token'); }
  fireEvent.click(screen.getByRole('button', { name: '读取下一份战术' }));
  expect(await screen.findByRole('heading', { name: '发布战术二' })).toBeVisible();
  expect(screen.getByRole('heading', { level: 1, name: '发布战术二' })).toBeVisible();
  expect(screen.getByText('作者发布的保存版本 4')).toBeVisible();
  fireEvent.click(screen.getByRole('button', { name: '发布战术一' }));
  expect(screen.getByRole('heading', { name: '发布战术一' })).toBeVisible();
  expect(screen.getByText('作者发布的保存版本 3')).toBeVisible();
  expect(fetcher.mock.calls.filter(([url]) => url.endsWith('/items'))).toHaveLength(2);
});
it('retains the cached version after a next-page version conflict without concatenating or auto-refreshing', async () => {
  const fetcher = vi.fn(async (url: string, options?: RequestInit) => url.endsWith('/items') && JSON.parse(options?.body as string).cursor ? response({ error: { code: 'SHARE_VERSION_CHANGED', message: 'updated' } }, 409) : publicRead(url, options)); vi.stubGlobal('fetch', fetcher); renderShare();
  await screen.findByRole('heading', { name: '发布战术一' }); fireEvent.click(screen.getByRole('button', { name: '读取下一份战术' }));
  expect(await screen.findByRole('alert')).toHaveTextContent('作者已更新');
  expect(screen.getByRole('heading', { name: '发布战术一' })).toBeVisible();
  expect(screen.getByRole('button', { name: '读取下一份战术' })).toBeDisabled();
  expect(fetcher.mock.calls.filter(([url]) => url.endsWith('/exchange'))).toHaveLength(1);
});
it('keeps token in component memory through inline login and copies only after a second explicit confirmation', async () => {
  let loggedIn = false;
  const fetcher = vi.fn(async (url: string, options?: RequestInit) => {
    if (url.endsWith('/shares/read')) return response({ ...manifest, allow_copy: true });
    if (url.endsWith('/auth/session')) return loggedIn ? response(session) : response({ error: { code: 'USER_AUTH_REQUIRED', message: 'login' } }, 401);
    if (url.endsWith('/auth/login')) { loggedIn = true; return response(session); }
    if (url.endsWith('/shares/copy')) return response({ tactics: [{ id: 'new-copy', title: '独立副本' }], source_version_id: 'version-one' }, 201);
    return publicRead(url, options);
  }); vi.stubGlobal('fetch', fetcher); renderShare(); await screen.findByRole('heading', { name: '发布战术一' });
  fireEvent.click(screen.getByRole('button', { name: '确认登录以继续复制' }));
  fireEvent.submit((await screen.findByLabelText('用户名')).closest('form')!);
  const confirm = await screen.findByRole('button', { name: '确认复制这个发布版本' });
  expect(fetcher.mock.calls.some(([url]) => url.endsWith('/shares/copy'))).toBe(false);
  fireEvent.click(confirm); expect(await screen.findByRole('link', { name: '独立副本' })).toHaveAttribute('href', '/tactics/new-copy');
  const mutation = fetcher.mock.calls.find(([url]) => url.endsWith('/shares/copy'))![1]!;
  expect(JSON.parse(mutation.body as string)).toEqual({ read_grant: 'grant', expected_version_id: 'version-one' });
  expect(mutation.headers).toMatchObject({ 'X-CSRF-Token': 'csrf', 'Idempotency-Key': expect.any(String) });
  expect(fetcher.mock.calls.some(([url]) => url.includes(token))).toBe(false);
});
it('reviews the entire active notebook set before creating a default-no-copy share', async () => {
  const fetcher = vi.fn(async (url: string, options?: RequestInit) => {
    if (url.includes('/shares?')) return response({ items: [], next_cursor: null });
    if (url.includes('/tactics?')) return response({ items: [{ id: 'tactic', title: '已确认标题', revision: 9 }], next_cursor: null, permissions: { publish: true } });
    if (url.endsWith('/shares') && options?.method === 'POST') return response({ id: 'share', version_id: 'version', notebook_id: 'book', tactic_id: null, allow_copy: false, token, token_available: true }, 201);
    throw Error('unexpected request');
  }); vi.stubGlobal('fetch', fetcher);
  render(<ShareManagement notebookId="book" session={session} />);
  fireEvent.click(screen.getByRole('button', { name: '打开发布分享管理' })); fireEvent.click(screen.getByRole('button', { name: '准备发布整本战术' }));
  expect(await screen.findByText('已确认标题 · 版本 9')).toBeVisible();
  expect(fetcher.mock.calls.some(([, options]) => options?.method === 'POST')).toBe(false);
  expect(screen.getByRole('button', { name: '确认发布这些保存版本' })).toHaveClass('button-primary');
  fireEvent.click(screen.getByRole('button', { name: '确认发布这些保存版本' }));
  expect(await screen.findByLabelText('分享链接')).toHaveValue(`${window.location.origin}/share#token=${token}`);
  const mutation = fetcher.mock.calls.find(([url]) => url.endsWith('/shares'))![1]!;
  expect(JSON.parse(mutation.body as string)).toEqual({ notebook_id: 'book', tactic_id: null, allow_copy: false, expected_revisions: [{ id: 'tactic', revision: 9 }] });
});
it('marks only the selected historical ID and clears selection on explicit history refresh', async () => {
  const versions = ['first', 'second'].map((id, index) => ({ id, source_epoch: 'old-epoch', source_revision: index + 2, reason: 'periodic', created_at: '2026-10-30', expires_at: '2026-11-29' }));
  const fetcher = vi.fn(async (url: string) => {
    if (url.includes('/versions?')) return response({ items: versions, next_cursor: null });
    if (url.includes('/versions/')) return response({ version: versions.find(version => url.endsWith(`/${version.id}`)), projection });
    if (url.endsWith('/notebooks/personal')) return response({ notebook: { id: 'personal' } });
    if (url.includes('/teams?')) return response({ items: [], next_cursor: null });
    throw Error('unexpected request');
  }); vi.stubGlobal('fetch', fetcher);
  render(<MemoryRouter><TacticHistory tacticId="tactic" session={session} /></MemoryRouter>);
  fireEvent.click(screen.getByRole('button', { name: '查看历史版本' }));
  const buttons = await screen.findAllByRole('button', { name: '查看此历史快照' });
  expect(buttons.every(button => button.getAttribute('aria-pressed') === 'false')).toBe(true);
  fireEvent.click(buttons[1]);
  expect(await screen.findByText('历史保存版本 3')).toBeVisible();
  expect(buttons[1]).toHaveAttribute('aria-pressed', 'true');
  expect(buttons[0]).toHaveAttribute('aria-pressed', 'false');
  fireEvent.click(buttons[0]);
  expect(await screen.findByText('历史保存版本 2')).toBeVisible();
  expect(buttons[0]).toHaveAttribute('aria-pressed', 'true');
  expect(buttons[1]).toHaveAttribute('aria-pressed', 'false');
  fireEvent.click(screen.getByRole('button', { name: '刷新历史' }));
  const refreshed = await screen.findAllByRole('button', { name: '查看此历史快照' });
  expect(refreshed.every(button => button.getAttribute('aria-pressed') === 'false')).toBe(true);
  expect(fetcher.mock.calls.every(([url]) => !url.endsWith('/restore-copy'))).toBe(true);
});
it('uses an immutable historical projection and restores to a returned new ID without editing current document', async () => {
  const version = { id: 'history', source_epoch: 'old-epoch', source_revision: 2, reason: 'periodic', created_at: '2026-10-30', expires_at: '2026-11-29' };
  const fetcher = vi.fn(async (url: string, options?: RequestInit) => {
    if (url.includes('/versions?')) return response({ items: [version], next_cursor: null });
    if (url.endsWith('/versions/history')) return response({ version, projection });
    if (url.endsWith('/notebooks/personal')) return response({ notebook: { id: 'personal' } });
    if (url.includes('/teams?')) return response({ items: [], next_cursor: null });
    if (url.endsWith('/restore-copy') && options?.method === 'POST') return response({ id: 'history-copy' }, 201);
    throw Error('unexpected current document call');
  }); vi.stubGlobal('fetch', fetcher);
  render(<MemoryRouter><TacticHistory tacticId="tactic" session={session} /></MemoryRouter>);
  fireEvent.click(screen.getByRole('button', { name: '查看历史版本' })); fireEvent.click(await screen.findByRole('button', { name: '查看此历史快照' }));
  expect(await screen.findByText('历史保存版本 2')).toBeVisible();
  await screen.findByRole('option', { name: '我的个人战术本' }); fireEvent.change(screen.getByLabelText('历史副本目标'), { target: { value: 'personal' } });
  fireEvent.click(screen.getByRole('button', { name: '恢复为新战术副本' }));
  expect(await screen.findByRole('link', { name: '打开恢复的新副本' })).toHaveAttribute('href', '/tactics/history-copy');
  expect(fetcher.mock.calls.some(([url]) => url.endsWith('/tactics/tactic'))).toBe(false);
});
it('routes anonymous teaching through shares/lineups, never through a private tactic resolver', async () => {
  const withReference = structuredClone(projection); withReference.structure.stages[0].lineup_ids = ['lineup'];
  const fetcher = vi.fn(async (url: string, options?: RequestInit) => {
    if (url.endsWith('/shares/items')) return response({ version_id: 'version-one', items: [{ item_id: 'item-one', projection: withReference }], next_cursor: null });
    if (url.endsWith('/shares/lineups')) return response({ id: 'lineup', available: false, lineup: null });
    return publicRead(url, options);
  }); vi.stubGlobal('fetch', fetcher); renderShare();
  expect(await screen.findByText(/原引用仍保留/)).toBeVisible();
  const request = fetcher.mock.calls.find(([url]) => url.endsWith('/shares/lineups'))![1]!;
  expect(JSON.parse(request.body as string)).toEqual({ read_grant: 'grant', version_id: 'version-one', item_id: 'item-one', lineup_id: 'lineup' });
  expect(fetcher.mock.calls.some(([url]) => url.includes('/tactics/'))).toBe(false);
});
it('clears rendered content when a new read reports the share unavailable instead of keeping an accessible-looking snapshot', async () => {
  const fetcher = vi.fn(async (url: string, options?: RequestInit) => url.endsWith('/items') && JSON.parse(options?.body as string).cursor ? response({ error: { code: 'SHARE_UNAVAILABLE', message: 'unavailable' } }, 404) : publicRead(url, options)); vi.stubGlobal('fetch', fetcher); renderShare();
  await screen.findByRole('heading', { name: '发布战术一' }); fireEvent.click(screen.getByRole('button', { name: '读取下一份战术' }));
  expect(await screen.findByRole('alert')).toHaveTextContent('分享已不可用');
  expect(screen.queryByRole('heading', { name: '发布战术一' })).not.toBeInTheDocument();
  expect(screen.queryByRole('button', { name: '自动演示' })).not.toBeInTheDocument();
});
it('requires a newly reviewed source set after a publication conflict, with no automatic second write', async () => {
  let revision = 1; let writes = 0;
  const fetcher = vi.fn(async (url: string, options?: RequestInit) => {
    if (url.includes('/shares?')) return response({ items: [], next_cursor: null });
    if (url.includes('/tactics?')) return response({ items: [{ id: 'source', title: '来源', revision }], next_cursor: null, permissions: { publish: true } });
    if (url.endsWith('/shares') && options?.method === 'POST') { writes += 1; revision = 2; return response({ error: { code: 'SHARE_SOURCE_CHANGED', message: '保存内容已变化，请重新确认' } }, 409); }
    throw Error('unexpected request');
  }); vi.stubGlobal('fetch', fetcher); render(<ShareManagement notebookId="book" session={session} />);
  fireEvent.click(screen.getByRole('button', { name: '打开发布分享管理' })); fireEvent.click(screen.getByRole('button', { name: '准备发布整本战术' })); await screen.findByText('来源 · 版本 1');
  fireEvent.click(screen.getByRole('button', { name: '确认发布这些保存版本' })); expect(await screen.findByRole('alert')).toHaveTextContent('重新确认');
  expect(screen.queryByRole('button', { name: '确认发布这些保存版本' })).not.toBeInTheDocument(); expect(writes).toBe(1);
  fireEvent.click(screen.getByRole('button', { name: '准备发布整本战术' })); expect(await screen.findByText('来源 · 版本 2')).toBeVisible(); expect(writes).toBe(1);
});
it('explains a lost create token response and reconciles the same key without recreating a link', async () => {
  let writes = 0;
  const fetcher = vi.fn(async (url: string, options?: RequestInit) => {
    if (url.includes('/shares?')) return response({ items: [], next_cursor: null });
    if (url.includes('/tactics?')) return response({ items: [{ id: 'source', title: '来源', revision: 1 }], next_cursor: null, permissions: { publish: true } });
    if (url.endsWith('/shares') && options?.method === 'POST') { if (++writes === 1) throw TypeError('lost'); return response({ id: 'same-share', version_id: 'version', notebook_id: 'book', tactic_id: null, allow_copy: false, token: null, token_available: false }); }
    throw Error('unexpected request');
  }); vi.stubGlobal('fetch', fetcher); render(<ShareManagement notebookId="book" session={session} />);
  fireEvent.click(screen.getByRole('button', { name: '打开发布分享管理' })); fireEvent.click(screen.getByRole('button', { name: '准备发布整本战术' })); await screen.findByText('来源 · 版本 1');
  fireEvent.click(screen.getByRole('button', { name: '确认发布这些保存版本' })); await screen.findByRole('button', { name: '按原请求核对发布结果' });
  fireEvent.click(screen.getByRole('button', { name: '关闭发布与固定版本分享' }));
  fireEvent.click(screen.getByRole('button', { name: '打开发布分享管理' }));
  fireEvent.click(await screen.findByRole('button', { name: '按原请求核对发布结果' }));
  expect(await screen.findByText(/原链接不能再次取回/)).toBeVisible(); expect(screen.queryByLabelText('分享链接')).not.toBeInTheDocument();
  const requests = fetcher.mock.calls.filter(([, options]) => options?.method === 'POST'); expect(requests).toHaveLength(2); expect(requests[0][1]?.headers).toEqual(requests[1][1]?.headers); expect(requests[0][1]?.body).toEqual(requests[1][1]?.body);
});
it('retains an uncertain copy across an explicit grant refresh and reconciles its original version and key', async () => {
  let grants = 0; let copies = 0;
  const fetcher = vi.fn(async (url: string, options?: RequestInit) => {
    if (url.endsWith('/shares/exchange')) return response({ read_grant: `grant-${++grants}`, expires_in: 300 });
    if (url.endsWith('/shares/read')) return response({ ...manifest, allow_copy: true });
    if (url.endsWith('/auth/session')) return response(session);
    if (url.endsWith('/shares/copy')) { if (++copies === 1) throw TypeError('lost'); return response({ tactics: [{ id: 'same-copy', title: '原请求副本' }], source_version_id: manifest.version_id }, 201); }
    return publicRead(url, options);
  }); vi.stubGlobal('fetch', fetcher); renderShare(); await screen.findByRole('heading', { name: '发布战术一' });
  fireEvent.click(screen.getByRole('button', { name: '确认登录以继续复制' })); fireEvent.click(await screen.findByRole('button', { name: '确认复制这个发布版本' }));
  await screen.findByRole('button', { name: '按原请求核对副本' }); fireEvent.click(screen.getByRole('button', { name: '刷新访问凭据和发布版本' }));
  const retry = await screen.findByRole('button', { name: '按原请求核对副本' }); expect(copies).toBe(1); fireEvent.click(retry);
  expect(await screen.findByRole('link', { name: '原请求副本' })).toBeVisible();
  const requests = fetcher.mock.calls.filter(([url]) => url.endsWith('/shares/copy')); expect(requests).toHaveLength(2);
  expect(requests[0][1]?.headers).toEqual(requests[1][1]?.headers);
  expect(JSON.parse(requests[1][1]?.body as string)).toEqual({ read_grant: 'grant-2', expected_version_id: 'version-one' });
});
it('captures a different same-tab share hash as a new flow, scrubs it and retains it on explicit refresh', async () => {
  const second = 'B'.repeat(43);
  const fetcher = vi.fn(async (url: string, options?: RequestInit) => {
    if (url.endsWith('/features')) return response({ tactics_enabled: true });
    const body = JSON.parse(options?.body as string);
    if (url.endsWith('/exchange')) return response({ read_grant: body.token === token ? 'grant-a' : 'grant-b', expires_in: 300 });
    if (url.endsWith('/read')) return response({ ...manifest, version_id: body.read_grant === 'grant-a' ? 'version-a' : 'version-b', items: [{ ...manifest.items[0], title: body.read_grant === 'grant-a' ? '分享A' : '分享B' }] });
    if (url.endsWith('/items')) return response({ version_id: body.version_id, items: [{ item_id: 'item-one', projection }], next_cursor: null });
    throw Error('unexpected request');
  }); vi.stubGlobal('fetch', fetcher);
  window.history.replaceState(null, '', `/share#token=${token}`);
  const router = createBrowserRouter([{ path: '*', element: <App /> }]); const { container } = render(<RouterProvider router={router} />);
  expect(await screen.findByRole('heading', { name: '分享A' })).toBeVisible(); expect(window.location.hash).toBe('');
  await act(async () => { await router.navigate(`/share#token=${second}`); });
  expect(await screen.findByRole('heading', { name: '分享B' })).toBeVisible(); expect(window.location.hash).toBe('');
  expect(screen.queryByRole('heading', { name: '分享A' })).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole('button', { name: '刷新访问凭据和发布版本' })); await screen.findByRole('heading', { name: '分享B' });
  const exchanges = fetcher.mock.calls.filter(([url]) => url.endsWith('/exchange')).map(([, options]) => JSON.parse(options?.body as string).token);
  expect(exchanges).toEqual([token, second, second]);
  expect(container.innerHTML).not.toContain(token); expect(container.innerHTML).not.toContain(second);
  router.dispose();
});
