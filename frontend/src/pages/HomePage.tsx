import { useCallback, useEffect, useRef, useState, type FormEvent, type KeyboardEvent } from 'react';
import { Link } from 'react-router-dom';

import { ApiError, countCodePoints, publicApi } from '../api/client';
import type {
  LineupDetail,
  MapResponse,
  SearchClarification,
  SearchNotFound,
  SearchOutOfScope,
  SearchResponse,
  Notice,
  RouteChoice,
} from '../types/api';
import ClarificationCard from '../components/ClarificationCard';
import ErrorNotice from '../components/ErrorNotice';
import MirageMap from '../components/MirageMap';
import SearchBar from '../components/SearchBar';
import SelectionPanel from '../components/SelectionPanel';
import VideoResultPanel, { releaseVideo } from '../components/VideoResultPanel';

type DrawerState =
  | { kind: 'route-list'; routes: RouteChoice[]; notices: Notice[]; title: string }
  | { kind: 'clarification'; result: SearchClarification; originalText: string }
  | { kind: 'notice'; result: SearchNotFound | SearchOutOfScope }
  | { kind: 'detail'; detail: LineupDetail; notices: Notice[]; fromList: boolean; renderKey: number };

type FocusTarget = HTMLElement | SVGElement;

function apiMessage(error: unknown): string {
  if (error instanceof ApiError) return error.message;
  return '网络暂时不可用，请保留输入后重试。';
}

function pauseVideo(video: HTMLVideoElement | null): void {
  video?.pause();
}

function clearRecorderHandlers(recorder: MediaRecorder | null): void {
  if (!recorder) return;
  recorder.ondataavailable = null;
  recorder.onstop = null;
  recorder.onerror = null;
}

const AUDIO_MIME_TYPES = ['audio/webm;codecs=opus', 'audio/ogg;codecs=opus', 'audio/webm'];
const MAX_RECORDING_SECONDS = 15;
const MAX_RECORDING_MS = MAX_RECORDING_SECONDS * 1000;

function supportedAudioMime(): string | null {
  if (typeof MediaRecorder === 'undefined') return null;
  if (typeof MediaRecorder.isTypeSupported !== 'function') return AUDIO_MIME_TYPES[0];
  return AUDIO_MIME_TYPES.find((mime) => MediaRecorder.isTypeSupported(mime)) ?? null;
}

