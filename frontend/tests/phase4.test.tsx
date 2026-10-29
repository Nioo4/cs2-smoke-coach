import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import App from '../src/app/App';

const target = { slug: 'vip', name_zh: 'VIP', map_x: 24, map_y: 36, origin_count: 1 };
const mapPayload = {
  map: { slug: 'mirage' as const, name_zh: '荒漠迷城', asset_url: '/assets/maps/mirage.svg', asset_status: 'placeholder' as const },
  published_lineup_count: 1,
  targets: [target],
};

function response(payload: unknown, status = 200): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    headers: new Headers({ 'Content-Type': 'application/json' }),
    json: async () => payload,
  } as Response;
}

function renderApp() {
  return render(<MemoryRouter><App /></MemoryRouter>);
}

function installFetch(handler: (input: RequestInfo | URL, init?: RequestInit) => Response | Promise<Response>) {
  const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => handler(input, init));
  vi.stubGlobal('fetch', fetchMock);
  return fetchMock;
}

function notFoundPayload(conversationId: string) {
  return {
    type: 'not_found' as const,
    conversation_id: conversationId,
    reason: 'NO_LOCAL_RESULT' as const,
    message: '暂无符合该位置要求的站内路线，可换个起点或稍后再试。',
  };
}

describe('Phase 4 public search surface', () => {
  beforeEach(() => {
    Object.defineProperty(window, 'matchMedia', {
      configurable: true,
      value: () => ({ matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn() }),
    });
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('renders a local no-result notice and carries the in-memory conversation', async () => {
    const searchBodies: Array<{ conversation_id?: string | null }> = [];
    let resolveMap: ((value: Response) => void) | undefined;
    installFetch(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.endsWith('/maps/mirage')) {
        return new Promise<Response>((resolve) => {
          resolveMap = resolve;
        });
      }
      if (url.endsWith('/search')) {
        searchBodies.push(JSON.parse(String(init?.body ?? '{}')) as { conversation_id?: string | null });
        return response(notFoundPayload('conversation-1'));
      }
      return response({ target: { slug: 'vip', name_zh: 'VIP', map_x: 24, map_y: 36 }, origins: [] });
    });
    renderApp();

    const input = await screen.findByRole('textbox', { name: '搜索路线' });
    fireEvent.change(input, { target: { value: 'VIP' } });
    fireEvent.submit(input.closest('form')!);
    expect(await screen.findByText('暂无符合该位置要求的站内路线，可换个起点或稍后再试。')).toBeVisible();
    expect(screen.queryByRole('region', { name: '外部候选视频' })).not.toBeInTheDocument();
    await waitFor(() => expect(resolveMap).toBeDefined());
    resolveMap!(response(mapPayload));
    expect(await screen.findByRole('button', { name: /VIP，1 个可用起点/ })).toBeVisible();

    fireEvent.submit(input.closest('form')!);
    await waitFor(() => expect(searchBodies).toHaveLength(2));
    expect(searchBodies[0]?.conversation_id).toBeNull();
    expect(searchBodies[1]?.conversation_id).toBe('conversation-1');
  });

  it('keeps drawer detail context while map Target navigation starts a fresh query', async () => {
    const searchBodies: Array<{ conversation_id?: string | null }> = [];
    const approvedMap = {
      ...mapPayload,
      map: { ...mapPayload.map, asset_status: 'approved' as const },
    };
    const detail = {
      id: '11111111-1111-4111-8111-111111111111',
      target: { slug: 'vip', name_zh: 'VIP', map_x: 24, map_y: 36 },
      origin: { slug: 't-spawn', name_zh: 'T 出生点', map_x: 12, map_y: 80 },
      is_default: true,
      source: 'local_curated' as const,
      steps: { setup: '站位', aim: '瞄点', throw: '跳投', result: '封烟' },
      check_info: { last_checked_at: null, game_version_note: null },
      primary_video: { url: '/media/route.mp4', poster_url: '/media/route.webp', duration_ms: 1000 },
      other_lineups: [],
    };
    installFetch(async (input, init) => {
      const url = String(input);
      if (url.endsWith('/maps/mirage')) return response(approvedMap);
      if (url.endsWith('/search')) {
        searchBodies.push(JSON.parse(String(init?.body ?? '{}')) as { conversation_id?: string | null });
        return response({
          type: 'route_matches',
          conversation_id: `conversation-${searchBodies.length}`,
          source: 'local_curated',
          intent: { map: 'mirage', grenade: 'smoke', target_slug: 'vip', origin_slug: null },
          notices: [],
          routes: [{ target: detail.target, origin: detail.origin, lineup: {
            id: detail.id,
            is_default: true,
            variant_hint: '默认路线',
            primary_video: detail.primary_video,
          } }],
        });
      }
      if (url.includes('/lineups/')) return response(detail);
      return response({ target: detail.target, origins: [] });
    });
    renderApp();

    const input = await screen.findByRole('textbox', { name: '搜索路线' });
    fireEvent.change(input, { target: { value: 'VIP' } });
    fireEvent.submit(input.closest('form')!);
    await screen.findByRole('heading', { name: 'VIP · 可用起点' });

    fireEvent.click(screen.getByRole('button', { name: /T 出生点 → VIP：查看完整教学/ }));
    await screen.findByRole('heading', { name: 'T 出生点 → VIP' });
    fireEvent.submit(input.closest('form')!);
    await waitFor(() => expect(searchBodies).toHaveLength(2));
    expect(searchBodies[1]?.conversation_id).toBe('conversation-1');

    await screen.findByRole('heading', { name: 'VIP · 可用起点' });
    fireEvent.click(document.querySelector('.map-target-button')!);
    await screen.findByRole('heading', { name: 'VIP · 可用起点' });
    fireEvent.submit(input.closest('form')!);
    await waitFor(() => expect(searchBodies).toHaveLength(3));
    expect(searchBodies[2]?.conversation_id).toBeNull();
  });

  it('ignores an aborted late response and never lets an older search overwrite the drawer', async () => {
    const deferred: Array<{ resolve: (value: Response) => void; signal?: AbortSignal }> = [];
    installFetch((input, init) => {
      const url = String(input);
      if (url.endsWith('/maps/mirage')) return response(mapPayload);
      if (url.endsWith('/search')) {
        return new Promise<Response>((resolve) => {
          deferred.push({ resolve, signal: init?.signal as AbortSignal | undefined });
        });
      }
      return response(mapPayload);
    });
    renderApp();
    const input = await screen.findByRole('textbox', { name: '搜索路线' });
    fireEvent.change(input, { target: { value: '旧查询' } });
    fireEvent.submit(input.closest('form')!);
    await waitFor(() => expect(deferred).toHaveLength(1));
    fireEvent.change(input, { target: { value: '新查询' } });
    fireEvent.submit(input.closest('form')!);
    await waitFor(() => expect(deferred).toHaveLength(2));
    expect(deferred[0]?.signal?.aborted).toBe(true);
    deferred[1]?.resolve(response(notFoundPayload('new-conversation')));
    await screen.findByText('暂无符合该位置要求的站内路线，可换个起点或稍后再试。');
    deferred[0]?.resolve(response({
      type: 'match',
      conversation_id: 'old-conversation',
      source: 'local_curated',
      confidence: 1,
      intent: { map: 'mirage', grenade: 'smoke', target_slug: 'vip', origin_slug: 't-spawn' },
      notices: [],
      lineup: {
        id: 'old-lineup',
        target: { slug: 'vip', name_zh: 'VIP', map_x: 24, map_y: 36 },
        origin: { slug: 't-spawn', name_zh: 'T 出生点', map_x: 12, map_y: 80 },
        is_default: true,
        source: 'local_curated',
        steps: { setup: '站位', aim: '瞄点', throw: '跳投', result: '封烟' },
        check_info: { last_checked_at: null, game_version_note: null },
        primary_video: { url: '/media/old.mp4', poster_url: '/media/old.webp', duration_ms: 1000 },
        other_lineups: [],
      },
    }));
    expect(screen.getByText('暂无符合该位置要求的站内路线，可换个起点或稍后再试。')).toBeVisible();
    expect(screen.queryByRole('heading', { name: 'T 出生点 → VIP' })).not.toBeInTheDocument();
  });

  it('does not carry a conversation across remount', async () => {
    const bodies: Array<Record<string, unknown>> = [];
    installFetch(async (input, init) => {
      const url = String(input);
      if (url.endsWith('/maps/mirage')) return response(mapPayload);
      if (url.endsWith('/search')) {
        bodies.push(JSON.parse(String(init?.body ?? '{}')) as Record<string, unknown>);
        return response(notFoundPayload(`conversation-${bodies.length}`));
      }
      return response(mapPayload);
    });
    const firstRender = renderApp();
    let input = await screen.findByRole('textbox', { name: '搜索路线' });
    fireEvent.change(input, { target: { value: '第一次' } });
    fireEvent.submit(input.closest('form')!);
    await screen.findByText('暂无符合该位置要求的站内路线，可换个起点或稍后再试。');
    firstRender.unmount();
    renderApp();
    input = await screen.findByRole('textbox', { name: '搜索路线' });
    fireEvent.change(input, { target: { value: '重新开始' } });
    fireEvent.submit(input.closest('form')!);
    await waitFor(() => expect(bodies).toHaveLength(2));
    expect(bodies[1]?.conversation_id).toBeNull();
  });

  it('keeps a local match usable after a provider error and does not expose action controls', async () => {
    let searches = 0;
    installFetch(async (input) => {
      const url = String(input);
      if (url.endsWith('/maps/mirage')) return response(mapPayload);
      if (url.endsWith('/search')) {
        searches += 1;
        if (searches === 1) return response({ error: { code: 'INTENT_AI_UNAVAILABLE', message: '意图解析服务暂不可用。' } }, 503);
        return response({
          type: 'match',
          conversation_id: 'local-conversation',
          source: 'local_curated',
          confidence: 1,
          intent: { map: 'mirage', grenade: 'smoke', target_slug: 'vip', origin_slug: 't-spawn' },
          notices: [],
          lineup: {
            id: 'lineup-1',
            target: { slug: 'vip', name_zh: 'VIP', map_x: 24, map_y: 36 },
            origin: { slug: 't-spawn', name_zh: 'T 出生点', map_x: 12, map_y: 80 },
            is_default: true,
            source: 'local_curated',
            steps: { setup: '站位', aim: '瞄点', throw: '跳投', result: '封烟' },
            check_info: { last_checked_at: null, game_version_note: null },
            primary_video: { url: '/media/route.mp4', poster_url: '/media/route.webp', duration_ms: 1000 },
            other_lineups: [],
          },
        });
      }
      return response(mapPayload);
    });
    renderApp();
    const input = await screen.findByRole('textbox', { name: '搜索路线' });
    fireEvent.change(input, { target: { value: '模糊查询' } });
    fireEvent.submit(input.closest('form')!);
    await screen.findByText('意图解析服务暂不可用。');
    fireEvent.change(input, { target: { value: 'VIP 烟' } });
    fireEvent.submit(input.closest('form')!);
    await screen.findByRole('heading', { name: 'T 出生点 → VIP' });
    expect(screen.queryByLabelText(/投法|麦克风/)).not.toBeInTheDocument();
  });

  it('submits only the server slug for clarification and preserves input/session on a disabled error', async () => {
    const bodies: unknown[] = [];
    installFetch(async (input, init) => {
      const url = String(input);
      if (url.endsWith('/maps/mirage')) return response(mapPayload);
      if (url.endsWith('/search')) {
        bodies.push(JSON.parse(String(init?.body ?? '{}')));
        if (bodies.length === 1) {
          return response({
            type: 'clarification',
            conversation_id: 'clarification-1',
            question: '你想封哪个目标位置？',
            field: 'target',
            options: [{ value: 'vip', label: 'VIP 展示名' }],
          });
        }
        return response({
          error: { code: 'INTENT_AI_UNAVAILABLE', message: '意图解析服务暂不可用，请使用明确的目标名称。' },
        }, 503);
      }
      return response(mapPayload);
    });
    renderApp();
    const input = await screen.findByRole('textbox', { name: '搜索路线' });
    fireEvent.change(input, { target: { value: '模糊位置' } });
    input.focus();
    fireEvent.submit(input.closest('form')!);
    expect(await screen.findByRole('heading', { name: '选择目标位置' })).toBeVisible();
    fireEvent.click(screen.getByRole('button', { name: 'VIP 展示名' }));
    expect((await screen.findAllByText('意图解析服务暂不可用，请使用明确的目标名称。')).length).toBeGreaterThanOrEqual(2);
    expect(bodies[1]).toEqual({
      kind: 'selection',
      conversation_id: 'clarification-1',
      selection: { field: 'target', value: 'vip' },
    });
    expect(input).toHaveValue('模糊位置');
    expect(bodies).toHaveLength(2);
  });

  it('does not send a request for “都不是” and keeps session memory out of storage', async () => {
    const fetchMock = installFetch(async (input) => {
      const url = String(input);
      if (url.endsWith('/maps/mirage')) return response(mapPayload);
      if (url.endsWith('/search')) {
        return response({
          type: 'clarification',
          conversation_id: 'clarification-2',
          question: '你想封哪个目标位置？',
          field: 'target',
          options: [{ value: 'vip', label: 'VIP' }],
        });
      }
      return response(mapPayload);
    });
    window.localStorage.clear();
    renderApp();
    const input = await screen.findByRole('textbox', { name: '搜索路线' });
    fireEvent.change(input, { target: { value: '不确定的位置' } });
    input.focus();
    fireEvent.submit(input.closest('form')!);
    await screen.findByRole('heading', { name: '选择目标位置' });
    fireEvent.click(screen.getByRole('button', { name: '都不是，修改输入' }));
    await waitFor(() => expect(screen.queryByRole('heading', { name: '选择目标位置' })).not.toBeInTheDocument());
    expect(fetchMock.mock.calls.filter(([url]) => String(url).endsWith('/search'))).toHaveLength(1);
    expect(window.localStorage.length).toBe(0);
    expect(input).toHaveValue('不确定的位置');
    await waitFor(() => expect(input).toHaveFocus());
  });

  it('shows the provider error without discarding the latest input or conversation id', async () => {
    let searchCount = 0;
    const bodies: Array<Record<string, unknown>> = [];
    installFetch(async (input, init) => {
      const url = String(input);
      if (url.endsWith('/maps/mirage')) return response(mapPayload);
      if (url.endsWith('/search')) {
        searchCount += 1;
        bodies.push(JSON.parse(String(init?.body ?? '{}')) as Record<string, unknown>);
        if (searchCount === 1) return response(notFoundPayload('conversation-3'));
        return response({ error: { code: 'INTENT_AI_UNAVAILABLE', message: '意图解析服务暂不可用，请稍后再试。' } }, 503);
      }
      return response(mapPayload);
    });
    renderApp();
    const input = await screen.findByRole('textbox', { name: '搜索路线' });
    fireEvent.change(input, { target: { value: 'VIP 烟' } });
    fireEvent.submit(input.closest('form')!);
    await screen.findByText('暂无符合该位置要求的站内路线，可换个起点或稍后再试。');
    fireEvent.change(input, { target: { value: '新的查询' } });
    fireEvent.submit(input.closest('form')!);
    expect(await screen.findByText('意图解析服务暂不可用，请稍后再试。')).toBeVisible();
    expect(input).toHaveValue('新的查询');
    expect(bodies[1]?.conversation_id).toBe('conversation-3');
  });

  it('uses a mobile dialog for a local no-result notice and closes on escape', async () => {
    Object.defineProperty(window, 'matchMedia', {
      configurable: true,
      value: (query: string) => ({
        matches: query === '(max-width: 1023px)',
        addEventListener: vi.fn(),
        removeEventListener: vi.fn(),
      }),
    });
    installFetch(async (input) => {
      const url = String(input);
      if (url.endsWith('/maps/mirage')) return response(mapPayload);
      if (url.endsWith('/search')) return response(notFoundPayload('conversation-4'));
      return response(mapPayload);
    });
    renderApp();
    const input = await screen.findByRole('textbox', { name: '搜索路线' });
    fireEvent.change(input, { target: { value: 'VIP' } });
    input.focus();
    fireEvent.submit(input.closest('form')!);
    const dialog = await screen.findByRole('dialog', { name: '检索结果' });
    expect(dialog).toHaveClass('drawer-mobile');
    expect(dialog).toHaveTextContent('暂无符合该位置要求的站内路线');
    expect(dialog.querySelectorAll('iframe, video, img, object, embed')).toHaveLength(0);
    fireEvent.keyDown(dialog, { key: 'Escape' });
    await waitFor(() => expect(screen.queryByRole('dialog', { name: '检索结果' })).not.toBeInTheDocument());
    expect(input).toHaveFocus();
  });
});
