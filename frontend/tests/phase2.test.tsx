import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { useRef } from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import App from '../src/app/App';
import { svgClientPointToPercent } from '../src/pages/AdminPage';
import { adminApi, ApiError, countCodePoints, encodeBasicUtf8, publicApi } from '../src/api/client';
import { AdminAuthProvider, useAdminAuth } from '../src/app/adminAuth';
import type { AdminLineupOut, PointOut } from '../src/types/api';

const target = { slug: 'vip', name_zh: 'VIP', map_x: 24, map_y: 36 };
const origin = { slug: 't-spawn', name_zh: 'T 出生点', map_x: 12, map_y: 20 };
const poster = '/media/public/lineup-1/poster.webp';
const videoUrl = '/media/public/lineup-1/video.mp4';

const mapPayload = {
  map: { slug: 'mirage' as const, name_zh: '荒漠迷城', asset_url: '/assets/maps/mirage.svg', asset_status: 'placeholder' as const },
  published_lineup_count: 1,
  targets: [{ ...target, origin_count: 1 }],
};

const summary = {
  id: 'lineup-1',
  is_default: true,
  variant_hint: '站位：站在箱子旁；瞄点：瞄准边缘',
  primary_video: { url: videoUrl, poster_url: poster, duration_ms: 2100 },
};

const detail = {
  id: summary.id,
  target,
  origin,
  is_default: true,
  source: 'local_curated' as const,
  steps: { setup: '站在箱子旁', aim: '瞄准边缘', throw: '跳投', result: '烟雾覆盖 VIP' },
  check_info: { last_checked_at: '2026-09-03T10:00:00Z', game_version_note: '当前版本' },
  primary_video: summary.primary_video,
  other_lineups: [{ ...summary, id: 'lineup-2', is_default: false }],
};

const adminTarget = {
  id: 'target-1', ...target, name_en: 'VIP', description: '', sort_order: 1, aliases: ['vip烟'], is_active: true,
  created_at: '2026-09-03T10:00:00Z', updated_at: '2026-09-03T10:00:00Z',
};
const adminOrigin = {
  id: 'origin-1', ...origin, name_en: 'T Spawn', description: '', sort_order: 1, aliases: [], is_active: true,
  created_at: '2026-09-03T10:00:00Z', updated_at: '2026-09-03T10:00:00Z',
};

function jsonResponse(payload: unknown, status = 200, headers: Record<string, string> = {}): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    headers: new Headers({ 'Content-Type': 'application/json', 'X-Request-ID': 'test-request', ...headers }),
    json: async () => payload,
  } as Response;
}

function renderApp(initialEntry = '/') {
  return render(<MemoryRouter initialEntries={[initialEntry]}><App /></MemoryRouter>);
}

function AuthRaceProbe() {
  const { authorization, authGeneration, login, clear } = useAdminAuth();
  const capturedGeneration = useRef<number | null>(null);
  return <>
    <output data-testid="auth-value">{authorization}</output>
    <button type="button" onClick={() => void login('旧用户', '旧密码')}>登录旧凭据</button>
    <button type="button" onClick={() => void login('新用户', '新密码')}>登录新凭据</button>
    <button type="button" onClick={() => { capturedGeneration.current = authGeneration; }}>记录当前会话</button>
    <button type="button" onClick={() => clear(encodeBasicUtf8('旧用户', '旧密码'), capturedGeneration.current ?? -1)}>模拟旧请求401</button>
    <button type="button" onClick={() => clear()}>退出当前会话</button>
  </>;
}

function installFetch(handler: (url: string, init?: RequestInit) => Response | Promise<Response>) {
  const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => handler(String(input), init));
  vi.stubGlobal('fetch', fetchMock);
  return fetchMock;
}

function adminLineup(overrides: Partial<typeof adminDraft> = {}) {
  return {
    ...adminDraft,
    ...overrides,
  };
}

const adminDraft: AdminLineupOut = {
  id: 'lineup-1', status: 'draft' as const, is_default: false, default_needs_review: false, sort_order: 1,
  created_at: '2026-09-03T10:00:00Z', updated_at: '2026-09-03T10:00:00Z',
  target: { id: adminTarget.id, slug: adminTarget.slug, name_zh: adminTarget.name_zh },
  origin: { id: adminOrigin.id, slug: adminOrigin.slug, name_zh: adminOrigin.name_zh },
  steps: { setup: '站位', aim: '瞄点', throw: '投掷', result: '结果' },
  check_info: { last_checked_at: null, game_version_note: null },
  primary_video: null,
};

