import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, expect, it, vi } from 'vitest';
import Notebook from '../src/tactics/Notebook';
const session = { user: { id: 'u', username: 'player', display_name: '玩家' }, csrf_token: 'csrf-from-session' };
const notebook = { id: 'book', kind: 'personal' as const, user_id: 'u', team_id: null };
const item = { id: 'one', notebook_id: 'book', title: 'A进攻', side: 'T', map: 'mirage', revision: 3, updated_at: '', archived_at: null, permissions: { read: true, edit: true, manage: true, publish: true } };
const reply = (data: unknown) => ({ ok: true, status: 200, headers: new Headers(), json: async () => data });
afterEach(() => vi.unstubAllGlobals());
it.each([true, false])('uses effective create=%s for the active empty-state copy without changing search or archive states', async create => {
  const fetcher = vi.fn(async (_url: string, options?: RequestInit) => {
    expect(options?.method).toBe('GET');
    return reply({ items: [], next_cursor: null, permissions: { read: true, create, manage: true, publish: false } });
  });
  vi.stubGlobal('fetch', fetcher);
  render(<MemoryRouter><Notebook notebook={{ ...notebook, kind: 'team', user_id: null, team_id: 'team' }} session={session} /></MemoryRouter>);
  const expected = create ? '还没有战术，创建第一份战术吧。' : '还没有可查看的战术。';
  expect(await screen.findByText(expected)).toBeVisible();
  expect(screen.queryByText(create ? '还没有可查看的战术。' : '还没有战术，创建第一份战术吧。')).not.toBeInTheDocument();
  expect(Boolean(screen.queryByRole('button', { name: '新建战术' }))).toBe(create);
  fireEvent.change(screen.getByLabelText('搜索战术名称'), { target: { value: '不存在' } });
  fireEvent.click(screen.getByRole('button', { name: '搜索' }));
  expect(await screen.findByText('没有匹配的战术。')).toBeVisible();
  fireEvent.change(screen.getByLabelText('搜索战术名称'), { target: { value: '' } });
  fireEvent.click(screen.getByRole('button', { name: '搜索' }));
  await screen.findByText(expected);
  fireEvent.click(screen.getByRole('button', { name: '已归档' }));
  expect(await screen.findByText('已归档为空。')).toBeVisible();
  expect(fetcher).toHaveBeenCalledTimes(4);
});
it.each(['personal', 'team'] as const)('labels the %s notebook without changing directory requests or create ownership', async kind => {
  const info = { ...notebook, kind, user_id: kind === 'personal' ? 'u' : null, team_id: kind === 'team' ? 'team-one' : null };
  const fetcher = vi.fn(async (url: string, options?: RequestInit) => {
    expect(url).toMatch(/^\/api\/v1\/notebooks\/book\/tactics\?/);
    expect(options?.method).toBe('GET');
    return reply({ items: [], next_cursor: null, permissions: { read: true, create: true, manage: true, publish: false } });
  });
  vi.stubGlobal('fetch', fetcher);
  render(<MemoryRouter><Notebook notebook={info} session={session} /></MemoryRouter>);
  const directory = screen.getByRole('region', { name: kind === 'team' ? '队伍战术目录' : '个人战术目录' });
  const activeLabel = kind === 'team' ? '共享战术' : '我的战术';
  expect(within(directory).getByRole('heading', { name: activeLabel })).toBeVisible();
  expect(await within(directory).findByRole('button', { name: activeLabel })).toHaveAttribute('aria-pressed', 'true');
  if (kind === 'team') {
    expect(screen.queryByRole('region', { name: '个人战术目录' })).not.toBeInTheDocument();
    expect(within(directory).queryByRole('button', { name: '我的战术' })).not.toBeInTheDocument();
  }
  fireEvent.click(within(directory).getByRole('button', { name: '新建战术' }));
  expect(within(screen.getByRole('dialog', { name: '新建战术' })).getByText(kind === 'team' ? '归属：当前队伍共享战术本。地图：Mirage。' : '归属：我的个人战术本。地图：Mirage。')).toBeVisible();
  fireEvent.click(screen.getByRole('button', { name: '关闭新建战术' }));
  fireEvent.click(within(directory).getByRole('button', { name: '已归档' }));
  expect(await within(directory).findByText('已归档为空。')).toBeVisible();
  expect(fetcher.mock.calls.map(([url]) => url)).toEqual(['/api/v1/notebooks/book/tactics?deleted=false&q=&limit=20', '/api/v1/notebooks/book/tactics?deleted=true&q=&limit=20']);
  expect(fetcher.mock.calls.every(([, options]) => options?.method === 'GET')).toBe(true);
});

