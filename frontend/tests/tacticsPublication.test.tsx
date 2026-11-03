import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, expect, it, vi } from 'vitest';
import TacticPublicationTools from '../src/tactics/TacticPublicationTools';

const session = { user: { id: 'u', username: 'player', display_name: '玩家' }, csrf_token: 'csrf' };
const props = { tacticId: 'tactic', session, hasUnconfirmedWork: false, connected: true, connectionId: 'first', readOnly: false };
function response(manage: boolean, publish: boolean) {
  return { ok: true, status: 200, headers: new Headers(), json: async () => ({ tactic: { notebook_id: 'book' }, permissions: { manage, publish } }) };
}
afterEach(() => vi.unstubAllGlobals());

it('reports a failed permissions read and retries without hiding the route permanently', async () => {
  const fetcher = vi.fn().mockRejectedValueOnce(TypeError('temporary')).mockResolvedValueOnce(response(true, true));
  vi.stubGlobal('fetch', fetcher);
  render(<MemoryRouter><TacticPublicationTools {...props} /></MemoryRouter>);
  expect(await screen.findByRole('alert')).toHaveTextContent('暂时无法读取发布与历史权限');
  expect(screen.queryByText('正在核对发布与历史权限…')).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole('button', { name: '重试读取发布与历史权限' }));
  expect(await screen.findByRole('button', { name: '打开发布分享管理' })).toBeVisible();
  expect(screen.getByRole('button', { name: '查看历史版本' })).toBeVisible();
  expect(fetcher).toHaveBeenCalledTimes(2);
});

it.each(['reconnect', 'readOnly'] as const)('rechecks server permissions after %s without retaining owner tools', async change => {
  const fetcher = vi.fn().mockResolvedValueOnce(response(true, true)).mockResolvedValue(response(false, false));
  vi.stubGlobal('fetch', fetcher);
  const { rerender } = render(<MemoryRouter><TacticPublicationTools {...props} /></MemoryRouter>);
  await screen.findByRole('button', { name: '查看历史版本' });
  if (change === 'reconnect') {
    rerender(<MemoryRouter><TacticPublicationTools {...props} connected={false} /></MemoryRouter>);
    expect(screen.queryByRole('button', { name: '查看历史版本' })).not.toBeInTheDocument();
  }
  rerender(<MemoryRouter><TacticPublicationTools {...props} connectionId={change === 'reconnect' ? 'second' : props.connectionId} readOnly={change === 'readOnly'} /></MemoryRouter>);
  await waitFor(() => expect(fetcher).toHaveBeenCalledTimes(2));
  await waitFor(() => expect(screen.queryByText('正在核对发布与历史权限…')).not.toBeInTheDocument());
  expect(screen.queryByRole('button', { name: '打开发布分享管理' })).not.toBeInTheDocument();
  expect(screen.queryByRole('button', { name: '查看历史版本' })).not.toBeInTheDocument();
});

it('preserves an unacknowledged one-time share link while rechecking the same owner after reconnect', async () => {
  const issuedToken = 'A'.repeat(43);
  const fetcher = vi.fn(async (url: string, options?: RequestInit) => ({ ok: true, status: 200, headers: new Headers(), json: async () => {
    if (url.endsWith('/tactics/tactic')) return { tactic: { id: 'tactic', notebook_id: 'book', title: '已保存战术', revision: 1 }, permissions: { manage: true, publish: true } };
    if (url.includes('/shares?')) return { items: [], next_cursor: null };
    if (url.endsWith('/shares') && options?.method === 'POST') return { id: 'share', token: issuedToken, token_available: true };
    throw Error('unexpected request');
  } }));
  vi.stubGlobal('fetch', fetcher);
  const { rerender } = render(<MemoryRouter><TacticPublicationTools {...props} /></MemoryRouter>);
  fireEvent.click(await screen.findByRole('button', { name: '打开发布分享管理' }));
  fireEvent.click(screen.getByRole('button', { name: '准备发布此战术' }));
  fireEvent.click(await screen.findByRole('button', { name: '确认发布这些保存版本' }));
  const link = await screen.findByLabelText('分享链接');
  rerender(<MemoryRouter><TacticPublicationTools {...props} connected={false} /></MemoryRouter>);
  expect(link).not.toBeVisible(); expect(link.isConnected).toBe(true);
  rerender(<MemoryRouter><TacticPublicationTools {...props} connectionId="second" /></MemoryRouter>);
  await waitFor(() => expect(link).toBeVisible());
  expect(screen.getByLabelText('分享链接')).toBe(link);
  expect(link).toHaveValue(`${window.location.origin}/share#token=${issuedToken}`);
  expect(fetcher.mock.calls.filter(([url, options]) => url.endsWith('/shares') && options?.method === 'POST')).toHaveLength(1);
});