describe('public phase 2 response branches', () => {
  beforeEach(() => {
    vi.stubGlobal('fetch', vi.fn(async () => jsonResponse(mapPayload)));
    Object.defineProperty(window, 'matchMedia', {
      configurable: true,
      value: (query: string) => ({ matches: query.includes('max-width'), addEventListener: vi.fn(), removeEventListener: vi.fn() }),
    });
  });

  it('opens a direct match, shows notices and releases/reloads native video', async () => {
    installFetch((url) => {
      if (url.endsWith('/maps/mirage')) return jsonResponse(mapPayload);
      if (url.endsWith('/search')) return jsonResponse({
        type: 'match', conversation_id: 'conversation-1', source: 'local_curated', confidence: 1,
        intent: { map: 'mirage', grenade: 'smoke', target_slug: 'vip', origin_slug: 't-spawn' },
        notices: [{ code: 'ORIGIN_UNRESOLVED', message: '起点已按当前目录处理。' }], lineup: detail,
      });
      return jsonResponse(detail);
    });
    renderApp();
    expect(document.querySelectorAll('video')).toHaveLength(0);
    const input = await screen.findByRole('textbox', { name: '搜索路线' });
    fireEvent.change(input, { target: { value: '我在 T 出生点，想封 VIP 烟' } });
    input.focus();
    fireEvent.submit(input.closest('form')!);
    expect(await screen.findByText('起点已按当前目录处理。')).toBeVisible();
    const video = document.querySelector('video[aria-label="T 出生点 到 VIP 教学视频"]') as HTMLVideoElement;
    expect(video).toHaveAttribute('poster', poster);
    expect(video).not.toHaveAttribute('autoplay');
    fireEvent.error(video);
    expect(await screen.findByText('教学视频暂不可用')).toBeVisible();
    fireEvent.click(screen.getByRole('button', { name: '重试' }));
    expect(document.querySelector('video[aria-label="T 出生点 到 VIP 教学视频"]')).toHaveAttribute('src', videoUrl);
    fireEvent.click(screen.getByRole('button', { name: '关闭详情' }));
    await waitFor(() => expect(input).toHaveFocus());
  });

  it('opens a target list, keeps cards and returns from a detail', async () => {
    const choices = { type: 'route_matches' as const, conversation_id: 'conversation-2', source: 'local_curated' as const, intent: { map: 'mirage' as const, grenade: 'smoke' as const, target_slug: 'vip', origin_slug: null }, notices: [], routes: [{ target, origin, lineup: summary }] };
    installFetch((url) => {
      if (url.endsWith('/maps/mirage')) return jsonResponse(mapPayload);
      if (url.includes('/targets/vip/lineups')) return jsonResponse({ target, origins: [{ origin, lineup: summary }] });
      if (url.endsWith('/search')) return jsonResponse(choices);
      return jsonResponse(detail);
    });
    renderApp();
    await screen.findByText('1 条可用路线');
    fireEvent.click(screen.getByRole('button', { name: /VIP，1 个可用起点/ }));
    expect(await screen.findByRole('button', { name: /T 出生点 → VIP/ })).toBeVisible();
    fireEvent.click(screen.getByRole('button', { name: /T 出生点 → VIP/ }));
    expect(await screen.findByRole('button', { name: '← 返回起点列表' })).toBeVisible();
    fireEvent.click(screen.getByRole('button', { name: '← 返回起点列表' }));
    expect(await screen.findByText('每组只展示公开默认路线。视频默认暂停，请主动播放；“查看完整教学”会打开文字步骤与其他路线。')).toBeVisible();
  });

  it('renders route previews paused and pauses sibling previews when one starts', async () => {
    const secondOrigin = { ...origin, slug: 'ct-spawn', name_zh: 'CT 出生点' };
    const choices = {
      type: 'route_matches' as const,
      conversation_id: 'conversation-route-previews',
      source: 'local_curated' as const,
      intent: { map: 'mirage' as const, grenade: 'smoke' as const, target_slug: null, origin_slug: null },
      notices: [],
      routes: [
        { target, origin, lineup: summary },
        { target, origin: secondOrigin, lineup: { ...summary, id: 'lineup-ct', primary_video: { ...summary.primary_video, url: '/media/public/lineup-ct/video.mp4' } } },
      ],
    };
    installFetch((url) => {
      if (url.endsWith('/maps/mirage')) return jsonResponse(mapPayload);
      if (url.endsWith('/search')) return jsonResponse(choices);
      return jsonResponse(detail);
    });
    renderApp();
    const input = await screen.findByRole('textbox', { name: '搜索路线' });
    fireEvent.change(input, { target: { value: '烟怎么丢' } });
    fireEvent.submit(input.closest('form')!);
    await screen.findByText('每组只展示公开默认路线。视频默认暂停，请主动播放；“查看完整教学”会打开文字步骤与其他路线。');

    const videos = Array.from(document.querySelectorAll<HTMLVideoElement>('.route-choice-video'));
    expect(videos).toHaveLength(2);
    for (const video of videos) {
      expect(video).not.toHaveAttribute('autoplay');
      expect(video).toHaveAttribute('preload', 'none');
      expect(video.paused).toBe(true);
    }
    const firstPause = vi.spyOn(videos[0], 'pause').mockImplementation(() => undefined);
    fireEvent.play(videos[1]);
    expect(firstPause).toHaveBeenCalledTimes(1);
  });

  it('keeps a failed route card and retries the same media URL', async () => {
    const choices = {
      type: 'route_matches' as const,
      conversation_id: 'conversation-route-error',
      source: 'local_curated' as const,
      intent: { map: 'mirage' as const, grenade: 'smoke' as const, target_slug: 'vip', origin_slug: null },
      notices: [],
      routes: [{ target, origin, lineup: summary }],
    };
    installFetch((url) => {
      if (url.endsWith('/maps/mirage')) return jsonResponse(mapPayload);
      if (url.endsWith('/search')) return jsonResponse(choices);
      return jsonResponse(detail);
    });
    renderApp();
    const input = await screen.findByRole('textbox', { name: '搜索路线' });
    fireEvent.change(input, { target: { value: 'VIP' } });
    fireEvent.submit(input.closest('form')!);
    await screen.findByText('每组只展示公开默认路线。视频默认暂停，请主动播放；“查看完整教学”会打开文字步骤与其他路线。');

    const first = document.querySelector<HTMLVideoElement>('.route-choice-video');
    expect(first).not.toBeNull();
    expect(first).toHaveAttribute('src', videoUrl);
    fireEvent.error(first!);
    expect(await screen.findByText('教学视频暂不可用')).toBeVisible();
    expect(screen.getByRole('button', { name: /查看完整教学/ })).toBeVisible();

    fireEvent.click(screen.getByRole('button', { name: '重试' }));
    await waitFor(() => {
      const retried = document.querySelector<HTMLVideoElement>('.route-choice-video');
      expect(retried).not.toBe(first);
      expect(retried).toHaveAttribute('src', videoUrl);
    });
    expect(screen.queryByText('教学视频暂不可用')).toBeNull();
  });

  it('keeps same-hint route identities and target-list notices across sibling and fresh searches', async () => {
    const sameHint = '站位：同一说明；瞄点：同一瞄点';
    const unlocatedTarget = { ...target, map_x: null, map_y: null };
    const unlocatedOrigin = { ...origin, map_x: null, map_y: null };
    const firstSummary = {
      ...summary,
      id: 'same-hint-lineup-a',
      variant_hint: sameHint,
      primary_video: { ...summary.primary_video, url: '/media/public/same-hint-a/video.mp4' },
    };
    const secondSummary = {
      ...summary,
      id: 'same-hint-lineup-b',
      is_default: false,
      variant_hint: sameHint,
      primary_video: { ...summary.primary_video, url: '/media/public/same-hint-b/video.mp4' },
    };
    const firstDetail = { ...detail, id: firstSummary.id, target: unlocatedTarget, origin: unlocatedOrigin, primary_video: firstSummary.primary_video, other_lineups: [secondSummary] };
    const secondDetail = { ...detail, id: secondSummary.id, target: unlocatedTarget, origin: unlocatedOrigin, is_default: false, primary_video: secondSummary.primary_video, other_lineups: [firstSummary] };
    const targetChoices = { target: unlocatedTarget, origins: [{ origin: unlocatedOrigin, lineup: firstSummary }] };
    const originNotice = '起点已按当前目录处理。';
    installFetch((url, init) => {
      if (url.endsWith('/maps/mirage')) return jsonResponse({ ...mapPayload, published_lineup_count: 2, targets: [{ ...mapPayload.targets[0], ...unlocatedTarget }] });
      if (url.includes('/targets/vip/lineups')) return jsonResponse(targetChoices);
      if (url.endsWith('/search')) {
        const body = JSON.parse(String(init?.body ?? '{}')) as { text?: string };
        if (body.text?.includes('T 出生点')) {
          return jsonResponse({
            type: 'match', conversation_id: 'fresh-origin', source: 'local_curated', confidence: 1,
            intent: { map: 'mirage', grenade: 'smoke', target_slug: 'vip', origin_slug: 't-spawn' },
            notices: [], lineup: firstDetail,
          });
        }
        return jsonResponse({
          type: 'route_matches', conversation_id: 'target-only', source: 'local_curated',
          intent: { map: 'mirage', grenade: 'smoke', target_slug: 'vip', origin_slug: null },
          notices: [{ code: 'ORIGIN_UNRESOLVED', message: originNotice }],
          routes: [{ target: unlocatedTarget, origin: unlocatedOrigin, lineup: firstSummary }],
        });
      }
      if (url.endsWith(`/lineups/${firstSummary.id}`)) return jsonResponse(firstDetail);
      if (url.endsWith(`/lineups/${secondSummary.id}`)) return jsonResponse(secondDetail);
      return jsonResponse(mapPayload);
    });
    renderApp();
    expect(await screen.findByText('2 条可用路线')).toBeVisible();
    const input = await screen.findByRole('textbox', { name: '搜索路线' });
    fireEvent.change(input, { target: { value: 'VIP' } });
    fireEvent.submit(input.closest('form')!);
    expect(await screen.findByText(originNotice)).toBeVisible();
    const originCard = await screen.findByRole('button', { name: /T 出生点 → VIP/ });
    fireEvent.click(originCard);
    const defaultVideo = await screen.findByLabelText('T 出生点 到 VIP 教学视频');
    expect(defaultVideo).toHaveAttribute('src', firstSummary.primary_video.url);
    expect(defaultVideo).not.toHaveAttribute('autoplay');
    fireEvent.click(screen.getByText('其他可用路线（1）'));
    const siblingButton = screen.getByRole('button', { name: new RegExp(sameHint) });
    expect(siblingButton).toBeVisible();
    fireEvent.click(siblingButton);
    const siblingVideo = await waitFor(() => {
      const currentVideo = screen.getByLabelText('T 出生点 到 VIP 教学视频');
      expect(currentVideo).toHaveAttribute('src', secondSummary.primary_video.url);
      return currentVideo;
    });
    expect(siblingVideo).not.toBe(defaultVideo);
    expect(siblingVideo).not.toHaveAttribute('autoplay');
    expect(defaultVideo).not.toHaveAttribute('src');
    fireEvent.click(screen.getByRole('button', { name: '← 返回起点列表' }));
    expect(siblingVideo).not.toHaveAttribute('src');
    expect(await screen.findByText(originNotice)).toBeVisible();
    expect(screen.getByRole('button', { name: /T 出生点 → VIP/ })).toBeVisible();
    expect(document.querySelectorAll('video')).toHaveLength(1);
    expect(document.querySelector('video.route-choice-video')).toHaveAttribute('preload', 'none');

    fireEvent.change(input, { target: { value: '我在 T 出生点，想封 VIP 烟' } });
    fireEvent.submit(input.closest('form')!);
    expect(await screen.findByLabelText('T 出生点 到 VIP 教学视频')).toHaveAttribute('src', firstSummary.primary_video.url);
    expect(screen.queryByRole('button', { name: '← 返回起点列表' })).toBeNull();
    expect(screen.queryByText(originNotice)).toBeNull();
  });

  it('handles target clarification, selection, not-found and out-of-scope responses', async () => {
    let call = 0;
    installFetch((url) => {
      if (url.endsWith('/maps/mirage')) return jsonResponse(mapPayload);
      if (url.endsWith('/search')) {
        call += 1;
        if (call === 1) return jsonResponse({ type: 'clarification', conversation_id: 'conversation-3', question: '你想封哪个目标位置？', field: 'target', options: [{ value: 'vip', label: 'VIP' }] });
        if (call === 2) return jsonResponse({ type: 'not_found', conversation_id: 'conversation-3', reason: 'NO_TARGET', message: '当前没有匹配路线。' });
        return jsonResponse({ type: 'out_of_scope', conversation_id: 'conversation-4', message: '当前版本只支持 Mirage 的烟雾弹教学。', supported_scope: { maps: ['mirage'], grenades: ['smoke'] } });
      }
      return jsonResponse(mapPayload);
    });
    renderApp();
    const input = await screen.findByRole('textbox', { name: '搜索路线' });
    fireEvent.change(input, { target: { value: 'VIP' } });
    fireEvent.submit(input.closest('form')!);
    expect(await screen.findByRole('heading', { name: '选择目标位置' })).toBeVisible();
    fireEvent.click(screen.getByRole('button', { name: 'VIP' }));
    expect(await screen.findByText('当前没有匹配路线。')).toBeVisible();
    fireEvent.click(screen.getByRole('button', { name: '修改输入' }));
    await waitFor(() => expect(input).toHaveFocus());
    input.focus();
    fireEvent.submit(input.closest('form')!);
    expect(await screen.findByText('当前版本只支持 Mirage 的烟雾弹教学。')).toBeVisible();
  });

  it('keeps map errors separate from an empty catalog and validates Unicode input', async () => {
    const fetchMock = installFetch((url) => url.endsWith('/maps/mirage') ? jsonResponse({ error: { code: 'UPSTREAM_UNAVAILABLE', message: '目录暂时不可用。' } }, 503) : jsonResponse(mapPayload));
    renderApp();
    expect(await screen.findByText('路线目录暂时不可用')).toBeVisible();
    expect(screen.getByText(/当前状态不代表没有路线/)).toBeVisible();
    fireEvent.click(screen.getByRole('button', { name: '重新读取' }));
    expect(fetchMock).toHaveBeenCalledTimes(2);
    const input = screen.getByRole('textbox', { name: '搜索路线' });
    fireEvent.change(input, { target: { value: '😀'.repeat(201) } });
    fireEvent.submit(input.closest('form')!);
    expect(await screen.findByText('请输入 1–200 个字符。')).toBeVisible();
  });

  it('navigates an approved map through Target markers without Origin geometry', async () => {
    const secondTarget = { slug: 'window', name_zh: '窗口', map_x: 72, map_y: 18, origin_count: 1 };
    const approvedMap = {
      ...mapPayload,
      map: { ...mapPayload.map, asset_status: 'approved' as const },
      published_lineup_count: 2,
      targets: [{ ...mapPayload.targets[0], origin_count: 2 }, secondTarget],
    };
    const secondOrigin = { ...origin, slug: 'ct-spawn', name_zh: 'CT 出生点', map_x: 72, map_y: 16 };
    const secondLineup = { ...summary, id: 'lineup-3', is_default: false, variant_hint: '站位：CT 侧；瞄点：箱角' };
    const mapDetail = { ...detail, target, origin, other_lineups: [secondLineup] };
    installFetch((url) => {
      if (url.endsWith('/maps/mirage')) return jsonResponse(approvedMap);
      if (url.includes('/targets/vip/lineups')) return jsonResponse({ target, origins: [{ origin, lineup: summary }, { origin: secondOrigin, lineup: secondLineup }] });
      if (url.endsWith('/lineups/lineup-1')) return jsonResponse(mapDetail);
      return jsonResponse(mapDetail);
    });
    renderApp();
    expect(await screen.findByRole('heading', { name: '在地图上选择。' })).toBeVisible();
    expect(document.querySelectorAll('.map-target-button')).toHaveLength(2);
    const targetMarker = document.querySelector<SVGGElement>('.map-target-button');
    expect(targetMarker).not.toBeNull();
    expect(targetMarker?.querySelector('.map-hit-area')).toHaveAttribute('r', '84');
    fireEvent.click(targetMarker!);
    const routeButton = await waitFor(() => {
      const elements = screen.getAllByRole('button', { name: 'T 出生点 → VIP：查看完整教学' });
      expect(elements[0]).toBeVisible();
      return elements[0];
    });
    expect(routeButton).toBeVisible();
    expect(screen.getByRole('button', { name: /CT 出生点 → VIP：查看完整教学/ })).toBeVisible();
    expect(document.querySelector('.map-origin-button')).not.toBeInTheDocument();
    expect(document.querySelector('.map-route-line')).not.toBeInTheDocument();
    fireEvent.click(routeButton);
    expect(await screen.findByLabelText('T 出生点 到 VIP 教学视频')).toBeVisible();
    expect((screen.getByLabelText('T 出生点 到 VIP 教学视频') as HTMLVideoElement).paused).toBe(true);
  });

  it('keeps only the Target selected for a direct text detail', async () => {
    const approvedMap = { ...mapPayload, map: { ...mapPayload.map, asset_status: 'approved' as const } };
    installFetch((url) => {
      if (url.endsWith('/maps/mirage')) return jsonResponse(approvedMap);
      if (url.endsWith('/search')) return jsonResponse({
        type: 'match', conversation_id: 'direct-map', source: 'local_curated', confidence: 1,
        intent: { map: 'mirage', grenade: 'smoke', target_slug: 'vip', origin_slug: 't-spawn' },
        notices: [], lineup: detail,
      });
      return jsonResponse(detail);
    });
    renderApp();
    const input = await screen.findByRole('textbox', { name: '搜索路线' });
    fireEvent.change(input, { target: { value: 'T 出生点 VIP' } });
    fireEvent.submit(input.closest('form')!);
    await screen.findByLabelText('T 出生点 到 VIP 教学视频');
    expect(document.querySelector('.map-target-button.is-selected')).toHaveAttribute('aria-current', 'true');
    expect(document.querySelector('.map-origin-button')).not.toBeInTheDocument();
    expect(document.querySelector('.map-route-line')).not.toBeInTheDocument();
  });

  it('shares target-only text results with the approved map Target marker', async () => {
    const approvedMap = { ...mapPayload, map: { ...mapPayload.map, asset_status: 'approved' as const } };
    const secondOrigin = { ...origin, slug: 'ct-spawn', name_zh: 'CT 出生点', map_x: 72, map_y: 16 };
    const choices = {
      type: 'route_matches' as const,
      conversation_id: 'target-only-map',
      source: 'local_curated' as const,
      intent: { map: 'mirage' as const, grenade: 'smoke' as const, target_slug: 'vip', origin_slug: null },
      notices: [],
      routes: [
        { target, origin, lineup: summary },
        { target, origin: secondOrigin, lineup: { ...summary, id: 'lineup-3' } },
      ],
    };
    installFetch((url) => {
      if (url.endsWith('/maps/mirage')) return jsonResponse(approvedMap);
      if (url.endsWith('/search')) return jsonResponse(choices);
      return jsonResponse(detail);
    });
    renderApp();
    const input = await screen.findByRole('textbox', { name: '搜索路线' });
    fireEvent.change(input, { target: { value: 'VIP' } });
    fireEvent.submit(input.closest('form')!);
    await screen.findByRole('dialog', { name: 'VIP · 可用起点' });
    await waitFor(() => expect(document.querySelectorAll('.map-target-button')).toHaveLength(1));
    expect(document.querySelectorAll('.map-origin-button')).toHaveLength(0);
    expect(screen.getByRole('group', { name: 'Mirage 地图与 Target 标记' })).toBeVisible();
  });

  it('does not pass malformed Target coordinates into the SVG marker layer', async () => {
    const malformedTarget = {
      ...mapPayload.targets[0],
      slug: 'malformed',
      name_zh: '异常点',
      map_x: 'not-a-number' as unknown as number,
    };
    const approvedMap = {
      ...mapPayload,
      map: { ...mapPayload.map, asset_status: 'approved' as const },
      targets: [mapPayload.targets[0], malformedTarget],
    };
    installFetch((url) => url.endsWith('/maps/mirage') ? jsonResponse(approvedMap) : jsonResponse(detail));
    renderApp();
    await screen.findByRole('heading', { name: '在地图上选择。' });
    expect(document.querySelectorAll('.map-target-button')).toHaveLength(1);
    expect(document.querySelector('.map-target-button')?.textContent).toContain('VIP');
  });

  it('keeps target and detail failures in the visible route context', async () => {
    installFetch((url) => {
      if (url.endsWith('/maps/mirage')) return jsonResponse(mapPayload);
      if (url.includes('/targets/vip/lineups')) return jsonResponse({ error: { code: 'UPSTREAM_UNAVAILABLE', message: '起点目录暂时不可用。' } }, 503);
      return jsonResponse(mapPayload);
    });
    renderApp();
    await screen.findByText('1 条可用路线');
    fireEvent.click(screen.getByRole('button', { name: /VIP，1 个可用起点/ }));
    expect(await screen.findByText('起点目录暂时不可用。')).toBeVisible();
  });

  it('shows an API failure for search and a failure while selecting clarification', async () => {
    let selection = false;
    installFetch((url) => {
      if (url.endsWith('/maps/mirage')) return jsonResponse(mapPayload);
      if (url.endsWith('/search')) {
        if (selection) return jsonResponse({ error: { code: 'UPSTREAM_UNAVAILABLE', message: '检索服务暂时不可用。' } }, 503);
        return jsonResponse({ type: 'clarification', conversation_id: 'conversation-5', question: '你想封哪个目标位置？', field: 'target', options: [{ value: 'vip', label: 'VIP' }] });
      }
      return jsonResponse(mapPayload);
    });
    renderApp();
    const input = await screen.findByRole('textbox', { name: '搜索路线' });
    fireEvent.change(input, { target: { value: 'VIP' } });
    fireEvent.submit(input.closest('form')!);
    await screen.findByRole('heading', { name: '选择目标位置' });
    selection = true;
    const clarificationButton = screen.getByRole('button', { name: 'VIP' });
    await waitFor(() => expect(clarificationButton).toBeEnabled());
    fireEvent.click(clarificationButton);
    expect((await screen.findAllByText('检索服务暂时不可用。')).length).toBeGreaterThan(0);
    fireEvent.click(screen.getByRole('button', { name: '关闭详情' }));
    fireEvent.submit(input.closest('form')!);
    expect((await screen.findAllByText('检索服务暂时不可用。')).length).toBeGreaterThan(0);
  });

  it('releases the current video when a later search starts, even if that search fails', async () => {
    Object.defineProperty(window, 'matchMedia', {
      configurable: true,
      value: () => ({ matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn() }),
    });
    let searchCount = 0;
    installFetch((url) => {
      if (url.endsWith('/maps/mirage')) return jsonResponse(mapPayload);
      if (url.endsWith('/search')) {
        searchCount += 1;
        if (searchCount === 1) return jsonResponse({
          type: 'match', conversation_id: 'conversation-6', source: 'local_curated', confidence: 1,
          intent: { map: 'mirage', grenade: 'smoke', target_slug: 'vip', origin_slug: 't-spawn' },
          notices: [], lineup: detail,
        });
        return jsonResponse({ error: { code: 'UPSTREAM_UNAVAILABLE', message: '新的检索暂时不可用。' } }, 503);
      }
      return jsonResponse(mapPayload);
    });
    renderApp();
    const input = await screen.findByRole('textbox', { name: '搜索路线' });
    fireEvent.change(input, { target: { value: 'VIP' } });
    fireEvent.submit(input.closest('form')!);
    const video = await screen.findByLabelText('T 出生点 到 VIP 教学视频') as HTMLVideoElement;
    const pause = vi.spyOn(video, 'pause').mockImplementation(() => undefined);
    const load = vi.spyOn(video, 'load').mockImplementation(() => undefined);
    fireEvent.change(input, { target: { value: '新的查询' } });
    fireEvent.submit(input.closest('form')!);
    expect(await screen.findByText('新的检索暂时不可用。')).toBeVisible();
    expect(pause).toHaveBeenCalled();
    expect(load).toHaveBeenCalled();
    expect(video).not.toHaveAttribute('src');
  });

  it('recreates the same direct detail after a later successful search', async () => {
    installFetch((url) => {
      if (url.endsWith('/maps/mirage')) return jsonResponse(mapPayload);
      if (url.endsWith('/search')) return jsonResponse({
        type: 'match', conversation_id: 'conversation-repeat', source: 'local_curated', confidence: 1,
        intent: { map: 'mirage', grenade: 'smoke', target_slug: 'vip', origin_slug: 't-spawn' },
        notices: [], lineup: detail,
      });
      return jsonResponse(mapPayload);
    });
    renderApp();
    const input = await screen.findByRole('textbox', { name: '搜索路线' });
    fireEvent.change(input, { target: { value: 'T 出生点封 VIP' } });
    fireEvent.submit(input.closest('form')!);
    const firstVideo = await screen.findByLabelText('T 出生点 到 VIP 教学视频') as HTMLVideoElement;
    const pause = vi.spyOn(firstVideo, 'pause').mockImplementation(() => undefined);
    const load = vi.spyOn(firstVideo, 'load').mockImplementation(() => undefined);

    fireEvent.submit(input.closest('form')!);

    await waitFor(() => {
      const currentVideo = screen.getByLabelText('T 出生点 到 VIP 教学视频');
      expect(currentVideo).not.toBe(firstVideo);
      expect(currentVideo).toHaveAttribute('src', videoUrl);
    });
    expect(pause).toHaveBeenCalled();
    expect(load).toHaveBeenCalled();
    expect(firstVideo).not.toHaveAttribute('src');
  });

  it('uses the desktop region and keeps detail lookup failures beside the list', async () => {
    Object.defineProperty(window, 'matchMedia', {
      configurable: true,
      value: () => ({ matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn() }),
    });
    installFetch((url) => {
      if (url.endsWith('/maps/mirage')) return jsonResponse(mapPayload);
      if (url.includes('/targets/vip/lineups')) return jsonResponse({ target, origins: [{ origin, lineup: summary }] });
      return jsonResponse({ error: { code: 'NOT_FOUND', message: '路线不存在。' } }, 404);
    });
    renderApp();
    await screen.findByText('1 条可用路线');
    fireEvent.click(screen.getByRole('button', { name: /VIP，1 个可用起点/ }));
    expect(await screen.findByRole('region', { name: 'VIP · 可用起点' })).toBeVisible();
    const originButton = screen.getByRole('button', { name: /T 出生点 → VIP/ });
    await waitFor(() => expect(originButton).toBeEnabled());
    fireEvent.click(originButton);
    expect((await screen.findAllByText('路线不存在。')).length).toBeGreaterThan(0);
  });
});

