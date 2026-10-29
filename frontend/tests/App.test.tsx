import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import App from '../src/app/App';

const mapPayload = {
  map: { slug: 'mirage', name_zh: '荒漠迷城', asset_url: '/assets/maps/mirage.svg', asset_status: 'placeholder' },
  published_lineup_count: 0,
  targets: [],
};

function renderApp(initialEntry = '/') {
  return render(<MemoryRouter initialEntries={[initialEntry]}><App /></MemoryRouter>);
}

beforeEach(() => {
  vi.stubGlobal('fetch', vi.fn(async () => ({
    ok: true,
    status: 200,
    headers: new Headers(),
    json: async () => mapPayload,
  })));
});

afterEach(() => {
  vi.unstubAllEnvs();
  vi.unstubAllGlobals();
});

describe('Phase 2 public shell', () => {
  it('clearly labels the isolated test environment on a secondary route', () => {
    vi.stubEnv('MODE', 'test');
    renderApp('/privacy');
    expect(screen.getByText('自动化测试环境 · 数据仅用于测试，非真实教学。')).toBeVisible();
  });

  it('does not show the test environment label in a production build', () => {
    vi.stubEnv('MODE', 'production');
    renderApp();
    expect(screen.queryByText(/自动化测试环境/)).not.toBeInTheDocument();
  });

  it('shows a real search entry without future fake controls', async () => {
    renderApp();
    expect(screen.getByRole('heading', { name: '告诉我，你要封哪里。' })).toBeVisible();
    expect(await screen.findByText('0 条可用路线')).toBeVisible();
    expect(screen.getByRole('textbox', { name: '搜索路线' })).toBeVisible();
    expect(screen.getByRole('button', { name: '立即查找' })).toBeVisible();
    expect(screen.getByRole('link', { name: '内容管理' })).toBeVisible();
    expect(screen.queryByText('录音')).not.toBeInTheDocument();
  });

  it('navigates through privacy and disclaimer routes', () => {
    renderApp();
    fireEvent.click(screen.getByRole('link', { name: '隐私' }));
    expect(screen.getByRole('heading', { name: '隐私说明' })).toBeVisible();
    expect(screen.getByText(/后台管理员凭据只在当前页面内存中保存/)).toBeVisible();
    expect(screen.getByText(/精确的本站目录查询不调用 AI/)).toBeVisible();
    expect(screen.getByText(/模糊中文表述.*阿里云百炼/)).toBeVisible();
    expect(screen.getByText(/首版不进行站外视频搜索/)).toBeVisible();
    expect(screen.getByText(/短音频.*请求结束后删除/)).toBeVisible();
  });

  it('renders disclaimer facts without future promises', () => {
    renderApp('/disclaimer');
    expect(screen.getByRole('heading', { name: '非官方声明' })).toBeVisible();
    expect(screen.getByText(/不读取游戏进程/)).toBeVisible();
    expect(screen.getByText(/与 Valve、Steam、Counter-Strike 2 无官方隶属/)).toBeVisible();
  });

  it('counts Unicode code points rather than UTF-16 units', async () => {
    renderApp();
    const input = screen.getByRole('textbox', { name: '搜索路线' });
    fireEvent.change(input, { target: { value: '😀' } });
    await waitFor(() => expect(screen.getByText('1/200')).toBeVisible());
  });
});
