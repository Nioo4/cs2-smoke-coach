import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import App from '../src/app/App';

const mapPayload = {
  map: { slug: 'mirage' as const, name_zh: '荒漠迷城', asset_url: '/assets/maps/mirage.svg', asset_status: 'placeholder' as const },
  published_lineup_count: 1,
  targets: [{ slug: 't-target', name_zh: 'VIP', map_x: 30, map_y: 40, origin_count: 1 }],
};

const notFoundPayload = {
  type: 'not_found' as const,
  conversation_id: 'voice-conversation',
  reason: 'NO_LOCAL_RESULT' as const,
  message: '暂无符合该位置要求的站内路线，可换个起点或稍后再试。',
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

describe('Phase 5 voice input', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.useRealTimers();
  });

  beforeEach(() => {
    Object.defineProperty(window, 'matchMedia', {
      configurable: true,
      value: () => ({ matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn() }),
    });
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.endsWith('/maps/mirage')) return response(mapPayload);
      if (url.endsWith('/targets/t-target/lineups')) {
        return response({ target: mapPayload.targets[0], origins: [] });
      }
      if (url.endsWith('/transcriptions')) return response({ text: '我在 Mirage 的 T 出生点，想封 VIP 烟', duration_seconds: 1.25 });
      if (url.endsWith('/search')) return response(notFoundPayload);
      return response({});
    }));
  });

  it('records, uploads, fills the same input, and searches with voice mode', async () => {
    const tracks = [{ stop: vi.fn() }];
    const stream = { getTracks: () => tracks };
    const getUserMedia = vi.fn(async () => stream);
    Object.defineProperty(navigator, 'mediaDevices', { configurable: true, value: { getUserMedia } });
    class FakeRecorder {
      static isTypeSupported = vi.fn(() => true);
      ondataavailable: ((event: { data: Blob }) => void) | null = null;
      onstop: (() => void) | null = null;
      start = vi.fn();
      stop = vi.fn(() => {
        this.ondataavailable?.({ data: new Blob(['audio'], { type: 'audio/webm' }) });
        this.onstop?.();
      });
      constructor(...args: unknown[]) { void args; }
    }
    vi.stubGlobal('MediaRecorder', FakeRecorder);

    renderApp();
    const microphone = await screen.findByRole('button', { name: '语音输入' });
    fireEvent.click(microphone);
    expect(await screen.findByRole('button', { name: '停止录音' })).toBeVisible();
    fireEvent.click(screen.getByRole('button', { name: '停止录音' }));

    await waitFor(() => expect(screen.getByRole('textbox', { name: '搜索路线' })).toHaveValue('我在 Mirage 的 T 出生点，想封 VIP 烟'));
    const calls = vi.mocked(fetch).mock.calls.filter(([input]) => String(input).endsWith('/search'));
    expect(calls).toHaveLength(1);
    expect(JSON.parse(String(calls[0]?.[1]?.body))).toMatchObject({ input_mode: 'voice' });
    expect(tracks[0].stop).toHaveBeenCalledOnce();
  });

  it('does not request the microphone again after permission denial', async () => {
    const getUserMedia = vi.fn(async () => {
      throw new DOMException('denied', 'NotAllowedError');
    });
    Object.defineProperty(navigator, 'mediaDevices', { configurable: true, value: { getUserMedia } });
    class FakeRecorder {
      static isTypeSupported = vi.fn(() => true);
    }
    vi.stubGlobal('MediaRecorder', FakeRecorder);

    renderApp();
    const microphone = await screen.findByRole('button', { name: '语音输入' });
    fireEvent.click(microphone);
    expect(await screen.findByText('麦克风权限被拒绝，请改用文字。')).toBeVisible();
    expect(microphone).toBeDisabled();
    expect(getUserMedia).toHaveBeenCalledOnce();
    fireEvent.click(microphone);
    expect(getUserMedia).toHaveBeenCalledOnce();
  });

  it('does not let a stale permission rejection overwrite a newer request', async () => {
    let rejectPermission: ((error: unknown) => void) | undefined;
    const getUserMedia = vi.fn(() => new Promise<MediaStream>((_resolve, reject) => {
      rejectPermission = reject;
    }));
    Object.defineProperty(navigator, 'mediaDevices', { configurable: true, value: { getUserMedia } });
    class FakeRecorder {
      static isTypeSupported = vi.fn(() => true);
    }
    vi.stubGlobal('MediaRecorder', FakeRecorder);

    renderApp();
    const microphone = await screen.findByRole('button', { name: '语音输入' });
    fireEvent.click(microphone);
    const target = await screen.findByRole('button', { name: 'VIP，1 个可用起点' });
    fireEvent.click(target);
    await waitFor(() => expect(target).toBeDisabled());
    rejectPermission?.(new DOMException('denied', 'NotAllowedError'));
    await act(async () => undefined);
    expect(screen.queryByText('麦克风权限被拒绝，请改用文字。')).not.toBeInTheDocument();
    expect(screen.queryByText('无法使用麦克风，请改用文字。')).not.toBeInTheDocument();
  });

  it.each([
    ['503', 503, 'ASR_UNAVAILABLE', '语音识别服务暂不可用，请改用文字输入。'],
    ['429', 429, 'AUDIO_PROCESSING_BUSY', '语音处理繁忙，请稍后重试。'],
    ['504', 504, 'ASR_TIMEOUT', '语音识别服务响应超时，请稍后重试。'],
    ['network', null, null, '网络暂时不可用，请保留输入后重试。'],
  ] as const)('keeps text input after ASR failure (%s) and allows a normal search', async (
    _caseName,
    status,
    code,
    expectedMessage,
  ) => {
    const tracks = [{ stop: vi.fn() }];
    const stream = { getTracks: () => tracks };
    Object.defineProperty(navigator, 'mediaDevices', {
      configurable: true,
      value: { getUserMedia: vi.fn(async () => stream) },
    });
    let recorder: {
      ondataavailable: ((event: { data: Blob }) => void) | null;
      onstop: (() => void) | null;
      onerror: (() => void) | null;
      state: string;
      mimeType: string;
      start: () => void;
      stop: () => void;
    } | undefined;
    class FakeRecorder {
      static isTypeSupported = vi.fn(() => true);
      static capture = (value: FakeRecorder) => { recorder = value; };
      ondataavailable: ((event: { data: Blob }) => void) | null = null;
      onstop: (() => void) | null = null;
      onerror: (() => void) | null = null;
      state = 'inactive';
      mimeType = 'audio/webm';
      start = vi.fn(() => { this.state = 'recording'; });
      stop = vi.fn(() => {
        this.state = 'inactive';
        this.ondataavailable?.({ data: new Blob(['audio'], { type: this.mimeType }) });
        this.onstop?.();
      });
      constructor(...args: unknown[]) { void args; FakeRecorder.capture(this); }
    }
    vi.stubGlobal('MediaRecorder', FakeRecorder);
    vi.mocked(fetch).mockImplementation(async (input, init) => {
      const url = String(input);
      if (url.endsWith('/transcriptions')) {
        if (status === null) throw new TypeError('network down');
        return response({ error: { code, message: expectedMessage } }, status);
      }
      if (url.endsWith('/search')) return response(notFoundPayload);
      if (url.endsWith('/maps/mirage')) return response(mapPayload);
      void init;
      return response({});
    });

    renderApp();
    const input = await screen.findByRole('textbox', { name: '搜索路线' });
    fireEvent.change(input, { target: { value: '保留这段文字' } });
    fireEvent.click(screen.getByRole('button', { name: '语音输入' }));
    fireEvent.click(await screen.findByRole('button', { name: '停止录音' }));
    await screen.findByText(expectedMessage);
    expect(input).toHaveValue('保留这段文字');
    fireEvent.change(input, { target: { value: '文字搜索' } });
    fireEvent.click(screen.getByRole('button', { name: '立即查找' }));
    await waitFor(() => {
      expect(vi.mocked(fetch).mock.calls.filter(([value]) => String(value).endsWith('/search'))).toHaveLength(1);
    });
    expect(recorder?.ondataavailable).toBeNull();
    expect(recorder?.onstop).toBeNull();
    expect(recorder?.onerror).toBeNull();
    expect(tracks[0].stop).toHaveBeenCalled();
  });

  it('automatically stops at 15 seconds through an independent hard timeout', async () => {
    vi.useFakeTimers();
    const tracks = [{ stop: vi.fn() }];
    const stream = { getTracks: () => tracks };
    Object.defineProperty(navigator, 'mediaDevices', {
      configurable: true,
      value: { getUserMedia: vi.fn(async () => stream) },
    });
    const recorder = {
      ondataavailable: null as ((event: { data: Blob }) => void) | null,
      onstop: null as (() => void) | null,
      onerror: null as (() => void) | null,
      state: 'inactive',
      mimeType: 'audio/webm',
      start: vi.fn(function(this: typeof recorder) { this.state = 'recording'; }),
      stop: vi.fn(function(this: typeof recorder) {
        this.state = 'inactive';
        this.ondataavailable?.({ data: new Blob(['audio'], { type: this.mimeType }) });
        this.onstop?.();
      }),
    };
    class FakeRecorder {
      static isTypeSupported = vi.fn(() => true);
      constructor(...args: unknown[]) { void args; return recorder as unknown as FakeRecorder; }
    }
    vi.stubGlobal('MediaRecorder', FakeRecorder);

    renderApp();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    const microphone = screen.getByRole('button', { name: '语音输入' });
    fireEvent.click(microphone);
    await act(async () => undefined);
    expect(screen.getByRole('button', { name: '停止录音' })).toBeVisible();
    expect(recorder.stop).not.toHaveBeenCalled();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(14_999);
    });
    expect(recorder.stop).not.toHaveBeenCalled();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1);
    });
    expect(recorder.stop).toHaveBeenCalledOnce();
    expect(recorder.ondataavailable).toBeNull();
    expect(recorder.onstop).toBeNull();
    expect(recorder.onerror).toBeNull();
  });

  it('aborts late transcription when a new text request starts', async () => {
    const tracks = [{ stop: vi.fn() }];
    const stream = { getTracks: () => tracks };
    Object.defineProperty(navigator, 'mediaDevices', {
      configurable: true,
      value: { getUserMedia: vi.fn(async () => stream) },
    });
    let transcriptionSignal: AbortSignal | undefined;
    let resolveTranscription: ((value: Response) => void) | undefined;
    class FakeRecorder {
      static isTypeSupported = vi.fn(() => true);
      ondataavailable: ((event: { data: Blob }) => void) | null = null;
      onstop: (() => void) | null = null;
      onerror: (() => void) | null = null;
      state = 'inactive';
      mimeType = 'audio/webm';
      start = vi.fn(() => { this.state = 'recording'; });
      stop = vi.fn(() => {
        this.state = 'inactive';
        this.ondataavailable?.({ data: new Blob(['audio'], { type: this.mimeType }) });
        this.onstop?.();
      });
    }
    vi.stubGlobal('MediaRecorder', FakeRecorder);
    vi.mocked(fetch).mockImplementation(async (input, init) => {
      const url = String(input);
      if (url.endsWith('/maps/mirage')) return response(mapPayload);
      if (url.endsWith('/transcriptions')) {
        transcriptionSignal = (init as RequestInit | undefined)?.signal ?? undefined;
        return new Promise<Response>((resolve) => { resolveTranscription = resolve; });
      }
      if (url.endsWith('/search')) return response(notFoundPayload);
      return response({});
    });

    renderApp();
    fireEvent.click(await screen.findByRole('button', { name: '语音输入' }));
    fireEvent.click(await screen.findByRole('button', { name: '停止录音' }));
    await waitFor(() => expect(transcriptionSignal).toBeDefined());
    const input = screen.getByRole('textbox', { name: '搜索路线' });
    fireEvent.change(input, { target: { value: '文字查询' } });
    fireEvent.click(screen.getByRole('button', { name: '立即查找' }));
    await waitFor(() => expect(transcriptionSignal?.aborted).toBe(true));
    resolveTranscription?.(response({ text: '晚到的语音', duration_seconds: 1 }));
    await waitFor(() => expect(input).toHaveValue('文字查询'));
  });

  it('clears recorder handlers and tracks on recorder error and unmount', async () => {
    const tracks = [{ stop: vi.fn() }];
    const stream = { getTracks: () => tracks };
    Object.defineProperty(navigator, 'mediaDevices', {
      configurable: true,
      value: { getUserMedia: vi.fn(async () => stream) },
    });
    let recorder: {
      ondataavailable: ((event: { data: Blob }) => void) | null;
      onstop: (() => void) | null;
      onerror: (() => void) | null;
      state: string;
      triggerError: () => void;
    } | undefined;
    class FakeRecorder {
      static isTypeSupported = vi.fn(() => true);
      static capture = (value: FakeRecorder) => { recorder = value; };
      ondataavailable: ((event: { data: Blob }) => void) | null = null;
      onstop: (() => void) | null = null;
      onerror: (() => void) | null = null;
      state = 'recording';
      start = vi.fn(() => { this.state = 'recording'; });
      stop = vi.fn();
      triggerError = () => this.onerror?.();
      constructor(...args: unknown[]) { void args; FakeRecorder.capture(this); }
    }
    vi.stubGlobal('MediaRecorder', FakeRecorder);
    const view = renderApp();
    fireEvent.click(await screen.findByRole('button', { name: '语音输入' }));
    await screen.findByRole('button', { name: '停止录音' });
    recorder?.triggerError();
    await screen.findByText('录音失败，请改用文字。');
    expect(recorder?.ondataavailable).toBeNull();
    expect(recorder?.onstop).toBeNull();
    expect(recorder?.onerror).toBeNull();
    expect(tracks[0].stop).toHaveBeenCalled();

    fireEvent.click(screen.getByRole('button', { name: '语音输入' }));
    await screen.findByRole('button', { name: '停止录音' });
    view.unmount();
    expect(tracks[0].stop).toHaveBeenCalled();
    expect(recorder?.ondataavailable).toBeNull();
    expect(recorder?.onstop).toBeNull();
    expect(recorder?.onerror).toBeNull();
  });

  it('checks recording MIME support in order and constructs the first supported type', async () => {
    const tracks = [{ stop: vi.fn() }];
    const stream = { getTracks: () => tracks };
    Object.defineProperty(navigator, 'mediaDevices', {
      configurable: true,
      value: { getUserMedia: vi.fn(async () => stream) },
    });
    const mimeCalls: string[] = [];
    let constructedMime = '';
    class OrderedRecorder {
      static isTypeSupported = vi.fn((mime: string) => {
        mimeCalls.push(mime);
        return mime === 'audio/ogg;codecs=opus';
      });
      ondataavailable: ((event: { data: Blob }) => void) | null = null;
      onstop: (() => void) | null = null;
      onerror: (() => void) | null = null;
      state = 'inactive';
      mimeType = '';
      start = vi.fn(() => { this.state = 'recording'; });
      stop = vi.fn(() => {
        this.state = 'inactive';
        this.ondataavailable?.({ data: new Blob(['audio'], { type: this.mimeType }) });
        this.onstop?.();
      });
      constructor(...args: unknown[]) {
        const options = args[1] as { mimeType?: string } | undefined;
        this.mimeType = options?.mimeType ?? '';
        constructedMime = this.mimeType;
      }
    }
    vi.stubGlobal('MediaRecorder', OrderedRecorder);

    renderApp();
    const microphone = await screen.findByRole('button', { name: '语音输入' });
    mimeCalls.length = 0;
    fireEvent.click(microphone);
    await screen.findByRole('button', { name: '停止录音' });

    expect(mimeCalls.slice(0, 2)).toEqual([
      'audio/webm;codecs=opus',
      'audio/ogg;codecs=opus',
    ]);
    expect(constructedMime).toBe('audio/ogg;codecs=opus');
    fireEvent.click(screen.getByRole('button', { name: '停止录音' }));
  });

  it('disables voice input when the browser has no supported recording MIME', async () => {
    Object.defineProperty(navigator, 'mediaDevices', {
      configurable: true,
      value: { getUserMedia: vi.fn() },
    });
    class UnsupportedRecorder {
      static isTypeSupported = vi.fn(() => false);
    }
    vi.stubGlobal('MediaRecorder', UnsupportedRecorder);

    renderApp();
    const microphone = await screen.findByRole('button', { name: '语音输入' });
    expect(microphone).toBeDisabled();
    expect(microphone).toHaveAttribute('title', '当前浏览器不支持录音，请改用文字。');
  });
});