describe('admin API transport and memory auth', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('encodes UTF-8 Basic credentials and maps safe API errors', async () => {
    expect(countCodePoints('😀')).toBe(1);
    const credentialBytes = new TextEncoder().encode('用户:密');
    expect(encodeBasicUtf8('用户', '密')).toBe(`Basic ${btoa(String.fromCharCode(...credentialBytes))}`);
    installFetch(() => jsonResponse({ error: { code: 'VALIDATION_ERROR', message: '输入不合法。', details: { fields: [{ path: 'name_zh', reason: 'string_too_short' }] } } }, 422));
    await expect(publicApi.map()).rejects.toMatchObject({ status: 422, code: 'VALIDATION_ERROR' });
    installFetch(() => ({ ok: true, status: 200, headers: new Headers(), json: async () => 'not-an-object' } as unknown as Response));
    await expect(publicApi.map()).rejects.toMatchObject({ code: 'INVALID_RESPONSE' });
    installFetch(() => ({ ok: true, status: 200, headers: new Headers(), json: async () => { throw new Error('bad body'); } } as unknown as Response));
    await expect(publicApi.map()).rejects.toMatchObject({ code: 'INVALID_RESPONSE' });
  });

  it('sends JSON and multipart admin requests with the UTF-8 auth header', async () => {
    const fetchMock = installFetch((url) => {
      if (url.endsWith('/admin/lineups/lineup-1/video')) return jsonResponse(adminDraft);
      return jsonResponse(adminTarget);
    });
    await adminApi.createTarget('Basic dGVzdA==', { slug: 'vip', name_zh: 'VIP', description: '', sort_order: 0, aliases: [] });
    await adminApi.uploadVideo('Basic dGVzdA==', 'lineup-1', new File(['video'], 'demo.mp4', { type: 'video/mp4' }), '自录视频');
    const jsonInit = fetchMock.mock.calls[0][1] as RequestInit;
    const multipartInit = fetchMock.mock.calls[1][1] as RequestInit;
    expect(new Headers(jsonInit.headers).get('Authorization')).toBe('Basic dGVzdA==');
    expect(new Headers(jsonInit.headers).get('Content-Type')).toBe('application/json');
    expect(multipartInit.body).toBeInstanceOf(FormData);
    expect(new Headers(multipartInit.headers).get('Content-Type')).toBeNull();
  });

  it('keeps every admin lifecycle request on its explicit API route', async () => {
    const fetchMock = installFetch(() => jsonResponse(adminTarget));
    const authorization = 'Basic dGVzdA==';
    await adminApi.patchTarget(authorization, 'target-1', { name_zh: 'VIP 更新' });
    await adminApi.patchOrigin(authorization, 'origin-1', { name_zh: 'T 出生点更新' });
    await adminApi.archiveTarget(authorization, 'target-1');
    await adminApi.restoreTarget(authorization, 'target-1');
    await adminApi.archiveOrigin(authorization, 'origin-1');
    await adminApi.restoreOrigin(authorization, 'origin-1');
    await adminApi.map(authorization);
    await adminApi.metrics(authorization);
    await adminApi.setDefault(authorization, 'lineup-1');
    await adminApi.unpublish(authorization, 'lineup-1');
    await adminApi.markNeeds(authorization, 'lineup-1');
    await adminApi.archiveLineup(authorization, 'lineup-1');
    await adminApi.restoreLineup(authorization, 'lineup-1');
    await adminApi.archiveVideo(authorization, 'lineup-1');
    await adminApi.restoreVideo(authorization, 'lineup-1');

    expect(fetchMock.mock.calls.map(([url, init]) => [String(url), init?.method ?? 'GET'])).toEqual([
      ['/api/v1/admin/targets/target-1', 'PATCH'],
      ['/api/v1/admin/origins/origin-1', 'PATCH'],
      ['/api/v1/admin/targets/target-1/archive', 'POST'],
      ['/api/v1/admin/targets/target-1/restore', 'POST'],
      ['/api/v1/admin/origins/origin-1/archive', 'POST'],
      ['/api/v1/admin/origins/origin-1/restore', 'POST'],
      ['/api/v1/admin/maps/mirage', 'GET'],
      ['/api/v1/admin/metrics/summary', 'GET'],
      ['/api/v1/admin/lineups/lineup-1/set-default', 'POST'],
      ['/api/v1/admin/lineups/lineup-1/unpublish', 'POST'],
      ['/api/v1/admin/lineups/lineup-1/mark-needs-revalidation', 'POST'],
      ['/api/v1/admin/lineups/lineup-1/archive', 'POST'],
      ['/api/v1/admin/lineups/lineup-1/restore', 'POST'],
      ['/api/v1/admin/lineups/lineup-1/video/archive', 'POST'],
      ['/api/v1/admin/lineups/lineup-1/video/restore', 'POST'],
    ]);
  });

  it('sends the dedicated Target position request without a generic point patch', async () => {
    const fetchMock = installFetch(() => jsonResponse(adminTarget));
    await adminApi.positionTarget('Basic dGVzdA==', 'target-1', { map_x: 12.34, map_y: 56.78 });
    expect(String(fetchMock.mock.calls[0]?.[0])).toBe('/api/v1/admin/targets/target-1/position');
    expect(fetchMock.mock.calls[0]?.[1]?.method).toBe('PATCH');
    expect(JSON.parse(String(fetchMock.mock.calls[0]?.[1]?.body))).toEqual({ map_x: 12.34, map_y: 56.78 });
  });

  it('converts client coordinates through the SVG CTM and preserves the 1000 viewBox', () => {
    class TestDOMPoint {
      constructor(readonly x: number, readonly y: number) {}
      matrixTransform(matrix: { scaleX: number; scaleY: number; offsetX: number; offsetY: number }) {
        return { x: this.x * matrix.scaleX + matrix.offsetX, y: this.y * matrix.scaleY + matrix.offsetY };
      }
    }
    vi.stubGlobal('DOMPoint', TestDOMPoint);
    const svg = { getScreenCTM: () => ({ inverse: () => ({ scaleX: 2, scaleY: 2, offsetX: -20, offsetY: -40 }) }) } as unknown as SVGSVGElement;
    expect(svgClientPointToPercent(svg, 220, 440)).toEqual({ map_x: 42, map_y: 84 });
    expect(svgClientPointToPercent(svg, -100, -100)).toBeNull();
  });

  it('does not let a stale 401 clear a newer in-memory login', async () => {
    installFetch(() => jsonResponse({ authenticated: true }));
    render(<AdminAuthProvider><AuthRaceProbe /></AdminAuthProvider>);
    fireEvent.click(screen.getByRole('button', { name: '登录旧凭据' }));
    const oldAuthorization = encodeBasicUtf8('旧用户', '旧密码');
    await waitFor(() => expect(screen.getByTestId('auth-value')).toHaveTextContent(oldAuthorization));
    fireEvent.click(screen.getByRole('button', { name: '登录新凭据' }));
    const newAuthorization = encodeBasicUtf8('新用户', '新密码');
    await waitFor(() => expect(screen.getByTestId('auth-value')).toHaveTextContent(newAuthorization));
    fireEvent.click(screen.getByRole('button', { name: '模拟旧请求401' }));
    expect(screen.getByTestId('auth-value')).toHaveTextContent(newAuthorization);
  });

  it('does not let an old 401 clear a re-login with the same credentials', async () => {
    installFetch(() => jsonResponse({ authenticated: true }));
    render(<AdminAuthProvider><AuthRaceProbe /></AdminAuthProvider>);
    fireEvent.click(screen.getByRole('button', { name: '登录旧凭据' }));
    const authorization = encodeBasicUtf8('旧用户', '旧密码');
    await waitFor(() => expect(screen.getByTestId('auth-value')).toHaveTextContent(authorization));
    fireEvent.click(screen.getByRole('button', { name: '记录当前会话' }));
    fireEvent.click(screen.getByRole('button', { name: '退出当前会话' }));
    await waitFor(() => expect(screen.getByTestId('auth-value')).toHaveTextContent(''));
    fireEvent.click(screen.getByRole('button', { name: '登录旧凭据' }));
    await waitFor(() => expect(screen.getByTestId('auth-value')).toHaveTextContent(authorization));
    fireEvent.click(screen.getByRole('button', { name: '模拟旧请求401' }));
    expect(screen.getByTestId('auth-value')).toHaveTextContent(authorization);
  });
});

