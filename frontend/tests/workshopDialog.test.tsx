import { fireEvent, render, screen } from '@testing-library/react';
import { expect, it, vi } from 'vitest';
import WorkshopDialog from '../src/tactics/WorkshopDialog';
import ShareManagement from '../src/tactics/ShareManagement';
import TacticHistory from '../src/tactics/TacticHistory';
import { MemoryRouter } from 'react-router-dom';

it('keeps composing input when an Escape cancellation is delivered, then closes normally after composition', () => {
  const close = vi.fn();
  render(<WorkshopDialog title="新建战术" onClose={close}><label>名称<input defaultValue="尚未提交" /></label></WorkshopDialog>);
  const dialog = screen.getByRole('dialog');
  const input = screen.getByLabelText('名称');
  fireEvent.compositionStart(input);
  fireEvent.change(input, { target: { value: '尚未提交的组词' } });
  const escape = new KeyboardEvent('keydown', { key: 'Escape', bubbles: true, cancelable: true, isComposing: true });
  fireEvent(input, escape);
  fireEvent(dialog, new Event('cancel', { cancelable: true }));
  expect(close).not.toHaveBeenCalled();
  expect(escape.defaultPrevented).toBe(true);
  expect(input).toHaveValue('尚未提交的组词');
  fireEvent.compositionEnd(input);
  fireEvent(dialog, new Event('cancel', { cancelable: true }));
  expect(close).toHaveBeenCalledTimes(1);
});

it.each([['share', true], ['history', true], ['share', false], ['history', false]] as const)('returns real DOM focus to the %s trigger after closing, initiallyFocused=%s', async (kind, initiallyFocused) => {
  const fetcher = vi.fn(async () => ({ ok: true, status: 200, headers: new Headers(), json: async () => ({ items: [], next_cursor: null }) }));
  vi.stubGlobal('fetch', fetcher);
  const session = { user: { id: 'u', username: 'player', display_name: '玩家' }, csrf_token: 'csrf' };
  render(<MemoryRouter>{kind === 'share' ? <ShareManagement notebookId="book" session={session} /> : <TacticHistory tacticId="tactic" session={session} />}</MemoryRouter>);
  const trigger = screen.getByRole('button', { name: kind === 'share' ? '打开发布分享管理' : '查看历史版本' });
  if (initiallyFocused) trigger.focus();
  fireEvent.click(trigger);
  const close = await screen.findByRole('button', { name: kind === 'share' ? '关闭发布与固定版本分享' : '关闭历史版本' });
  close.focus(); fireEvent.click(close);
  expect(trigger.isConnected).toBe(true);
  expect(document.activeElement).toBe(trigger);
  vi.unstubAllGlobals();
});
