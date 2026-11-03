import { act, fireEvent, render, screen } from '@testing-library/react';
import { createMemoryRouter, RouterProvider } from 'react-router-dom';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import App from '../src/app/App';

beforeEach(() => {
  vi.spyOn(window, 'scrollTo').mockImplementation(() => {});
  vi.stubGlobal('fetch', vi.fn(async () => ({ ok: true, status: 200, headers: new Headers(), json: async () => ({ tactics_enabled: false }) })));
});
afterEach(() => { vi.restoreAllMocks(); vi.unstubAllGlobals(); });

function open(entry: string) {
  const router = createMemoryRouter([{ path: '*', element: <App /> }], { initialEntries: [entry] });
  render(<RouterProvider router={router} />);
  return router;
}

it('starts the destination reading page at the top after leaving a privacy paragraph', async () => {
  const router = open('/privacy#legal-part-8');
  expect(document.getElementById('legal-part-8')).toBeInTheDocument();
  vi.mocked(window.scrollTo).mockClear();
  fireEvent.click(screen.getByRole('link', { name: '非官方声明' }));
  expect(await screen.findByRole('heading', { name: '非官方声明' })).toBeVisible();
  expect(window.scrollTo).toHaveBeenCalledExactlyOnceWith(0, 0);
  router.dispose();
});

it('resets once on entering a reading page, not on query changes or same-page directory navigation', async () => {
  const router = open('/disclaimer');
  expect(window.scrollTo).toHaveBeenCalledExactlyOnceWith(0, 0);
  vi.mocked(window.scrollTo).mockClear();
  await act(async () => { await router.navigate('/disclaimer?reading=1'); });
  expect(window.scrollTo).not.toHaveBeenCalled();
  expect(screen.getByRole('link', { name: '非官方关系' })).toHaveAttribute('href', '#legal-part-4');
  await act(async () => { await router.navigate('/disclaimer?reading=1#legal-part-4'); });
  expect(document.getElementById('legal-part-4')).toBeInTheDocument();
  expect(window.scrollTo).not.toHaveBeenCalled();
  await act(async () => { await router.navigate('/disclaimer?reading=1'); });
  expect(window.scrollTo).not.toHaveBeenCalled();
  router.dispose();
});

it('preserves an incoming reading anchor instead of forcing the page top', () => {
  const router = open('/privacy#legal-part-8');
  expect(window.scrollTo).not.toHaveBeenCalled();
  expect(screen.getByRole('link', { name: '匿名使用记录' })).toHaveAttribute('href', '#legal-part-8');
  router.dispose();
});

it('does not apply reading-page scroll behavior to tactical routes or their location changes', async () => {
  const router = open('/tactics');
  await screen.findByText('战术本暂未开放，请稍后再来。');
  await act(async () => { await router.navigate('/tactics?panel=details'); });
  expect(window.scrollTo).not.toHaveBeenCalled();
  router.dispose();
});
