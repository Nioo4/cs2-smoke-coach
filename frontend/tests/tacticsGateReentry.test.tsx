import { act, render, screen, waitFor } from '@testing-library/react';
import { createBrowserRouter, RouterProvider } from 'react-router-dom';
import { afterEach, expect, it, vi } from 'vitest';
import TacticsGate from '../src/tactics/TacticsGate';

const reentry = vi.hoisted(() => ({ armed: false, inserted: false, token: 'A'.repeat(43) }));
vi.mock('../src/components/WorkshopHeader', async importOriginal => {
  const original = await importOriginal<typeof import('../src/components/WorkshopHeader')>();
  const { useLayoutEffect } = await import('react');
  const { useLocation } = await import('react-router-dom');
  return { default: function Header(props: Parameters<typeof original.default>[0]) {
    const route = useLocation();
    useLayoutEffect(() => {
      // A descendant commits before Gate's layout effect. Native navigation queues its own events.
      if (reentry.armed && !reentry.inserted && !route.hash) {
        reentry.inserted = true;
        window.location.hash = `token=${reentry.token}`;
      }
    }, [route]);
    return <original.default {...props} />;
  } };
});

afterEach(() => {
  vi.unstubAllGlobals(); reentry.armed = false; reentry.inserted = false;
  window.history.replaceState(null, '', '/');
});

it.each(['/share', '/join'])('captures a native %s fragment arriving before the prior empty-route layout effect', async path => {
  const response = (payload: unknown) => ({ ok: true, status: 200, headers: new Headers(), json: async () => payload });
  const fetcher = vi.fn(async (url: string) => {
    if (url.endsWith('/features')) return response({ tactics_enabled: true });
    if (url.endsWith('/auth/session')) return response({ user: { id: 'u', username: 'player', display_name: '玩家' }, csrf_token: 'csrf' });
    if (url.endsWith('/notebooks/personal')) return response({ notebook: { id: 'book', kind: 'personal', user_id: 'u', team_id: null } });
    if (url.endsWith('/shares/exchange')) return response({ read_grant: 'grant' });
    if (url.endsWith('/shares/read')) return response({ id: 'share', version_id: 'version', published_at: '2026-10-30', allow_copy: false, items: [] });
    if (url.endsWith('/shares/items')) return response({ version_id: 'version', items: [], next_cursor: null });
    throw Error('Unexpected mutation');
  });
  vi.stubGlobal('fetch', fetcher);
  window.history.replaceState(null, '', `${path}#token=${reentry.token}`);
  const router = createBrowserRouter([{ path: '*', element: <TacticsGate publicShare={path === '/share'} /> }]);
  const { container } = render(<RouterProvider router={router} />);
  const entryEndpoint = path === '/share' ? '/shares/exchange' : '/auth/session';
  const entryCount = () => fetcher.mock.calls.filter(([url]) => url.endsWith(entryEndpoint)).length;
  try {
    if (path === '/share') await screen.findByText('作者未允许复制此发布版本。');
    else await screen.findByRole('button', { name: '查看邀请' });
    expect(entryCount()).toBe(1);
    expect(router.state.location.hash).toBe('');
    reentry.armed = true;
    await act(async () => { window.location.hash = `token=${reentry.token}`; });
    await waitFor(() => expect(reentry.inserted).toBe(true));
    await waitFor(() => expect(entryCount()).toBe(3));
    await waitFor(() => expect(window.location.hash).toBe(''));
    expect(router.state.location.hash).toBe('');
    expect(container.innerHTML).not.toContain(reentry.token);
    expect(fetcher.mock.calls.some(([url]) => url.endsWith('/accept') || url.endsWith('/preview') || url.endsWith('/shares/copy'))).toBe(false);
  } finally { router.dispose(); }
});