describe('admin workspace', () => {
  let currentLineup = adminDraft;
  beforeEach(() => {
    currentLineup = adminDraft;
  });

  function installAdminBackend() {
    let targetDirectory: PointOut[] = [adminTarget];
    let originDirectory: PointOut[] = [adminOrigin];
    return installFetch((url, init) => {
      const methodName = init?.method ?? 'GET';
      if (url.endsWith('/admin/session')) return jsonResponse({ authenticated: true });
      if (url.endsWith('/admin/maps/mirage')) return jsonResponse(mapPayload.map);
      if (url.endsWith('/admin/targets') && methodName === 'POST') {
        const payload = JSON.parse(String(init?.body ?? '{}')) as Partial<PointOut>;
        const created = { ...adminTarget, ...payload, id: 'target-created-null' } as PointOut;
        targetDirectory = [...targetDirectory, created];
        return jsonResponse(created, 201);
      }
      if (url.endsWith('/admin/origins') && methodName === 'POST') {
        const payload = JSON.parse(String(init?.body ?? '{}')) as Partial<PointOut>;
        const created = { ...adminOrigin, ...payload, id: 'origin-created' } as PointOut;
        originDirectory = [...originDirectory, created];
        return jsonResponse(created, 201);
      }
      if (url.endsWith('/admin/lineups') && methodName === 'POST') return jsonResponse(currentLineup, 201);
      if (url.includes('/video') && methodName === 'POST') {
        currentLineup = adminLineup({ primary_video: { id: 'video-1', status: 'ready', duration_ms: 2100, size_bytes: 100, rights_note: '自录视频', failure_reason: null, url: videoUrl, poster_url: poster } });
        return jsonResponse(currentLineup);
      }
      if (url.includes('/publish') && methodName === 'POST') {
        currentLineup = adminLineup({ status: 'published', is_default: true, primary_video: { id: 'video-1', status: 'ready', duration_ms: 2100, size_bytes: 100, rights_note: '自录视频', failure_reason: null, url: videoUrl, poster_url: poster } });
        return jsonResponse(currentLineup);
      }
      if (url.match(/\/admin\/lineups\/lineup-1$/) && methodName === 'PATCH') return jsonResponse(currentLineup);
      if (url.endsWith('/admin/targets')) return jsonResponse(targetDirectory);
      if (url.endsWith('/admin/origins')) return jsonResponse(originDirectory);
      if (url.includes('/admin/lineups?status=all')) return jsonResponse([currentLineup]);
      return jsonResponse({});
    });
  }

  async function login() {
    fireEvent.change(screen.getByLabelText('用户名'), { target: { value: 'admin' } });
    fireEvent.change(screen.getByLabelText('密码'), { target: { value: 'test-password' } });
    fireEvent.click(screen.getByRole('button', { name: '登录' }));
    await screen.findByRole('heading', { name: '把一条路线教清楚。' });
    await screen.findByRole('tablist', { name: '管理标签' });
  }

  it('does not burst authenticated workspace requests beyond one at a time', async () => {
    let releaseRequests = (): void => undefined;
    const requestGate = new Promise<void>((resolve) => { releaseRequests = resolve; });
    let inFlight = 0;
    let maxInFlight = 0;
    const fetchMock = installFetch(async (url) => {
      if (url.endsWith('/admin/session')) return jsonResponse({ authenticated: true });
      inFlight += 1;
      maxInFlight = Math.max(maxInFlight, inFlight);
      await requestGate;
      inFlight -= 1;
      if (url.endsWith('/admin/maps/mirage')) return jsonResponse(mapPayload.map);
      if (url.endsWith('/admin/targets')) return jsonResponse([adminTarget]);
      if (url.endsWith('/admin/origins')) return jsonResponse([adminOrigin]);
      if (url.includes('/admin/lineups?status=all')) return jsonResponse([adminDraft]);
      return jsonResponse({});
    });

    renderApp('/admin');
    await login();
    await waitFor(() => expect(inFlight).toBeGreaterThan(0));
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(maxInFlight).toBeLessThanOrEqual(1);
    releaseRequests();
    await waitFor(() => expect(fetchMock.mock.calls.filter(([url]) => String(url) !== '/api/v1/admin/session')).toHaveLength(4));
    await waitFor(() => expect(inFlight).toBe(0));
  });

  it('refreshes only the admin collection changed by a successful action', async () => {
    const published = adminLineup({ id: 'lineup-published', status: 'published', is_default: true });
    const fetchMock = installFetch((url, init) => {
      if (url.endsWith('/admin/session')) return jsonResponse({ authenticated: true });
      if (url.endsWith('/admin/maps/mirage')) return jsonResponse(mapPayload.map);
      if (url.endsWith('/admin/lineups/lineup-published/set-default') && init?.method === 'POST') return jsonResponse(published);
      if (url.endsWith('/admin/targets/target-1/archive') && init?.method === 'POST') return jsonResponse({ ...adminTarget, is_active: false });
      if (url.endsWith('/admin/targets')) return jsonResponse([adminTarget]);
      if (url.endsWith('/admin/origins')) return jsonResponse([adminOrigin]);
      if (url.includes('/admin/lineups?status=all')) return jsonResponse([published]);
      return jsonResponse({});
    });
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(true);

    renderApp('/admin');
    await login();
    const setDefault = await screen.findByRole('button', { name: '设为默认' });
    await waitFor(() => expect(fetchMock.mock.calls.filter(([url]) => String(url).includes('/admin/lineups?status=all'))).toHaveLength(1));

    const routeStart = fetchMock.mock.calls.length;
    fireEvent.click(setDefault);
    await waitFor(() => expect(fetchMock.mock.calls.slice(routeStart).filter(([url]) => String(url).includes('/admin/lineups?status=all'))).toHaveLength(1));
    const routeCalls = fetchMock.mock.calls.slice(routeStart);
    expect(routeCalls.filter(([url]) => String(url).endsWith('/admin/targets'))).toHaveLength(0);
    expect(routeCalls.filter(([url]) => String(url).endsWith('/admin/origins'))).toHaveLength(0);

    fireEvent.click(screen.getByRole('tab', { name: '点位' }));
    const targetSection = screen.getByRole('heading', { name: '现有 Target' }).parentElement;
    expect(targetSection).not.toBeNull();
    const pointStart = fetchMock.mock.calls.length;
    fireEvent.click(within(targetSection as HTMLElement).getByRole('button', { name: '归档' }));
    await waitFor(() => expect(fetchMock.mock.calls.slice(pointStart).filter(([url]) => String(url).endsWith('/admin/targets'))).toHaveLength(1));
    const pointCalls = fetchMock.mock.calls.slice(pointStart);
    expect(pointCalls.filter(([url]) => String(url).endsWith('/admin/origins'))).toHaveLength(0);
    expect(pointCalls.filter(([url]) => String(url).includes('/admin/lineups?status=all'))).toHaveLength(0);
    expect(confirmSpy).toHaveBeenCalledTimes(1);
    confirmSpy.mockRestore();
  });

  it('renders only the selected management tab panel', async () => {
    installAdminBackend();
    renderApp('/admin');
    await login();
    expect(screen.getByRole('tablist', { name: '管理标签' })).toBeVisible();
    for (const tab of screen.getAllByRole('tab')) expect(tab).not.toHaveAttribute('tabindex', '-1');
    expect(screen.getByRole('tabpanel', { name: '路线' })).toBeVisible();
    expect(screen.queryByRole('tabpanel', { name: '点位' })).toBeNull();
    expect(screen.queryByRole('tabpanel', { name: '指标' })).toBeNull();
    fireEvent.click(screen.getByRole('tab', { name: '点位' }));
    expect(screen.getByRole('tabpanel', { name: '点位' })).toBeVisible();
    expect(document.querySelectorAll('#target-slug')).toHaveLength(1);
    expect(document.querySelectorAll('#origin-slug')).toHaveLength(1);
    expect(screen.queryByRole('tabpanel', { name: '路线' })).toBeNull();
    fireEvent.click(screen.getByRole('tab', { name: '指标' }));
    expect(screen.getByRole('tabpanel', { name: '指标' })).toBeVisible();
    expect(screen.queryByRole('tabpanel', { name: '点位' })).toBeNull();
  });

  it('exposes the published, draft and archived lifecycle actions', async () => {
    const readyVideo = { id: 'video-ready', status: 'ready', duration_ms: 2100, size_bytes: 100, rights_note: '自录视频', failure_reason: null, url: videoUrl, poster_url: poster };
    const archivedVideo = { ...readyVideo, id: 'video-archived', status: 'archived' };
    const published = adminLineup({ id: 'lineup-published', status: 'published', is_default: true, default_needs_review: true, primary_video: readyVideo });
    const draftReady = adminLineup({ id: 'lineup-draft', status: 'draft', primary_video: readyVideo });
    const needsReview = adminLineup({ id: 'lineup-needs', status: 'needs_revalidation', primary_video: archivedVideo });
    const archived = adminLineup({ id: 'lineup-archived', status: 'archived', primary_video: archivedVideo });
    const lineups = [published, draftReady, needsReview, archived];
    const fetchMock = installFetch((url, init) => {
      if (url.endsWith('/admin/maps/mirage')) return jsonResponse(mapPayload.map);
      if (url.endsWith('/maps/mirage')) return jsonResponse(mapPayload);
      if (url.endsWith('/admin/session')) return jsonResponse({ authenticated: true });
      if (url.endsWith('/admin/targets')) return jsonResponse([adminTarget]);
      if (url.endsWith('/admin/origins')) return jsonResponse([adminOrigin]);
      if (url.includes('/admin/lineups?status=all')) return jsonResponse(lineups);
      return jsonResponse(init?.method === 'POST' ? published : {});
    });
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(true);
    renderApp('/admin');
    await login();

    const clickAction = async (selector: string, label: string, path: string) => {
      const card = document.querySelector(selector);
      expect(card).not.toBeNull();
      const before = fetchMock.mock.calls.length;
      fireEvent.click(within(card as HTMLElement).getByRole('button', { name: label }));
      await waitFor(() => expect(fetchMock.mock.calls.slice(before).some(([url, init]) => String(url).endsWith(path) && init?.method === 'POST')).toBe(true));
      await waitFor(() => expect(within(card as HTMLElement).getByRole('button', { name: label })).toBeEnabled());
    };

    expect(document.querySelector('.lineup-published')?.textContent).toContain('ID lineup-published · 优先级 1');
    expect(document.querySelector('.lineup-draft')?.textContent).toContain('ID lineup-draft · 优先级 1');
    expect(document.querySelector('.lineup-archived')?.textContent).toContain('ID lineup-archived · 优先级 1');
    expect(screen.getByRole('button', { name: '设为默认' })).toBeVisible();
    expect(screen.getByRole('button', { name: '下架' })).toBeVisible();
    expect(screen.getByRole('button', { name: '标记待复查' })).toBeVisible();
    expect(screen.getByText('默认路线因媒体故障自动调整，请确认')).toBeVisible();
    expect(within(document.querySelector('.lineup-draft') as HTMLElement).getByRole('button', { name: '归档路线' })).toBeVisible();
    expect(within(document.querySelector('.lineup-draft') as HTMLElement).getByRole('button', { name: '归档主视频' })).toBeVisible();
    expect(within(document.querySelector('.lineup-archived') as HTMLElement).getByRole('button', { name: '恢复路线' })).toBeVisible();
    expect(within(document.querySelector('.lineup-needs_revalidation') as HTMLElement).getByRole('button', { name: '恢复主视频' })).toBeVisible();

    await clickAction('.lineup-published', '设为默认', '/admin/lineups/lineup-published/set-default');
    await clickAction('.lineup-published', '下架', '/admin/lineups/lineup-published/unpublish');
    await clickAction('.lineup-published', '标记待复查', '/admin/lineups/lineup-published/mark-needs-revalidation');
    await clickAction('.lineup-draft', '归档路线', '/admin/lineups/lineup-draft/archive');
    await clickAction('.lineup-draft', '归档主视频', '/admin/lineups/lineup-draft/video/archive');
    await clickAction('.lineup-archived', '恢复路线', '/admin/lineups/lineup-archived/restore');
    await clickAction('.lineup-needs_revalidation', '恢复主视频', '/admin/lineups/lineup-needs/video/restore');
    expect(confirmSpy).toHaveBeenCalledTimes(2);
    confirmSpy.mockRestore();
  });

  it('keeps lifecycle API errors visible on a published route card', async () => {
    const published = adminLineup({ id: 'lineup-published', status: 'published', is_default: true });
    installFetch((url, init) => {
      if (url.endsWith('/admin/maps/mirage')) return jsonResponse(mapPayload.map);
      if (url.endsWith('/admin/session')) return jsonResponse({ authenticated: true });
      if (url.endsWith('/admin/targets')) return jsonResponse([adminTarget]);
      if (url.endsWith('/admin/origins')) return jsonResponse([adminOrigin]);
      if (url.includes('/admin/lineups?status=all')) return jsonResponse([published]);
      if (url.endsWith('/admin/lineups/lineup-published/unpublish') && init?.method === 'POST') {
        return jsonResponse({ error: { code: 'DEFAULT_REPLACEMENT_REQUIRED', message: '请先在另一条已发布路线设为默认。' } }, 409);
      }
      return jsonResponse({});
    });
    renderApp('/admin');
    await login();
    const card = document.querySelector('.lineup-published') as HTMLElement;
    fireEvent.click(within(card).getByRole('button', { name: '下架' }));
    expect(await within(card).findByText('请先在另一条已发布路线设为默认。')).toBeVisible();
  });

  it('logs in, creates drafts, saves, uploads and publishes explicitly', async () => {
    const fetchMock = installAdminBackend();
    renderApp('/admin');
    await login();
    fireEvent.click(screen.getByRole('tab', { name: '点位' }));
    expect(await screen.findByRole('tabpanel', { name: '点位' })).toBeVisible();
    fireEvent.submit(document.getElementById('target-slug')!.closest('form')!);
    expect(await screen.findByText('Target 的 slug 和中文名称为必填项。')).toBeVisible();
    fireEvent.change(document.getElementById('target-slug')!, { target: { value: 'new-target' } });
    fireEvent.change(document.getElementById('target-name-zh')!, { target: { value: '新目标' } });
    fireEvent.submit(document.getElementById('target-slug')!.closest('form')!);
    await waitFor(() => expect(document.getElementById('target-slug')).toHaveValue(''));
    const targetCreate = fetchMock.mock.calls.find(([url, init]) => String(url).endsWith('/admin/targets') && init?.method === 'POST');
    expect(JSON.parse(String((targetCreate?.[1] as RequestInit).body))).not.toHaveProperty('map_x');
    expect(JSON.parse(String((targetCreate?.[1] as RequestInit).body))).not.toHaveProperty('map_y');

    fireEvent.change(document.getElementById('origin-slug')!, { target: { value: 'new-origin' } });
    fireEvent.change(document.getElementById('origin-name-zh')!, { target: { value: '新起点' } });
    fireEvent.submit(document.getElementById('origin-slug')!.closest('form')!);
    await waitFor(() => expect(document.getElementById('origin-slug')).toHaveValue(''));
    const originCreate = fetchMock.mock.calls.find(([url, init]) => String(url).endsWith('/admin/origins') && init?.method === 'POST');
    expect(JSON.parse(String((originCreate?.[1] as RequestInit).body))).not.toHaveProperty('map_x');
    expect(JSON.parse(String((originCreate?.[1] as RequestInit).body))).not.toHaveProperty('map_y');

    fireEvent.click(screen.getByRole('tab', { name: '路线' }));
    expect(await screen.findByRole('tabpanel', { name: '路线' })).toBeVisible();
    const createForm = document.querySelector('.lineup-create-form')!;
    fireEvent.change(document.getElementById('lineup-target')!, { target: { value: adminTarget.id } });
    fireEvent.change(document.getElementById('lineup-origin')!, { target: { value: adminOrigin.id } });
    fireEvent.change(document.getElementById('new-lineup-setup')!, { target: { value: '新站位' } });
    fireEvent.change(document.getElementById('new-lineup-aim')!, { target: { value: '新瞄点' } });
    fireEvent.change(document.getElementById('new-lineup-throw')!, { target: { value: '新投掷' } });
    fireEvent.change(document.getElementById('new-lineup-result')!, { target: { value: '新结果' } });
    fireEvent.submit(createForm);
    await screen.findByText('路线工作台。');
    fireEvent.click(screen.getByRole('button', { name: '编辑草稿' }));
    fireEvent.change(document.getElementById('edit-lineup-1-setup')!, { target: { value: '修改站位' } });
    fireEvent.change(document.getElementById('edit-lineup-1-sort-order')!, { target: { value: '5' } });
    fireEvent.change(document.getElementById('edit-lineup-1-version-note')!, { target: { value: '1.39 当前版本' } });
    fireEvent.click(screen.getByRole('button', { name: '保存教学内容与优先级' }));
    await waitFor(() => expect(screen.getByRole('button', { name: '保存教学内容与优先级' })).toBeEnabled());
    const draftPatches = fetchMock.mock.calls.filter(([url, init]) => String(url).endsWith('/admin/lineups/lineup-1') && init?.method === 'PATCH');
    expect(draftPatches).toHaveLength(1);
    expect(JSON.parse(String((draftPatches[0]?.[1] as RequestInit).body))).toEqual({
      sort_order: 5,
      steps: { setup: '修改站位', aim: '瞄点', throw: '投掷', result: '结果' },
    });
    fireEvent.click(screen.getByRole('button', { name: '保存检查信息' }));
    await waitFor(() => expect(screen.getByRole('button', { name: '保存检查信息' })).toBeEnabled());
    const allDraftPatches = fetchMock.mock.calls.filter(([url, init]) => String(url).endsWith('/admin/lineups/lineup-1') && init?.method === 'PATCH');
    expect(allDraftPatches).toHaveLength(2);
    expect(JSON.parse(String((allDraftPatches[1]?.[1] as RequestInit).body))).toEqual({
      check_info: { last_checked_at: null, game_version_note: '1.39 当前版本' },
    });
    fireEvent.click(screen.getByRole('button', { name: '上传主视频' }));
    expect(await screen.findByText('请选择一个 MP4、WebM 或 MOV 视频文件。')).toBeVisible();
    const fileInput = document.querySelector('input[type="file"]')!;
    fireEvent.change(fileInput, { target: { files: [new File(['mp4'], 'demo.mp4', { type: 'video/mp4' })] } });
    fireEvent.click(screen.getByRole('button', { name: '上传主视频' }));
    expect(await screen.findByText('请填写 rights_note。')).toBeVisible();
    fireEvent.change(screen.getByLabelText('rights_note'), { target: { value: '自录视频，已获授权' } });
    fireEvent.click(screen.getByRole('button', { name: '上传主视频' }));
    await screen.findByText('主视频已就绪');
    fireEvent.click(screen.getByRole('button', { name: '发布路线' }));
    expect(await screen.findByText('请明确选择是否设为默认，再发布。')).toBeVisible();
    fireEvent.click(screen.getByLabelText('设为默认（true）'));
    fireEvent.click(screen.getByRole('button', { name: '发布路线' }));
    expect(await screen.findByText('已发布教学字段只读；请先下架后修改。')).toBeVisible();
    fireEvent.change(screen.getByLabelText('优先级'), { target: { value: '9' } });
    fireEvent.click(screen.getByRole('button', { name: '保存优先级' }));
    await waitFor(() => expect(fetchMock.mock.calls.some(([url, init]) =>
      String(url).endsWith('/admin/lineups/lineup-1')
        && init?.method === 'PATCH'
        && JSON.parse(String(init.body)).sort_order === 9,
    )).toBe(true));
    const priorityPatch = fetchMock.mock.calls.filter(([url, init]) => String(url).endsWith('/admin/lineups/lineup-1') && init?.method === 'PATCH').at(-1);
    expect(JSON.parse(String((priorityPatch?.[1] as RequestInit).body))).toEqual({ sort_order: 9 });
    expect(screen.getByAltText('T 出生点 到 VIP 的已保存视频封面')).toBeVisible();
  });

  it('imports an exact manifest and video set as ready drafts without publishing', async () => {
    const fetchMock = installAdminBackend();
    renderApp('/admin');
    await login();
    const manifest = new File([JSON.stringify({
      lineups: [{
        action: 'create_upload_publish',
        origin_slug: adminOrigin.slug,
        target_slug: adminTarget.slug,
        sort_order: 7,
        source_file: 'batch-demo.mp4',
        steps: { setup: '批量站位', aim: '批量瞄点', throw: '左键投掷。', result: '批量结果' },
      }],
    })], 'manifest.json', { type: 'application/json' });
    const video = new File(['mp4'], 'batch-demo.mp4', { type: 'video/mp4' });
    fireEvent.change(document.getElementById('batch-manifest')!, { target: { files: [manifest] } });
    fireEvent.change(document.getElementById('batch-videos')!, { target: { files: [video] } });
    fireEvent.click(screen.getByRole('button', { name: '导入为视频就绪草稿' }));

    expect(await screen.findByText(/导入完成：1\/1；新建 1 条，上传 1 个视频/)).toBeVisible();
    const createCall = fetchMock.mock.calls.find(([url, init]) => String(url).endsWith('/admin/lineups') && init?.method === 'POST');
    expect(JSON.parse(String((createCall?.[1] as RequestInit).body))).toEqual({
      target_id: adminTarget.id,
      origin_id: adminOrigin.id,
      steps: { setup: '批量站位', aim: '批量瞄点', throw: '左键投掷。', result: '批量结果' },
      sort_order: 7,
      check_info: { last_checked_at: null, game_version_note: null },
    });
    const uploadCall = fetchMock.mock.calls.find(([url, init]) => String(url).endsWith('/admin/lineups/lineup-1/video') && init?.method === 'POST');
    const uploadBody = (uploadCall?.[1] as RequestInit).body as FormData;
    expect((uploadBody.get('file') as File).name).toBe('batch-demo.mp4');
    expect(uploadBody.get('rights_note')).toBe('内容提供者本人录制的 Mirage 烟雾教学视频，提供用于 SmokeLine 教学展示；作者已确认自行验证。');
    expect(fetchMock.mock.calls.some(([url]) => String(url).includes('/publish'))).toBe(false);
    expect(document.getElementById('batch-manifest')).toHaveValue('');
    expect(document.getElementById('batch-videos')).toHaveValue('');
  });

  it('rejects a batch when selected video names do not exactly match the manifest', async () => {
    const fetchMock = installAdminBackend();
    renderApp('/admin');
    await login();
    const before = fetchMock.mock.calls.length;
    const manifest = new File([JSON.stringify({
      lineups: [{
        action: 'create_upload_publish', origin_slug: adminOrigin.slug, target_slug: adminTarget.slug,
        sort_order: 8, source_file: 'expected.mp4',
        steps: { setup: '站位', aim: '瞄点', throw: '投掷', result: '结果' },
      }],
    })], 'manifest.json', { type: 'application/json' });
    fireEvent.change(document.getElementById('batch-manifest')!, { target: { files: [manifest] } });
    fireEvent.change(document.getElementById('batch-videos')!, { target: { files: [new File(['x'], 'wrong.mp4', { type: 'video/mp4' })] } });
    fireEvent.click(screen.getByRole('button', { name: '导入为视频就绪草稿' }));
    expect(await screen.findByText(/所选视频与清单不完全一致/)).toBeVisible();
    expect(fetchMock.mock.calls.slice(before).some(([url, init]) => String(url).endsWith('/admin/lineups') && init?.method === 'POST')).toBe(false);
  });

  it('preflights every batch item before the first write', async () => {
    const fetchMock = installAdminBackend();
    renderApp('/admin');
    await login();
    const before = fetchMock.mock.calls.length;
    const manifest = new File([JSON.stringify({
      lineups: [
        {
          action: 'create_upload_publish', origin_slug: adminOrigin.slug, target_slug: adminTarget.slug,
          sort_order: 7, source_file: 'valid.mp4',
          steps: { setup: '站位一', aim: '瞄点一', throw: '投掷一', result: '结果一' },
        },
        {
          action: 'create_upload_publish', origin_slug: adminOrigin.slug, target_slug: 'missing-target',
          sort_order: 8, source_file: 'invalid.mp4',
          steps: { setup: '站位二', aim: '瞄点二', throw: '投掷二', result: '结果二' },
        },
      ],
    })], 'manifest.json', { type: 'application/json' });
    const videos = [
      new File(['x'], 'valid.mp4', { type: 'video/mp4' }),
      new File(['x'], 'invalid.mp4', { type: 'video/mp4' }),
    ];
    fireEvent.change(document.getElementById('batch-manifest')!, { target: { files: [manifest] } });
    fireEvent.change(document.getElementById('batch-videos')!, { target: { files: videos } });
    fireEvent.click(screen.getByRole('button', { name: '导入为视频就绪草稿' }));

    expect(await screen.findByText(/找不到启用中的 Target：missing-target/)).toBeVisible();
    expect(fetchMock.mock.calls.slice(before).some(([, init]) => init?.method === 'POST')).toBe(false);
  });

  it('safely skips an exact ready draft when a batch is resumed', async () => {
    currentLineup = adminLineup({
      primary_video: {
        id: 'video-ready', status: 'ready', duration_ms: 2100, size_bytes: 100,
        rights_note: '内容提供者本人录制的 Mirage 烟雾教学视频，提供用于 SmokeLine 教学展示；作者已确认自行验证。',
        failure_reason: null, url: videoUrl, poster_url: poster,
      },
    });
    const fetchMock = installAdminBackend();
    renderApp('/admin');
    await login();
    const before = fetchMock.mock.calls.length;
    const manifest = new File([JSON.stringify({
      lineups: [{
        action: 'create_upload_publish', origin_slug: adminOrigin.slug, target_slug: adminTarget.slug,
        sort_order: adminDraft.sort_order, source_file: 'resume.mp4', steps: adminDraft.steps,
      }],
    })], 'manifest.json', { type: 'application/json' });
    fireEvent.change(document.getElementById('batch-manifest')!, { target: { files: [manifest] } });
    fireEvent.change(document.getElementById('batch-videos')!, { target: { files: [new File(['x'], 'resume.mp4', { type: 'video/mp4' })] } });
    fireEvent.click(screen.getByRole('button', { name: '导入为视频就绪草稿' }));

    expect(await screen.findByText(/导入完成：1\/1；新建 0 条，上传 0 个视频，复用 1 条已就绪草稿/)).toBeVisible();
    expect(fetchMock.mock.calls.slice(before).some(([, init]) => init?.method === 'POST')).toBe(false);
  });

  it('shows every provider metric field in the metrics tab', async () => {
    installFetch((url) => {
      if (url.endsWith('/admin/session')) return jsonResponse({ authenticated: true });
      if (url.endsWith('/admin/maps/mirage')) return jsonResponse(mapPayload.map);
      if (url.endsWith('/admin/targets')) return jsonResponse([adminTarget]);
      if (url.endsWith('/admin/origins')) return jsonResponse([adminOrigin]);
      if (url.includes('/admin/lineups?status=all')) return jsonResponse([]);
      if (url.endsWith('/admin/metrics/summary')) return jsonResponse({
        window_hours: 24, from: '2026-09-04T00:00:00Z', to: '2026-09-05T00:00:00Z',
        response_counts: { match: 1, route_matches: 2, clarification: 3, not_found: 5, out_of_scope: 6 },
        latency_ms: { p50: 12, p95: 34 },
         provider_usage: {
           intent: { attempts: 1, success: 1, failed: 0, unknown: 0, input_tokens: 10, output_tokens: 11 },
           asr: { attempts: 3, success: 2, failed: 0, unknown: 1, reserved_seconds: 42 },
         },
         not_found_combinations: [{ target_slug: 'vip', origin_slug: 't-spawn', count: 2 }],
         errors: [{ code: 'UPSTREAM_UNAVAILABLE', count: 1 }],
       });
      return jsonResponse({});
    });
    renderApp('/admin');
    await login();
    fireEvent.click(screen.getByRole('tab', { name: '指标' }));
    expect(await screen.findByRole('heading', { name: '指标摘要。' })).toBeVisible();
    expect(screen.getAllByText('input_tokens')).toHaveLength(1);
    expect(screen.getAllByText('output_tokens')).toHaveLength(1);
    expect(screen.getByText('reserved_seconds')).toBeVisible();
    expect(screen.getByText('42')).toBeVisible();
    expect(screen.getByText('vip / t-spawn · 2')).toBeVisible();
    expect(screen.getByText('UPSTREAM_UNAVAILABLE · 1')).toBeVisible();
  });

  it('renders validation details and clears the in-memory credential on a 401', async () => {
    let targetCalls = 0;
    installFetch((url, init) => {
      if (url.endsWith('/admin/session')) {
        return jsonResponse({ authenticated: true });
      }
      if (url.endsWith('/admin/maps/mirage')) return jsonResponse(mapPayload.map);
      if (url.endsWith('/admin/targets') && init?.method === 'POST') {
        targetCalls += 1;
        if (targetCalls === 1) return jsonResponse({ error: { code: 'VALIDATION_ERROR', message: '请检查输入。', details: { fields: [{ path: 'rights_note', reason: 'invalid_text' }] } } }, 422);
        if (targetCalls === 2) return jsonResponse({ error: { code: 'VALIDATION_ERROR', message: '请检查输入。', details: { fields: [{ path: 'request', reason: 'value_error' }] } } }, 422);
        return jsonResponse({ error: { code: 'UNAUTHORIZED', message: '登录已失效。' } }, 401);
      }
      if (url.endsWith('/admin/targets')) return jsonResponse([adminTarget]);
      if (url.endsWith('/admin/origins')) return jsonResponse([adminOrigin]);
      if (url.includes('/admin/lineups?status=all')) return jsonResponse([adminDraft]);
      return jsonResponse({ error: { code: 'VALIDATION_ERROR', message: '请检查输入。', details: { fields: [{ path: 'rights_note', reason: 'invalid_text' }] } } }, 422);
    });
    renderApp('/admin');
    await login();
    fireEvent.click(screen.getByRole('tab', { name: '点位' }));
    fireEvent.change(document.getElementById('target-slug')!, { target: { value: 'x' } });
    fireEvent.change(document.getElementById('target-name-zh')!, { target: { value: 'X' } });
    fireEvent.submit(document.getElementById('target-slug')!.closest('form')!);
    expect(await screen.findByText('请检查输入。（rights_note: 含不允许的控制字符）')).toBeVisible();
    expect(document.getElementById('target-slug')).toHaveValue('x');
    fireEvent.submit(document.getElementById('target-slug')!.closest('form')!);
    expect(await screen.findByText('请检查输入。（request: 不合法）')).toBeVisible();
    expect(document.getElementById('target-slug')).toHaveValue('x');
    fireEvent.submit(document.getElementById('target-slug')!.closest('form')!);
    expect(await screen.findByRole('heading', { name: '进入内容管理。' })).toBeVisible();
  });

  it('renders loaded nullable and zero coordinates without collapsing the directory', async () => {
    const zeroTarget = { ...adminTarget, id: 'target-zero', slug: 'zero-target', name_zh: '0边界目标', map_x: 0, map_y: 0 };
    const nullTarget = { ...adminTarget, id: 'target-null', slug: 'null-target', name_zh: '待定位目标', map_x: null, map_y: null };
    installFetch((url) => {
      if (url.endsWith('/admin/session')) return jsonResponse({ authenticated: true });
      if (url.endsWith('/admin/maps/mirage')) return jsonResponse(mapPayload.map);
      if (url.endsWith('/admin/targets')) return jsonResponse([adminTarget, zeroTarget, nullTarget]);
      if (url.endsWith('/admin/origins')) return jsonResponse([adminOrigin]);
      if (url.includes('/admin/lineups?status=all')) return jsonResponse([]);
      return jsonResponse({});
    });
    renderApp('/admin');
    await login();
    fireEvent.click(screen.getByRole('tab', { name: '点位' }));
    expect(await screen.findByText('待定位')).toBeVisible();
    expect(screen.getByText('坐标 0, 0')).toBeVisible();
    expect(screen.getByText('0边界目标')).toBeVisible();
  });

  it('keeps archived points visible for maintenance but out of new-lineup selectors', async () => {
    const archivedTarget = { ...adminTarget, id: 'target-archived', slug: 'old-target', name_zh: '旧目标', is_active: false };
    const archivedOrigin = { ...adminOrigin, id: 'origin-archived', slug: 'old-origin', name_zh: '旧起点', is_active: false };
    installFetch((url) => {
      if (url.endsWith('/admin/session')) return jsonResponse({ authenticated: true });
      if (url.endsWith('/admin/maps/mirage')) return jsonResponse(mapPayload.map);
      if (url.endsWith('/admin/targets?include_archived=true')) return jsonResponse([adminTarget, archivedTarget]);
      if (url.endsWith('/admin/origins?include_archived=true')) return jsonResponse([adminOrigin, archivedOrigin]);
      if (url.endsWith('/admin/targets')) return jsonResponse([adminTarget]);
      if (url.endsWith('/admin/origins')) return jsonResponse([adminOrigin]);
      if (url.includes('/admin/lineups?status=all')) return jsonResponse([]);
      return jsonResponse({});
    });
    renderApp('/admin');
    await login();
    fireEvent.click(screen.getByRole('tab', { name: '点位' }));
    fireEvent.click(screen.getByRole('checkbox', { name: '显示已归档' }));
    expect(await screen.findByText('旧目标')).toBeVisible();
    expect(screen.getByText('旧起点')).toBeVisible();
    fireEvent.click(screen.getByRole('tab', { name: '路线' }));
    expect(within(document.getElementById('lineup-target')!).queryByRole('option', { name: /旧目标/ })).toBeNull();
    expect(within(document.getElementById('lineup-origin')!).queryByRole('option', { name: /旧起点/ })).toBeNull();
  });

  it('uses the authenticated catalog status when the public map is unavailable', async () => {
    const fetchMock = installFetch((url) => {
      if (url.endsWith('/admin/session')) return jsonResponse({ authenticated: true });
      if (url.endsWith('/admin/maps/mirage')) return jsonResponse({ ...mapPayload.map, asset_status: 'approved' });
      if (url.endsWith('/maps/mirage')) return jsonResponse({ error: { code: 'CONTENT_CONFIGURATION_ERROR', message: '公开内容配置暂时异常，请稍后重试。' } }, 503);
      if (url.endsWith('/admin/targets')) return jsonResponse([adminTarget]);
      if (url.endsWith('/admin/origins')) return jsonResponse([adminOrigin]);
      if (url.includes('/admin/lineups?status=all')) return jsonResponse([]);
      return jsonResponse({});
    });
    renderApp('/admin');
    await login();
    fireEvent.click(screen.getByRole('tab', { name: '点位' }));
    expect(await screen.findByText('APPROVED')).toBeVisible();
    expect(screen.getByRole('img', { name: 'Mirage 正式地图，点击设置 Target 预览位置' })).toBeVisible();
    expect(fetchMock.mock.calls.some(([url]) => String(url).endsWith('/admin/maps/mirage'))).toBe(true);
    expect(fetchMock.mock.calls.some(([url]) => String(url) === '/api/v1/maps/mirage')).toBe(false);
  });

  it('hides clear positioning when the published-lineup directory did not load', async () => {
    installFetch((url) => {
      if (url.endsWith('/admin/session')) return jsonResponse({ authenticated: true });
      if (url.endsWith('/admin/maps/mirage')) return jsonResponse({ ...mapPayload.map, asset_status: 'approved' });
      if (url.endsWith('/admin/targets')) return jsonResponse([{ ...adminTarget, map_x: 24, map_y: 36 }]);
      if (url.endsWith('/admin/origins')) return jsonResponse([]);
      if (url.includes('/admin/lineups?status=all')) {
        return jsonResponse({ error: { code: 'UPSTREAM_UNAVAILABLE', message: '路线目录暂时不可用。' } }, 503);
      }
      return jsonResponse({});
    });
    renderApp('/admin');
    await login();
    fireEvent.click(screen.getByRole('tab', { name: '点位' }));
    await screen.findByRole('img', { name: 'Mirage 正式地图，点击设置 Target 预览位置' });
    expect(await screen.findByText('路线目录暂时不可用。')).toBeVisible();
    expect(screen.queryByRole('button', { name: '清除定位' })).not.toBeInTheDocument();
  });

  it('turns a map click into an explicit Target position save', async () => {
    let targetState: PointOut = { ...adminTarget, map_x: null, map_y: null };
    const fetchMock = installFetch((url, init) => {
      const methodName = init?.method ?? 'GET';
      if (url.endsWith('/admin/session')) return jsonResponse({ authenticated: true });
      if (url.endsWith('/admin/maps/mirage')) return jsonResponse(mapPayload.map);
      if (url.endsWith('/admin/targets/target-1/position') && methodName === 'PATCH') {
        targetState = { ...targetState, ...JSON.parse(String(init?.body ?? '{}')) } as PointOut;
        return jsonResponse(targetState);
      }
      if (url.endsWith('/admin/targets')) return jsonResponse([targetState]);
      if (url.endsWith('/admin/origins')) return jsonResponse([]);
      if (url.includes('/admin/lineups?status=all')) return jsonResponse([]);
      return jsonResponse({});
    });
    renderApp('/admin');
    await login();
    fireEvent.click(screen.getByRole('tab', { name: '点位' }));
    const svg = await screen.findByRole('img', { name: 'Mirage 正式地图，点击设置 Target 预览位置' });
    const TestDOMPoint = class {
      constructor(readonly x: number, readonly y: number) {}
      matrixTransform(matrix: { scaleX: number; scaleY: number; offsetX: number; offsetY: number }) {
        return { x: this.x * matrix.scaleX + matrix.offsetX, y: this.y * matrix.scaleY + matrix.offsetY };
      }
    };
    vi.stubGlobal('DOMPoint', TestDOMPoint);
    Object.defineProperty(svg, 'getScreenCTM', {
      configurable: true,
      value: () => ({ inverse: () => ({ scaleX: 1, scaleY: 1, offsetX: 0, offsetY: 0 }) }),
    });
    const save = screen.getByRole('button', { name: '保存目标位置' });
    expect(save).toBeDisabled();
    fireEvent.click(svg, { clientX: 240, clientY: 360 });
    await waitFor(() => expect(save).toBeEnabled());
    fireEvent.click(save);
    await waitFor(() => expect(fetchMock.mock.calls.some(([url, init]) =>
      String(url).endsWith('/admin/targets/target-1/position')
        && init?.method === 'PATCH'
        && JSON.parse(String(init.body)).map_x === 24
        && JSON.parse(String(init.body)).map_y === 36,
    )).toBe(true));
  });

  it('validates, saves and archives a point without losing its directory form', async () => {
    const fetchMock = installAdminBackend();
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(true);
    renderApp('/admin');
    await login();
    fireEvent.click(screen.getByRole('tab', { name: '点位' }));
    const targetItem = document.querySelector('.point-directory-item') as HTMLElement;
    fireEvent.click(within(targetItem).getByRole('button', { name: '编辑' }));
    fireEvent.change(document.getElementById('edit-point-description-target-1')!, { target: { value: '更新说明' } });
    fireEvent.click(within(targetItem).getByRole('button', { name: '保存点位' }));
    await waitFor(() => expect(within(targetItem).getByRole('button', { name: '编辑' })).toBeVisible());
    const archiveBefore = fetchMock.mock.calls.length;
    fireEvent.click(within(targetItem).getByRole('button', { name: '归档' }));
    await waitFor(() => expect(fetchMock.mock.calls.slice(archiveBefore).some(([url, init]) => String(url).endsWith('/admin/targets/target-1/archive') && init?.method === 'POST')).toBe(true));
    expect(confirmSpy).toHaveBeenCalledWith('确定归档Target“VIP”（vip）吗？');
    confirmSpy.mockRestore();
  });

  it('keeps a login failure in the login form', async () => {
    installFetch((url) => url.endsWith('/admin/session')
      ? jsonResponse({ error: { code: 'UNAUTHORIZED', message: '用户名或密码错误。' } }, 401)
      : jsonResponse([]));
    renderApp('/admin');
    fireEvent.change(screen.getByLabelText('用户名'), { target: { value: 'admin' } });
    fireEvent.change(screen.getByLabelText('密码'), { target: { value: 'wrong' } });
    fireEvent.click(screen.getByRole('button', { name: '登录' }));
    expect(await screen.findByText('用户名或密码错误。')).toBeVisible();
  });
});

describe('router fallback', () => {
  it('shows the safe not-found page', () => {
    renderApp('/missing');
    expect(screen.getByRole('heading', { name: '找不到这页。' })).toBeVisible();
    expect(screen.getByRole('link', { name: '返回教学页' })).toHaveAttribute('href', '/');
  });
});

it('keeps the API error type stable', () => {
  const error = new ApiError('bad', 418, 'TEST', { safe: true }, 'req');
  expect(error).toBeInstanceOf(Error);
  expect(error.requestId).toBe('req');
});
