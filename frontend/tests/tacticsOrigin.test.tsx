import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { useState } from 'react';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, expect, it, vi } from 'vitest';
import TacticOrigin from '../src/tactics/TacticOrigin';
import TacticPublicationTools from '../src/tactics/TacticPublicationTools';
import { resolveTacticOrigin } from '../src/tactics/tacticOriginResolver';

function response(value: unknown) { return { ok: true, status: 200, headers: new Headers(), json: async () => value }; }
const personal = { notebook: { id: 'personal', kind: 'personal', user_id: 'user', team_id: null } };
afterEach(() => vi.unstubAllGlobals());

it('resolves a direct personal bookmark from authorized catalog rather than untrusted location state', async () => {
  const fetcher = vi.fn(async () => response(personal)); vi.stubGlobal('fetch', fetcher);
  render(<MemoryRouter initialEntries={[{ pathname: '/tactics/doc', state: { teamId: 'invented', notebookId: 'other' } }]}><TacticOrigin notebookId="personal" scope="doc:user" connected retry={vi.fn()} /></MemoryRouter>);
  expect(await screen.findByRole('link', { name: '返回我的个人战术本' })).toHaveAttribute('href', '/tactics');
  expect(fetcher).toHaveBeenCalledTimes(1);
});

it('walks authorized team pages to find notebook ownership and returns the exact team', async () => {
  const fetcher = vi.fn(async (raw: RequestInfo | URL) => {
    const url = String(raw);
    return response(url.endsWith('notebooks/personal') ? personal : url.includes('cursor=next') ? { items: [{ id: 'real-team', name: '真实队伍', notebook_id: 'shared' }], next_cursor: null } : { items: [{ id: 'other', name: '其他', notebook_id: 'other' }], next_cursor: 'next' });
  }); vi.stubGlobal('fetch', fetcher);
  render(<MemoryRouter><TacticOrigin notebookId="shared" scope="doc:user" connected retry={vi.fn()} /></MemoryRouter>);
  expect(await screen.findByRole('link', { name: '返回队伍：真实队伍' })).toHaveAttribute('href', '/teams/real-team');
  expect(fetcher.mock.calls.map(call => String(call[0]))).toEqual(['/api/v1/notebooks/personal', '/api/v1/teams?limit=50', '/api/v1/teams?limit=50&cursor=next']);
});

it('shows unknown on a catalog failure, never falls back to personal, and supports retry', async () => {
  const fetcher = vi.fn().mockRejectedValueOnce(Error('offline')).mockResolvedValue(response(personal)); vi.stubGlobal('fetch', fetcher);
  function Harness() { const [attempt, setAttempt] = useState(0); return <TacticOrigin notebookId="personal" scope={`doc:user:${attempt}`} connected retry={() => setAttempt(value => value + 1)} />; }
  render(<MemoryRouter><Harness /></MemoryRouter>);
  await screen.findByText('归属未知'); expect(screen.queryByRole('link')).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole('button', { name: '重新核对归属' }));
  expect(await screen.findByRole('link')).toHaveAttribute('href', '/tactics');
});

it('does not display a previous account origin while a new account scope is being checked', async () => {
  const fetcher = vi.fn().mockResolvedValueOnce(response(personal)).mockImplementation(() => new Promise(() => {})); vi.stubGlobal('fetch', fetcher);
  const { rerender } = render(<MemoryRouter><TacticOrigin notebookId="personal" scope="doc:u1" connected retry={vi.fn()} /></MemoryRouter>);
  await screen.findByRole('link');
  rerender(<MemoryRouter><TacticOrigin notebookId="personal" scope="doc:u2" connected retry={vi.fn()} /></MemoryRouter>);
  expect(screen.queryByRole('link')).not.toBeInTheDocument(); expect(screen.getByText('归属核对中…')).toBeVisible();
});

it('fails unknown on exhausted membership or repeated cursor without fabricating ownership', async () => {
  vi.stubGlobal('fetch', vi.fn(async (raw: RequestInfo | URL) => response(String(raw).endsWith('notebooks/personal') ? personal : { items: [], next_cursor: null })));
  await expect(resolveTacticOrigin('unknown-book', new AbortController().signal)).rejects.toThrow('origin_unknown');
  vi.stubGlobal('fetch', vi.fn(async (raw: RequestInfo | URL) => response(String(raw).endsWith('notebooks/personal') ? personal : { items: [], next_cursor: 'repeat' })));
  await expect(resolveTacticOrigin('unknown-book', new AbortController().signal)).rejects.toThrow('repeated_cursor');
});

it('reuses the publication permission read for the notebook identity and invalidates it on retry failure', async () => {
  const fetcher = vi.fn().mockResolvedValueOnce(response({ tactic: { notebook_id: 'shared' }, permissions: { manage: false, publish: false } })).mockRejectedValueOnce(Error('offline')); vi.stubGlobal('fetch', fetcher);
  const identity = vi.fn();
  const props = { tacticId: 'doc', session: { user: { id: 'u', username: 'name', display_name: '玩家' }, csrf_token: 'csrf' }, hasUnconfirmedWork: false, connected: true, connectionId: 'connection', readOnly: false, onNotebookId: identity };
  const { rerender } = render(<MemoryRouter><TacticPublicationTools {...props} /></MemoryRouter>);
  await waitFor(() => expect(identity).toHaveBeenLastCalledWith('shared'));
  expect(fetcher).toHaveBeenCalledTimes(1);
  await act(async () => rerender(<MemoryRouter><TacticPublicationTools {...props} contextRefresh={1} /></MemoryRouter>));
  await screen.findByText('暂时无法读取发布与历史权限，请重试。');
  expect(identity).toHaveBeenLastCalledWith(null);
});