it('opens archived content through its authorized read endpoint without joining collaboration or writing', async () => {
  vi.stubGlobal('matchMedia', vi.fn(() => ({ matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn() })));
  const projection = { structure: { schema_kind: 'stage-board', schema_version: 2, document_id: item.id, map: 'mirage', side: 'T', roles: [{ id: 'role', number: 1, color: '#cb5a42' }], stages: [{ id: 'stage', version: 1, initial_positions: { role: { x: 50, y: 50, version: 1 } }, actions: [], lineup_ids: [] }] }, fields: { 'tactic.purpose': '归档时目的', 'stage.stage.name': '归档阶段' } };
  const fetcher = vi.fn(async (url: string) => url.endsWith('/archived') ? reply({ tactic: { ...item, archived_at: '2026-11-01' }, epoch: 'epoch', projection, permissions: { read: true, edit: false, manage: true, publish: false } }) : reply({ items: [item], next_cursor: null, permissions: { read: true, create: true, manage: true, publish: true } }));
  vi.stubGlobal('fetch', fetcher);
  render(<MemoryRouter><Notebook notebook={notebook} session={session} /></MemoryRouter>);
  fireEvent.click(await screen.findByRole('button', { name: '已归档' }));
  const archiveTrigger = await screen.findByRole('button', { name: `查看归档：${item.title}` });
  fireEvent.click(archiveTrigger);
  const dialog = await screen.findByRole('dialog', { name: '归档战术只读详情' });
  expect(await within(dialog).findByText('归档时目的')).toBeVisible();
  expect(within(dialog).queryByRole('button', { name: '新增阶段' })).not.toBeInTheDocument();
  expect(fetcher.mock.calls.filter(([url]) => url.endsWith('/archived'))).toHaveLength(1);
  expect(fetcher.mock.calls.some(([url]) => url.includes('collaboration-ticket'))).toBe(false);
  fireEvent.click(within(dialog).getByRole('button', { name: '关闭归档战术只读详情' }));
  expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  expect(document.activeElement).toBe(archiveTrigger);
});
it('creates from the explicit new-tactic dialog using the existing notebook and side contract', async () => {
  const fetcher = vi.fn(async (_url: string, options?: RequestInit) => options?.method === 'POST' ? reply({ tactic: item }) : reply({ items: [], next_cursor: null, permissions: { read: true, create: true, manage: true, publish: true } }));
  vi.stubGlobal('fetch', fetcher);
  render(<MemoryRouter><Notebook notebook={notebook} session={session} /></MemoryRouter>);
  const createTrigger = await screen.findByRole('button', { name: '新建战术' });
  fireEvent.click(createTrigger);
  const close = screen.getByRole('button', { name: '关闭新建战术' });
  close.focus();
  fireEvent.click(close);
  expect(document.activeElement).toBe(createTrigger);
  fireEvent.click(createTrigger);
  const dialog = screen.getByRole('dialog', { name: '新建战术' });
  fireEvent.change(within(dialog).getByLabelText('战术名称'), { target: { value: '新战术' } });
  fireEvent.change(within(dialog).getByLabelText('阵营'), { target: { value: 'CT' } });
  fireEvent.submit(within(dialog).getByRole('button', { name: '创建战术' }).closest('form')!);
  await waitFor(() => expect(fetcher.mock.calls.some(([, options]) => options?.method === 'POST')).toBe(true));
  const mutation = fetcher.mock.calls.find(([, options]) => options?.method === 'POST')!;
  expect(mutation[0]).toBe('/api/v1/notebooks/book/tactics');
  expect(JSON.parse(mutation[1]!.body as string)).toEqual({ title: '新战术', side: 'CT' });
  expect(mutation[1]!.headers).toMatchObject({ 'X-CSRF-Token': session.csrf_token, 'Idempotency-Key': expect.any(String) });
});
it('searches and pages real directory requests and restores archived tactics using CSRF and idempotency', async () => {
  const fetcher = vi.fn(async (url: string, options?: RequestInit) => {
    if (options?.method === 'POST') return reply(item);
    return reply({ items: [item], next_cursor: url.includes('cursor=') ? null : 'page-two', permissions: { read: true, create: true, manage: true, publish: true } });
  });
  vi.stubGlobal('fetch', fetcher);
  render(<MemoryRouter><Notebook notebook={notebook} session={session} /></MemoryRouter>);
  expect(await screen.findByRole('link', { name: item.title })).toBeVisible();
  fireEvent.click(screen.getByRole('button', { name: '下一页' }));
  await waitFor(() => expect(fetcher.mock.calls.some(([url]) => url.includes('cursor=page-two'))).toBe(true));
  await screen.findByRole('link', { name: item.title });
  fireEvent.click(screen.getByRole('button', { name: '已归档' }));
  fireEvent.click(await screen.findByRole('button', { name: `恢复 ${item.title}` }));
  await waitFor(() => expect(fetcher.mock.calls.some(([url]) => url.endsWith('/restore'))).toBe(true));
  const mutation = fetcher.mock.calls.find(([url]) => url.endsWith('/restore'))![1]!;
  expect(mutation.headers).toMatchObject({ 'X-CSRF-Token': session.csrf_token, 'Idempotency-Key': expect.stringMatching(/^[0-9a-f-]{36}$/) });
  expect(mutation.body).toBe('{}');
});
it('requires explicit archive confirmation with saved revision and does not retry an uncertain write', async () => {
  const fetcher = vi.fn(async (_url: string, options?: RequestInit) => {
    if (options?.method === 'POST') throw new TypeError('response lost');
    return reply({ items: [item], next_cursor: null, permissions: { read: true, create: true, manage: true, publish: true } });
  });
  vi.stubGlobal('fetch', fetcher);
  render(<MemoryRouter><Notebook notebook={notebook} session={session} /></MemoryRouter>);
  fireEvent.click(await screen.findByRole('button', { name: `归档 ${item.title}` }));
  expect(fetcher).toHaveBeenCalledTimes(1);
  fireEvent.click(screen.getByRole('button', { name: '确认归档' }));
  expect(await screen.findByRole('alert')).toHaveTextContent('操作可能已成功');
  expect(screen.getByRole('button', { name: '确认归档' })).toBeDisabled();
  const mutation = fetcher.mock.calls.find(([, options]) => options?.method === 'POST')![1]!;
  expect(JSON.parse(mutation.body as string)).toEqual({ expected_revision: 3 });
  expect(fetcher).toHaveBeenCalledTimes(2);
});
