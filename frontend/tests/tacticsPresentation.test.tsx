import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, expect, it, vi } from 'vitest';
import App from '../src/app/App';
const session = { user: { id: 'u', username: 'player', display_name: '玩家' }, csrf_token: 'csrf' };
const saved = { tactic: { id: 'tactic', title: '真实保存标题', revision: 8 }, epoch: 'epoch', permissions: { read: true }, projection: {
  structure: { schema_kind: 'stage-board', schema_version: 2, document_id: 'tactic', map: 'mirage', side: 'T', roles: [{ id: 'r', number: 1, color: '#cb5a42' }], stages: [{ id: 'stage', version: 1, initial_positions: { r: { x: 50, y: 50, version: 1 } }, actions: [], lineup_ids: [] }] },
  fields: { 'stage.stage.name': '保存阶段', 'stage.stage.description': '保存说明' },
} };
function reply(data: unknown, status = 200) { return { ok: status < 400, status, headers: new Headers(), json: async () => data }; }
afterEach(() => vi.unstubAllGlobals());
function setup(failSecond = false) {
  let reads = 0;
  vi.stubGlobal('matchMedia', vi.fn(() => ({ matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn() })));
  const fetcher = vi.fn(async (url: string) => {
    if (url.endsWith('/features')) return reply({ tactics_enabled: true });
    if (url.endsWith('/auth/session')) return reply(session);
    if (url.endsWith('/notebooks/personal')) return reply({ notebook: { id: 'book', kind: 'personal', user_id: 'u', team_id: null } });
    if (url.endsWith('/tactics/tactic/present')) {
      reads += 1;
      if (reads > 1 && failSecond) return reply({ error: { message: 'not found' } }, 404);
      return reply({ ...saved, revision: reads > 1 ? 9 : 8, tactic: { ...saved.tactic, revision: reads > 1 ? 9 : 8 } });
    }
    throw Error('unexpected request');
  });
  vi.stubGlobal('fetch', fetcher);
  render(<MemoryRouter initialEntries={['/tactics/tactic/present']}><App /></MemoryRouter>);
  return fetcher;
}
it('routes through features/session to a real saved projection and refreshes explicitly', async () => {
  const fetcher = setup();
  expect(await screen.findByText('已保存版本 8')).toBeVisible();
  expect(screen.getByRole('heading', { level: 1, name: '真实保存标题' })).toBeVisible();
  expect(screen.getAllByRole('heading', { level: 1 })).toHaveLength(1);
  const header = screen.getByRole('heading', { level: 1, name: '真实保存标题' }).closest('header')!;
  expect(within(header).getByText('已保存版本 8')).toBeVisible();
  expect(within(header).getByRole('link', { name: '返回战术详情' })).toHaveAttribute('href', '/tactics/tactic');
  expect(within(header).getByRole('button', { name: '刷新已保存版本' })).toBeEnabled();
  expect(screen.getByText('保存说明')).toBeVisible();
  expect(screen.getByRole('button', { name: '自动演示' })).toBeEnabled();
  expect(screen.getByText(/不实时跟随草稿修改/)).toBeVisible();
  fireEvent.click(screen.getByRole('button', { name: '刷新已保存版本' }));
  expect(screen.queryByRole('heading', { name: '真实保存标题' })).not.toBeInTheDocument();
  expect(screen.getByRole('heading', { level: 1, name: '作者演示' })).toBeVisible();
  expect(screen.getByRole('button', { name: '刷新已保存版本' })).toBeDisabled();
  expect(await screen.findByText('已保存版本 9')).toBeVisible();
  expect(fetcher.mock.calls.filter(([url]) => url.endsWith('/tactics/tactic/present'))).toHaveLength(2);
});
it('removes previous saved snapshot when refreshed access fails', async () => {
  setup(true);
  await screen.findByText('已保存版本 8');
  fireEvent.click(screen.getByRole('button', { name: '刷新已保存版本' }));
  expect(await screen.findByRole('alert')).toHaveTextContent('不属于当前账户');
  await waitFor(() => expect(screen.queryByText('真实保存标题')).not.toBeInTheDocument());
  expect(screen.queryByRole('button', { name: '自动演示' })).not.toBeInTheDocument();
});
