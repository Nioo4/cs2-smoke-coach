import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import StageTeachingReferences, { SavedTeachingReferences } from '../src/tactics/StageTeachingReferences';
import { TacticCollaborationClient } from '../src/tactics/collaborationClient';
import TacticPlaybackPanel from '../src/tactics/TacticPlaybackPanel';
import type { TacticProjection, TacticStage } from '../src/tactics/editorTypes';
import type { LineupDetail } from '../src/types/api';
const oldId = '11111111-1111-4111-8111-111111111111';
const newId = '22222222-2222-4222-8222-222222222222';
const stage: TacticStage = { id: 'stage', version: 1, initial_positions: {}, actions: [], lineup_ids: [oldId] };
const point = { slug: 'a', name_zh: 'A点', map_x: 20, map_y: 20 };
const detail: LineupDetail = { id: newId, target: point, origin: { ...point, name_zh: '出生点' }, is_default: true, source: 'local_curated', steps: { setup: '站在此处', aim: '瞄准此处', throw: '直接投掷', result: '覆盖目标' }, check_info: { last_checked_at: null, game_version_note: null }, primary_video: { url: '/media/real.mp4', poster_url: '/media/poster.jpg', duration_ms: 2000 }, other_lineups: [{ id: 'alternative', is_default: false, variant_hint: '其他路线', primary_video: { url: '/media/other.mp4', poster_url: '', duration_ms: 1000 } }] };
const clients: TacticCollaborationClient[] = [];
function client() { const instance = new TacticCollaborationClient('tactic', 'csrf'); clients.push(instance); return instance; }
function reply(value: unknown, status = 200) { return { ok: status < 400, status, headers: new Headers(), json: async () => value }; }
function catalog() {
  const fetcher = vi.fn(async (url: string) => {
    if (url === '/api/v1/maps/mirage') return reply({ map: { slug: 'mirage' }, targets: [point] });
    if (url === '/api/v1/targets/a/lineups') return reply({ target: point, origins: [{ origin: detail.origin, lineup: { id: newId, variant_hint: '烟雾教学', primary_video: detail.primary_video } }] });
    return reply({ id: oldId, available: false, lineup: null });
  });
  vi.stubGlobal('fetch', fetcher); return fetcher;
}
afterEach(() => { clients.splice(0).forEach(item => item.dispose()); vi.restoreAllMocks(); vi.unstubAllGlobals(); });
it('preserves unavailable IDs, selects from the real public catalog, and submits ordered IDs only through WS command', async () => {
  const fetcher = catalog(); const instance = client(); const command = vi.spyOn(instance, 'command').mockResolvedValue(undefined);
  render(<StageTeachingReferences client={instance} stage={stage} revision={7} editable />);
  expect(await screen.findByText(/原引用仍保留/)).toBeVisible();
  fireEvent.click(screen.getByRole('button', { name: '添加关联教学' }));
  await screen.findByRole('option', { name: 'A点' });
  fireEvent.change(screen.getByLabelText('教学目标'), { target: { value: 'a' } });
  fireEvent.click(await screen.findByRole('checkbox'));
  expect(command).not.toHaveBeenCalled();
  expect(instance.hasUnconfirmedWork()).toBe(true);
  fireEvent.click(screen.getByRole('button', { name: '保存关联教学' }));
  await waitFor(() => expect(command).toHaveBeenCalledWith({ type: 'stage.lineups.set', stage_id: 'stage', lineup_ids: [oldId, newId] }, undefined, 7));
  expect(fetcher.mock.calls.some(([url]) => url === '/api/v1/targets/a/lineups')).toBe(true);
  expect(fetcher.mock.calls.some(([url]) => url.includes('/internal'))).toBe(false);
});
it('keeps draft tied to its stage and starting revision across a remote change', async () => {
  catalog(); const instance = client(); const command = vi.spyOn(instance, 'command').mockResolvedValue(undefined);
  const { rerender } = render(<StageTeachingReferences client={instance} stage={stage} revision={7} editable />);
  fireEvent.click(screen.getByRole('button', { name: '移除教学 1' }));
  rerender(<StageTeachingReferences client={instance} stage={{ ...stage, id: 'other' }} revision={9} editable />);
  expect(screen.getByRole('alert')).toHaveTextContent('仍属于原阶段');
  expect(screen.getByRole('button', { name: '保存关联教学' })).toBeDisabled();
  expect(command).not.toHaveBeenCalled();
  rerender(<StageTeachingReferences client={instance} stage={stage} revision={9} editable />);
  fireEvent.click(screen.getByRole('button', { name: '保存关联教学' }));
  await waitFor(() => expect(command).toHaveBeenCalledWith({ type: 'stage.lineups.set', stage_id: 'stage', lineup_ids: [] }, undefined, 7));
});
it('distinguishes a read failure from an unavailable tutorial and renders only the referenced video without alternatives', async () => {
  let failed = true;
  vi.spyOn(HTMLMediaElement.prototype, 'pause').mockImplementation(() => {});
  vi.spyOn(HTMLMediaElement.prototype, 'load').mockImplementation(() => {});
  const fetcher = vi.fn(async () => failed ? reply({ error: { code: 'ERROR', message: 'database failure' } }, 503) : reply({ id: newId, available: true, lineup: detail }));
  vi.stubGlobal('fetch', fetcher);
  render(<SavedTeachingReferences tacticId="tactic" ids={[newId]} revision={1} />);
  expect(await screen.findByRole('alert')).toHaveTextContent('暂时无法读取');
  expect(screen.queryByText(/原引用仍保留/)).not.toBeInTheDocument();
  failed = false; fireEvent.click(screen.getByRole('button', { name: '重试读取教学' }));
  fireEvent.click(await screen.findByRole('button', { name: '查看关联教学' }));
  expect(screen.getByLabelText('出生点 到 A点 教学视频')).toHaveAttribute('src', '/media/real.mp4');
  expect(screen.queryByRole('button', { name: '一键换路线' })).not.toBeInTheDocument();
  expect(screen.queryByText('其他可用路线（1）')).not.toBeInTheDocument();
});
it('keeps reference IDs frozen after playback starts despite a new confirmed projection', async () => {
  const fetcher = catalog();
  vi.stubGlobal('matchMedia', vi.fn(() => ({ matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn() })));
  vi.stubGlobal('requestAnimationFrame', vi.fn(() => 1)); vi.stubGlobal('cancelAnimationFrame', vi.fn());
  const confirmed: TacticProjection = { structure: { schema_kind: 'stage-board', schema_version: 2, document_id: 'tactic', map: 'mirage', side: 'T', roles: [], stages: [stage] }, fields: { 'stage.stage.name': '阶段1' } };
  const { rerender } = render(<TacticPlaybackPanel confirmed={confirmed} revision={1} epoch="epoch" hasUnconfirmedWork={false} />);
  await screen.findByText(/原引用仍保留/);
  fireEvent.click(screen.getByRole('button', { name: '分阶段演示' }));
  const changed = structuredClone(confirmed); changed.structure.stages[0].lineup_ids = [newId];
  await act(async () => rerender(<TacticPlaybackPanel confirmed={changed} revision={2} epoch="epoch" hasUnconfirmedWork={false} />));
  expect(fetcher.mock.calls.some(([url]) => url.endsWith(`/lineups/${newId}`))).toBe(false);
  expect(screen.getByText(/预览期间收到新修改/)).toBeVisible();
});
it('limits selection to ten without dropping existing unavailable IDs', async () => {
  catalog(); const instance = client();
  const tenIds = Array.from({ length: 10 }, (_, index) => `existing-${index}`);
  render(<StageTeachingReferences client={instance} stage={{ ...stage, lineup_ids: tenIds }} revision={4} editable />);
  fireEvent.click(screen.getByRole('button', { name: '添加关联教学' }));
  await screen.findByRole('option', { name: 'A点' });
  fireEvent.change(screen.getByLabelText('教学目标'), { target: { value: 'a' } });
  expect(await screen.findByRole('checkbox')).toBeDisabled();
  expect(screen.getAllByRole('button', { name: /移除教学/ })).toHaveLength(10);
});
it('explicitly expands published variants from the existing public detail API', async () => {
  const fetcher = catalog();
  fetcher.mockImplementation(async (url: string) => {
    if (url === '/api/v1/maps/mirage') return reply({ targets: [point] });
    if (url === '/api/v1/targets/a/lineups') return reply({ target: point, origins: [{ origin: detail.origin, lineup: { id: newId, variant_hint: '默认烟雾' } }] });
    if (url === `/api/v1/lineups/${newId}`) return reply(detail);
    return reply({ id: oldId, available: false, lineup: null });
  });
  const instance = client(); const command = vi.spyOn(instance, 'command').mockResolvedValue(undefined);
  render(<StageTeachingReferences client={instance} stage={{ ...stage, lineup_ids: [] }} revision={4} editable />);
  fireEvent.click(screen.getByRole('button', { name: '添加关联教学' })); await screen.findByRole('option', { name: 'A点' });
  fireEvent.change(screen.getByLabelText('教学目标'), { target: { value: 'a' } });
  fireEvent.click(await screen.findByRole('button', { name: '展开出生点其他路线' }));
  fireEvent.click(await screen.findByRole('checkbox', { name: /其他路线/ }));
  fireEvent.click(screen.getByRole('button', { name: '保存关联教学' }));
  await waitFor(() => expect(command).toHaveBeenCalledWith({ type: 'stage.lineups.set', stage_id: 'stage', lineup_ids: ['alternative'] }, undefined, 4));
});
it('pauses other expanded referenced videos when one begins playing', async () => {
  vi.spyOn(HTMLMediaElement.prototype, 'load').mockImplementation(() => {});
  const pause = vi.spyOn(HTMLMediaElement.prototype, 'pause').mockImplementation(() => {});
  vi.stubGlobal('fetch', vi.fn(async (url: string) => reply({ id: url.endsWith(newId) ? newId : oldId, available: true, lineup: { ...detail, id: url.endsWith(newId) ? newId : oldId } })));
  const { container } = render(<SavedTeachingReferences tacticId="tactic" ids={[oldId, newId]} revision={1} />);
  const buttons = await screen.findAllByRole('button', { name: '查看关联教学' }); buttons.forEach(button => fireEvent.click(button));
  const videos = container.querySelectorAll('video'); expect(videos).toHaveLength(2); pause.mockClear();
  fireEvent.play(videos[0]);
  expect(pause.mock.contexts).toContain(videos[1]); expect(pause.mock.contexts).not.toContain(videos[0]);
});
it('distinguishes a successfully empty public catalog from loading and failure', async () => {
  vi.stubGlobal('fetch', vi.fn(async () => reply({ targets: [] })));
  render(<StageTeachingReferences client={client()} stage={{ ...stage, lineup_ids: [] }} revision={1} editable />);
  fireEvent.click(screen.getByRole('button', { name: '添加关联教学' }));
  expect(await screen.findByText('当前没有可关联的公开教学。')).toBeVisible();
  expect(screen.queryByRole('alert')).not.toBeInTheDocument();
});