export default function HomePage() {
  const [map, setMap] = useState<MapResponse | null>(null);
  const [mapLoading, setMapLoading] = useState(true);
  const [mapError, setMapError] = useState('');
  const [query, setQuery] = useState('');
  const [conversationId, setConversationId] = useState<string | null>(null);
  const [searchError, setSearchError] = useState('');
  const [loading, setLoading] = useState(false);
  const [drawer, setDrawer] = useState<DrawerState | null>(null);
  const [drawerError, setDrawerError] = useState('');
  const [selectedTarget, setSelectedTarget] = useState<string | null>(null);
  const [routeListNonce, setRouteListNonce] = useState(0);
  const [failedLineupIds, setFailedLineupIds] = useState<Set<string>>(() => new Set());
  const [voiceState, setVoiceState] = useState<'idle' | 'requesting_permission' | 'recording' | 'transcribing'>('idle');
  const [voiceSeconds, setVoiceSeconds] = useState(0);
  const [voiceError, setVoiceError] = useState('');
  const [microphoneDenied, setMicrophoneDenied] = useState(false);
  const [mobileDrawer, setMobileDrawer] = useState(() => (
    typeof window !== 'undefined'
      && typeof window.matchMedia === 'function'
      && window.matchMedia('(max-width: 1023px)').matches
  ));
  const requestSequence = useRef(0);
  const requestController = useRef<AbortController | null>(null);
  const transcriptionController = useRef<AbortController | null>(null);
  const voiceSequence = useRef(0);
  const recorderRef = useRef<MediaRecorder | null>(null);
  const streamRef = useRef<MediaStream | null>(null);
  const chunksRef = useRef<Blob[]>([]);
  const recordingTimer = useRef<number | null>(null);
  const recordingTimeout = useRef<number | null>(null);
  const trigger = useRef<FocusTarget | null>(null);
  const drawerRef = useRef<HTMLElement | null>(null);
  const titleRef = useRef<HTMLHeadingElement | null>(null);
  const videoRef = useRef<HTMLVideoElement | null>(null);
  const mapController = useRef<AbortController | null>(null);
  const previousRoutes = useRef<RouteChoice[] | null>(null);
  const previousListTitle = useRef('');
  const previousListNotices = useRef<Notice[]>([]);
  const previousListScrollTop = useRef(0);
  const detailRenderSequence = useRef(0);

  const loadMap = useCallback(() => {
    mapController.current?.abort();
    const controller = new AbortController();
    mapController.current = controller;
    setMapLoading(true);
    setMapError('');
    publicApi.map(controller.signal).then((value) => {
      if (controller.signal.aborted) return;
      setMap(value);
      setMapLoading(false);
    }).catch((error: unknown) => {
      if (error instanceof DOMException && error.name === 'AbortError') return;
      setMapLoading(false);
      setMapError(apiMessage(error));
    });
  }, []);

  useEffect(() => {
    const timer = window.setTimeout(loadMap, 0);
    return () => {
      window.clearTimeout(timer);
      mapController.current?.abort();
    };
  }, [loadMap]);

  useEffect(() => () => {
    requestController.current?.abort();
    transcriptionController.current?.abort();
    requestSequence.current += 1;
    voiceSequence.current += 1;
    if (recordingTimer.current !== null) {
      window.clearInterval(recordingTimer.current);
      recordingTimer.current = null;
    }
    if (recordingTimeout.current !== null) {
      window.clearTimeout(recordingTimeout.current);
      recordingTimeout.current = null;
    }
    const recorder = recorderRef.current;
    if (recorder) {
      clearRecorderHandlers(recorder);
      try {
        if (recorder.state !== 'inactive') recorder.stop();
      } catch {
        // The recorder may already be inactive during unmount cleanup.
      }
    }
    streamRef.current?.getTracks().forEach((track) => track.stop());
    recorderRef.current = null;
    streamRef.current = null;
    chunksRef.current = [];
    releaseVideo(videoRef.current);
  }, []);

  useEffect(() => {
    if (typeof window.matchMedia !== 'function') return;
    const media = window.matchMedia('(max-width: 1023px)');
    const update = () => setMobileDrawer(media.matches);
    update();
    media.addEventListener?.('change', update);
    return () => media.removeEventListener?.('change', update);
  }, []);

  useEffect(() => {
    const surface = document.getElementById('public-surface') as (HTMLElement & { inert?: boolean }) | null;
    if (!surface) return;
    surface.inert = Boolean(drawer && mobileDrawer);
    return () => {
      surface.inert = false;
    };
  }, [drawer, mobileDrawer]);

  useEffect(() => {
    if (!drawer) return;
    const frame = window.requestAnimationFrame(() => titleRef.current?.focus());
    return () => window.cancelAnimationFrame(frame);
  }, [drawer]);

  function clearRecordingTimer(): void {
    if (recordingTimer.current !== null) {
      window.clearInterval(recordingTimer.current);
      recordingTimer.current = null;
    }
    if (recordingTimeout.current !== null) {
      window.clearTimeout(recordingTimeout.current);
      recordingTimeout.current = null;
    }
  }

  function stopStreamTracks(): void {
    streamRef.current?.getTracks().forEach((track) => track.stop());
    streamRef.current = null;
  }

  function invalidateVoiceWork(): void {
    voiceSequence.current += 1;
    transcriptionController.current?.abort();
    transcriptionController.current = null;
    clearRecordingTimer();
    const recorder = recorderRef.current;
    if (recorder) {
      clearRecorderHandlers(recorder);
      try {
        if (recorder.state !== 'inactive') recorder.stop();
      } catch {
        // The browser may report an already stopped recorder during navigation.
      }
    }
    stopStreamTracks();
    recorderRef.current = null;
    chunksRef.current = [];
    setVoiceState('idle');
    setVoiceSeconds(0);
  }

  async function startVoice(): Promise<void> {
    if (microphoneDenied || voiceState !== 'idle') return;
    const mime = supportedAudioMime();
    if (!mime) {
      setVoiceError('当前浏览器不支持录音，请改用文字。');
      return;
    }
    requestController.current?.abort();
    requestController.current = null;
    requestSequence.current += 1;
    invalidateVoiceWork();
    setVoiceError('');
    setSearchError('');
    setLoading(false);
    setVoiceState('requesting_permission');
    const permissionSequence = voiceSequence.current;
    if (!navigator.mediaDevices?.getUserMedia) {
      setVoiceState('idle');
      setVoiceError('当前浏览器不支持录音，请改用文字。');
      return;
    }
    let stream: MediaStream;
    try {
      stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    } catch (error) {
      if (permissionSequence !== voiceSequence.current) return;
      if (error instanceof DOMException && error.name === 'NotAllowedError') {
        setMicrophoneDenied(true);
        setVoiceError('麦克风权限被拒绝，请改用文字。');
      } else {
        setVoiceError('无法使用麦克风，请改用文字。');
      }
      setVoiceState('idle');
      return;
    }
    if (permissionSequence !== voiceSequence.current || microphoneDenied) {
      stream.getTracks().forEach((track) => track.stop());
      return;
    }
    streamRef.current = stream;
    chunksRef.current = [];
    let recorder: MediaRecorder;
    try {
      recorder = new MediaRecorder(stream, { mimeType: mime });
    } catch {
      stopStreamTracks();
      setVoiceState('idle');
      setVoiceError('当前浏览器不支持录音，请改用文字。');
      return;
    }
    recorderRef.current = recorder;
    recorder.ondataavailable = (event) => {
      if (event.data.size > 0) chunksRef.current.push(event.data);
    };
    recorder.onstop = () => {
      clearRecorderHandlers(recorder);
      clearRecordingTimer();
      stopStreamTracks();
      if (recorderRef.current === recorder) recorderRef.current = null;
      const blob = chunksRef.current.length
        ? new Blob(chunksRef.current, { type: recorder.mimeType || 'audio/webm' })
        : null;
      chunksRef.current = [];
      if (!blob || blob.size === 0) {
        setVoiceState('idle');
        setVoiceSeconds(0);
        setVoiceError('没有录到音频，请重试。');
        return;
      }
      const sequence = ++voiceSequence.current;
      const controller = new AbortController();
      transcriptionController.current = controller;
      setVoiceState('transcribing');
      publicApi.transcription(blob, controller.signal).then((result) => {
        if (controller.signal.aborted || sequence !== voiceSequence.current) return;
        transcriptionController.current = null;
        setVoiceError('');
        setQuery(result.text);
        void runSearch(result.text, 'voice', result.text);
      }).catch((error: unknown) => {
        if (controller.signal.aborted || sequence !== voiceSequence.current) return;
        transcriptionController.current = null;
        setVoiceState('idle');
        setVoiceSeconds(0);
        if (error instanceof DOMException && error.name === 'AbortError') return;
        setVoiceError(apiMessage(error));
      });
    };
    recorder.onerror = () => {
      clearRecorderHandlers(recorder);
      clearRecordingTimer();
      stopStreamTracks();
      if (recorderRef.current === recorder) recorderRef.current = null;
      chunksRef.current = [];
      setVoiceState('idle');
      setVoiceSeconds(0);
      setVoiceError('录音失败，请改用文字。');
    };
    try {
      recorder.start();
    } catch {
      clearRecorderHandlers(recorder);
      stopStreamTracks();
      if (recorderRef.current === recorder) recorderRef.current = null;
      chunksRef.current = [];
      setVoiceState('idle');
      setVoiceError('录音失败，请改用文字。');
      return;
    }
    setVoiceState('recording');
    setVoiceSeconds(0);
    let elapsed = 0;
    recordingTimer.current = window.setInterval(() => {
      elapsed = Math.min(MAX_RECORDING_SECONDS, elapsed + 1);
      setVoiceSeconds(elapsed);
    }, 1000);
    recordingTimeout.current = window.setTimeout(() => {
      recordingTimeout.current = null;
      setVoiceSeconds(MAX_RECORDING_SECONDS);
      stopVoice();
    }, MAX_RECORDING_MS);
  }

  function stopVoice(): void {
    const recorder = recorderRef.current;
    if (!recorder) return;
    clearRecordingTimer();
    try {
      if (recorder.state !== 'inactive') recorder.stop();
    } catch {
      clearRecorderHandlers(recorder);
      if (recorderRef.current === recorder) recorderRef.current = null;
      chunksRef.current = [];
      stopStreamTracks();
      setVoiceState('idle');
      setVoiceSeconds(0);
      setVoiceError('录音失败，请改用文字。');
    }
    // Stop the permission-bearing tracks immediately; onstop still consumes the buffered chunks.
    stopStreamTracks();
  }

  function beginRequest(): { sequence: number; signal: AbortSignal } {
    invalidateVoiceWork();
    drawerRef.current?.querySelectorAll<HTMLVideoElement>('video').forEach(releaseVideo);
    requestController.current?.abort();
    const controller = new AbortController();
    requestController.current = controller;
    requestSequence.current += 1;
    return { sequence: requestSequence.current, signal: controller.signal };
  }

  function isCurrent(sequence: number): boolean {
    return sequence === requestSequence.current;
  }

  function rememberTrigger(element: FocusTarget): void {
    trigger.current = element;
  }

  function closeDrawer(): void {
    invalidateVoiceWork();
    requestSequence.current += 1;
    requestController.current?.abort();
    releaseVideo(videoRef.current);
    setDrawer(null);
    setDrawerError('');
    setLoading(false);
    window.requestAnimationFrame(() => trigger.current?.focus());
  }

  function closeDrawerToInput(): void {
    invalidateVoiceWork();
    requestSequence.current += 1;
    requestController.current?.abort();
    releaseVideo(videoRef.current);
    setDrawer(null);
    setDrawerError('');
    setLoading(false);
    window.requestAnimationFrame(() => {
      const input = document.getElementById('route-query') as HTMLInputElement | null;
      input?.focus();
      input?.select();
    });
  }

  useEffect(() => {
    if (!drawer) return;
    const closeOnEscape = (event: globalThis.KeyboardEvent) => {
      if (event.key === 'Escape') {
        event.preventDefault();
        closeDrawer();
      }
    };
    document.addEventListener('keydown', closeOnEscape);
    return () => document.removeEventListener('keydown', closeOnEscape);
    // closeDrawer is intentionally the current event handler; drawer controls listener lifetime.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [drawer]);

  async function openTarget(targetSlug: string, element?: FocusTarget): Promise<void> {
    if (element) rememberTrigger(element);
    setConversationId(null);
    setSelectedTarget(targetSlug);
    setFailedLineupIds(new Set());
    pauseVideo(videoRef.current);
    setDrawerError('');
    const { sequence, signal } = beginRequest();
    setLoading(true);
    setSearchError('');
    try {
      const choices = await publicApi.targetLineups(targetSlug, signal);
      if (!isCurrent(sequence)) return;
      const routes = choices.origins.map(({ origin, lineup }) => ({
        target: choices.target,
        origin,
        lineup,
      }));
      previousRoutes.current = routes;
      previousListTitle.current = `${choices.target.name_zh} · 可用起点`;
      previousListNotices.current = [];
      setRouteListNonce((value) => value + 1);
      setDrawer({
        kind: 'route-list',
        routes,
        notices: [],
        title: previousListTitle.current,
      });
    } catch (error) {
      if (error instanceof DOMException && error.name === 'AbortError') return;
      if (!isCurrent(sequence)) return;
      setSearchError(apiMessage(error));
      setDrawerError(apiMessage(error));
    } finally {
      if (isCurrent(sequence)) setLoading(false);
    }
  }

  async function openDetail(lineupId: string): Promise<void> {
    const fromList = drawer?.kind === 'route-list' || (
      drawer?.kind === 'detail' && drawer.fromList
    );
    const listNotices = drawer?.kind === 'route-list'
      ? drawer.notices
      : drawer?.kind === 'detail' && drawer.fromList
        ? drawer.notices
        : [];
    if (drawer?.kind === 'route-list') {
      previousRoutes.current = drawer.routes;
      previousListTitle.current = drawer.title;
      previousListNotices.current = listNotices;
      previousListScrollTop.current = drawerRef.current?.querySelector('.drawer-body')?.scrollTop ?? 0;
    }
    pauseVideo(videoRef.current);
    setDrawerError('');
    const { sequence, signal } = beginRequest();
    setLoading(true);
    try {
      const detail = await publicApi.detail(lineupId, signal);
      if (!isCurrent(sequence)) return;
      setDrawer({
        kind: 'detail',
        detail,
        notices: listNotices,
        fromList,
        renderKey: ++detailRenderSequence.current,
      });
    } catch (error) {
      if (error instanceof DOMException && error.name === 'AbortError') return;
      if (!isCurrent(sequence)) return;
      setSearchError(apiMessage(error));
      setDrawerError(apiMessage(error));
    } finally {
      if (isCurrent(sequence)) setLoading(false);
    }
  }

  function showSearchResponse(response: SearchResponse, originalText: string): void {
    setConversationId(response.conversation_id);
    if (response.type === 'match') {
      setSelectedTarget(response.lineup.target.slug);
      setDrawer({
        kind: 'detail',
        detail: response.lineup,
        notices: response.notices,
        fromList: false,
        renderKey: ++detailRenderSequence.current,
      });
    } else if (response.type === 'route_matches') {
      setSelectedTarget(response.intent.target_slug);
      const title = response.intent.target_slug
        ? `${response.routes[0]?.target.name_zh ?? '目标'} · 可用起点`
        : response.intent.origin_slug
          ? `从${response.routes[0]?.origin.name_zh ?? '起点'}可到达的目标`
          : '全部已收录路线';
      previousRoutes.current = response.routes;
      previousListTitle.current = title;
      previousListNotices.current = response.notices;
      setRouteListNonce((value) => value + 1);
      setDrawer({ kind: 'route-list', routes: response.routes, notices: response.notices, title });
    } else if (response.type === 'clarification') {
      setSelectedTarget(null);
      setDrawer({ kind: 'clarification', result: response, originalText });
    } else {
      setSelectedTarget(null);
      setDrawer({ kind: 'notice', result: response });
    }
  }

  async function runSearch(text: string, inputMode: 'text' | 'voice', originalText = text): Promise<void> {
    const normalized = text.trim();
    const length = countCodePoints(normalized);
    if (length < 1 || length > 200) {
      setSearchError('请输入 1–200 个字符。');
      return;
    }
    setFailedLineupIds(new Set());
    setVoiceError('');
    const { sequence, signal } = beginRequest();
    pauseVideo(videoRef.current);
    setDrawerError('');
    setLoading(true);
    setSearchError('');
    try {
      const response = await publicApi.search(
        { kind: 'query', text: normalized, conversation_id: conversationId, input_mode: inputMode },
        signal,
      );
      if (isCurrent(sequence)) showSearchResponse(response, originalText);
    } catch (error) {
      if (error instanceof DOMException && error.name === 'AbortError') return;
      if (isCurrent(sequence)) setSearchError(apiMessage(error));
    } finally {
      if (isCurrent(sequence)) setLoading(false);
    }
  }

  async function submitQuery(event: FormEvent<HTMLFormElement>): Promise<void> {
    event.preventDefault();
    const active = document.activeElement;
    if (active instanceof HTMLElement) rememberTrigger(active);
    await runSearch(query, 'text', query);
  }

  async function selectClarification(value: string): Promise<void> {
    if (!drawer || drawer.kind !== 'clarification') return;
    const { result, originalText } = drawer;
    const { sequence, signal } = beginRequest();
    setLoading(true);
    setSearchError('');
    setDrawerError('');
    try {
      const response = await publicApi.search(
        {
          kind: 'selection',
          conversation_id: result.conversation_id,
          selection: { field: 'target', value },
        },
        signal,
      );
      if (isCurrent(sequence)) showSearchResponse(response, originalText);
    } catch (error) {
      if (error instanceof DOMException && error.name === 'AbortError') return;
      if (isCurrent(sequence)) {
        setSearchError(apiMessage(error));
        setDrawerError(apiMessage(error));
      }
    } finally {
      if (isCurrent(sequence)) setLoading(false);
    }
  }

  function handleDrawerKeyDown(event: KeyboardEvent<HTMLElement>): void {
    if (event.key === 'Escape') {
      event.preventDefault();
      closeDrawer();
      return;
    }
    if (event.key !== 'Tab' || !mobileDrawer || !drawerRef.current) return;
    const focusable = Array.from(
      drawerRef.current.querySelectorAll<HTMLElement>(
        'h2[tabindex="-1"], button:not([disabled]), a[href], input:not([disabled]), textarea:not([disabled]), select:not([disabled]), video',
      ),
    );
    if (!focusable.length) return;
    const first = focusable[0];
    const last = focusable[focusable.length - 1];
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  }

  function handleMediaFailed(lineupId: string): void {
    setFailedLineupIds((old) => {
      const next = new Set(old);
      next.add(lineupId);
      return next;
    });
  }

  function quickSwitch(detail: LineupDetail): void {
    const next = detail.other_lineups.find((lineup) => !failedLineupIds.has(lineup.id));
    if (next) void openDetail(next.id);
  }

  const routeCount = map?.published_lineup_count;
  const routeCountLabel = mapLoading ? '读取中…' : mapError ? '暂不可用' : `${routeCount ?? 0} 条可用路线`;
  const voiceSupported = typeof navigator !== 'undefined'
    && Boolean(navigator.mediaDevices?.getUserMedia)
    && Boolean(supportedAudioMime());
  const voiceDisabled = !voiceSupported || microphoneDenied || loading
    || voiceState === 'requesting_permission' || voiceState === 'transcribing';

  return (
    <div className={`site-frame${drawer && !mobileDrawer ? ' has-drawer' : ''}`}>
      <div id="public-surface" className="public-surface">
      <header className="topbar">
        <Link className="brand" to="/" aria-label="SmokeLine 首页">
          <span className="brand-mark" aria-hidden="true">SL</span>
          <span>SmokeLine</span>
        </Link>
        <span className="scope-pill">MIRAGE · 烟雾</span>
        <span className="route-count"><strong>{routeCountLabel}</strong></span>
        <Link className="topbar-link" to="/admin">内容管理</Link>
        <Link className="topbar-link" to="/disclaimer">非官方声明</Link>
      </header>

      <main id="public-main" className="page-shell home-page">
        <section className="hero" aria-labelledby="home-title">
          <p className="eyebrow">MIRAGE / TRAINING MANUAL</p>
          <h1 id="home-title">告诉我，你要封哪里。</h1>
          <p className="hero-copy">
            输入 Mirage 烟雾目标；只说目标也可以，若补充起点就会直接定位路线。视频会停在封面，是否播放由你决定。
          </p>
          <SearchBar
            query={query}
            onQueryChange={setQuery}
            onSubmit={submitQuery}
            loading={loading}
            voiceState={voiceState}
            voiceSeconds={voiceSeconds}
            voiceSupported={voiceSupported}
            voiceDisabled={voiceDisabled}
            onVoiceStart={() => void startVoice()}
            onVoiceStop={stopVoice}
            searchError={searchError}
            voiceError={voiceError}
            mapLoading={mapLoading}
            mapError={mapError}
            routeCount={routeCount}
            onMapRetry={loadMap}
          />
        </section>

        <section className="manual-section route-section" aria-labelledby="routes-title">
          <div className="section-heading">
            <div>
              <p className="eyebrow">01 · AVAILABLE TARGETS</p>
              <h2 id="routes-title">从目标开始。</h2>
            </div>
            <p className="section-note">点击目标查看每个可用起点</p>
          </div>
          {mapLoading ? (
            <div className="empty-card">正在读取公开目标…</div>
          ) : mapError ? (
            <div className="empty-card">路线目录暂时不可用，请使用上方“重新读取”。</div>
          ) : map?.targets.length ? (
            <div className="target-grid">
              {map.targets.map((target) => (
                <button
                  className={`target-card${selectedTarget === target.slug ? ' is-selected' : ''}`}
                  type="button"
                  key={target.slug}
                  aria-current={selectedTarget === target.slug ? 'true' : undefined}
                  aria-label={`${target.name_zh}，${target.origin_count} 个可用起点`}
                  disabled={loading}
                  onClick={(event) => void openTarget(target.slug, event.currentTarget)}
                >
                  <span className="target-shape" aria-hidden="true" />
                  <span className="target-card-name">{target.name_zh}</span>
                  <span className="target-card-meta">{target.origin_count} 个可用起点</span>
                </button>
              ))}
            </div>
          ) : (
            <div className="empty-card">还没有公开路线。管理员完成第一条真实教学路线后，这里会显示目标卡片。</div>
          )}
        </section>

        {map && !mapLoading && !mapError && map.map.asset_status === 'approved' && (
          <MirageMap
            map={map}
            selectedTarget={selectedTarget}
            loading={loading}
            onTarget={(slug, element) => void openTarget(slug, element)}
          />
        )}

        <aside className="scope-note" aria-label="当前范围说明">
          <span className="scope-note-label">SCOPE NOTE</span>
          <p>免费、匿名、只做 Mirage 烟雾教学；结果只来自本站目录，首版不搜索站外视频。</p>
        </aside>
      </main>

      <footer className="site-footer">
        <span>SmokeLine · Mirage smoke manual</span>
        <nav aria-label="页脚导航">
          <Link to="/privacy">隐私</Link>
          <Link to="/disclaimer">非官方声明</Link>
        </nav>
      </footer>
      </div>

      {drawer && (
        <section
          ref={drawerRef}
          className={`route-drawer${mobileDrawer ? ' drawer-mobile' : ' drawer-desktop'}`}
          role={mobileDrawer ? 'dialog' : 'region'}
          aria-modal={mobileDrawer ? 'true' : undefined}
          aria-labelledby="drawer-title"
          tabIndex={-1}
          onKeyDown={handleDrawerKeyDown}
        >
          <div className="drawer-header">
            <div>
              <p className="eyebrow">ROUTE NOTES</p>
              <h2 id="drawer-title" ref={titleRef} tabIndex={-1}>
                {drawer.kind === 'route-list' ? drawer.title :
                    drawer.kind === 'clarification' ? '选择目标位置' :
                    drawer.kind === 'detail' ? `${drawer.detail.origin.name_zh} → ${drawer.detail.target.name_zh}` : '检索结果'}
              </h2>
            </div>
            <button className="icon-button" type="button" aria-label="关闭详情" onClick={closeDrawer}>×</button>
          </div>
          {drawerError && <p className="inline-error drawer-error" role="alert">{drawerError}</p>}

          {drawer.kind === 'route-list' && (
            <SelectionPanel key={routeListNonce} routes={drawer.routes} notices={drawer.notices} loading={loading} onOpen={(id) => void openDetail(id)} />
          )}
          {drawer.kind === 'clarification' && (
            <ClarificationCard
              result={drawer.result}
              onSelect={(value) => void selectClarification(value)}
              loading={loading}
              onEdit={closeDrawerToInput}
            />
          )}
          {drawer.kind === 'notice' && <ErrorNotice result={drawer.result} onEdit={closeDrawerToInput} />}
          {drawer.kind === 'detail' && (
            <VideoResultPanel
              key={`${drawer.detail.id}:${drawer.renderKey}`}
              detail={drawer.detail}
              notices={drawer.notices}
              showBack={drawer.fromList}
              loading={loading}
              videoRef={videoRef}
              failedLineupIds={failedLineupIds}
              onMediaFailed={handleMediaFailed}
              onBack={() => {
                releaseVideo(videoRef.current);
                if (previousRoutes.current) {
                  setDrawer({ kind: 'route-list', routes: previousRoutes.current, notices: previousListNotices.current, title: previousListTitle.current });
                  window.requestAnimationFrame(() => {
                    const body = drawerRef.current?.querySelector('.drawer-body');
                    if (body) body.scrollTop = previousListScrollTop.current;
                  });
                } else closeDrawer();
              }}
              onOpenOther={(id) => void openDetail(id)}
              onQuickSwitch={() => quickSwitch(drawer.detail)}
            />
          )}
        </section>
      )}
    </div>
  );
}
