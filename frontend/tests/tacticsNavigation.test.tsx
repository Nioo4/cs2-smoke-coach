import { act, fireEvent, render, screen } from '@testing-library/react';
import { createMemoryRouter, RouterProvider } from 'react-router-dom';
import { expect, it, vi } from 'vitest';
import TacticNavigationGuard from '../src/tactics/TacticNavigationGuard';
import { actionDraftStart } from '../src/tactics/actionGeometry';
import type { TacticStage } from '../src/tactics/editorTypes';

it('derives after and parallel draft starts from complete preceding groups', () => {
  const stage: TacticStage = { id: 's', version: 1, initial_positions: { r: { x: 10, y: 10, version: 1 } }, lineup_ids: [], actions: [
    { id: 'one', version: 1, group_id: 'first', kind: 'move', role_id: 'r', path: [{ x: 10, y: 10 }, { x: 50, y: 50 }] },
    { id: 'two', version: 1, group_id: 'second', kind: 'move', role_id: 'r', path: [{ x: 50, y: 50 }, { x: 90, y: 90 }] },
  ] };
  expect(actionDraftStart(stage, 'r', 'after')).toEqual({ x: 90, y: 90 });
  expect(actionDraftStart(stage, 'r', 'parallel')).toEqual({ x: 50, y: 50 });
});
it('blocks browser Back with pending edits and allows normal navigation after they are confirmed', async () => {
  let pending = true;
  const router = createMemoryRouter([{ path: '/previous', element: <p>Previous page</p> }, { path: '/editor', element: <TacticNavigationGuard hasUnconfirmedWork={() => pending} exportDraft={() => {}} /> }], { initialEntries: ['/previous', '/editor'], initialIndex: 1 });
  render(<RouterProvider router={router} />);
  await act(async () => { await router.navigate(-1); });
  expect(screen.getByRole('alertdialog')).toHaveTextContent('未确认内容');
  expect(router.state.location.pathname).toBe('/editor');
  fireEvent.click(screen.getByRole('button', { name: '返回继续编辑' }));
  expect(pending).toBe(true);
  pending = false;
  await act(async () => { await router.navigate(-1); });
  expect(await screen.findByText('Previous page')).toBeVisible();
  router.dispose();
});
it('requires explicit export before proceeding with blocked navigation and does not clear pending itself', async () => {
  const pending = { operations: ['unchanged-operation-id'] };
  const exportDraft = vi.fn();
  const router = createMemoryRouter([{ path: '/previous', element: <p>Previous page</p> }, { path: '/editor', element: <TacticNavigationGuard hasUnconfirmedWork={() => pending.operations.length > 0} exportDraft={exportDraft} /> }], { initialEntries: ['/previous', '/editor'], initialIndex: 1 });
  render(<RouterProvider router={router} />);
  await act(async () => { await router.navigate(-1); });
  fireEvent.click(screen.getByRole('button', { name: '导出恢复副本后离开' }));
  expect(await screen.findByText('Previous page')).toBeVisible();
  expect(exportDraft).toHaveBeenCalledOnce(); expect(pending.operations).toEqual(['unchanged-operation-id']);
  router.dispose();
});
