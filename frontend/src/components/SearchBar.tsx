import type { FormEvent } from 'react';

import { countCodePoints } from '../api/client';

type VoiceState = 'idle' | 'requesting_permission' | 'recording' | 'transcribing';

interface SearchBarProps {
  query: string;
  onQueryChange: (value: string) => void;
  onSubmit: (event: FormEvent<HTMLFormElement>) => void;
  loading: boolean;
  voiceState: VoiceState;
  voiceSeconds: number;
  voiceSupported: boolean;
  voiceDisabled: boolean;
  onVoiceStart: () => void;
  onVoiceStop: () => void;
  searchError: string;
  voiceError: string;
  mapLoading: boolean;
  mapError: string;
  routeCount: number | undefined;
  onMapRetry: () => void;
}

function voiceTimerLabel(seconds: number): string {
  return `00:${String(Math.min(15, Math.max(0, seconds))).padStart(2, '0')}`;
}

export default function SearchBar({
  query,
  onQueryChange,
  onSubmit,
  loading,
  voiceState,
  voiceSeconds,
  voiceSupported,
  voiceDisabled,
  onVoiceStart,
  onVoiceStop,
  searchError,
  voiceError,
  mapLoading,
  mapError,
  routeCount,
  onMapRetry,
}: SearchBarProps) {
  return (
    <>
      <form className="search-form" onSubmit={onSubmit} noValidate>
        <label htmlFor="route-query">搜索路线</label>
        <div className="search-row">
          <input
            id="route-query"
            name="route-query"
            type="text"
            value={query}
            onChange={(event) => onQueryChange(event.target.value)}
            placeholder="例如：我在 Mirage 的 T 出生点，想封 VIP 烟"
            aria-describedby="query-help query-count"
            disabled={loading || voiceState === 'recording' || voiceState === 'requesting_permission'}
          />
          <button
            className="button button-secondary voice-button"
            type="button"
            aria-label={voiceState === 'recording' ? '停止录音' : '语音输入'}
            disabled={voiceState === 'recording' ? false : voiceDisabled}
            title={!voiceSupported ? '当前浏览器不支持录音，请改用文字。' : undefined}
            onClick={voiceState === 'recording' ? onVoiceStop : onVoiceStart}
          >
            {voiceState === 'recording' ? '停止录音' : voiceState === 'transcribing' ? '识别中…' : '语音输入'}
          </button>
          <button
            className="button button-primary"
            type="submit"
            disabled={loading || voiceState === 'recording' || voiceState === 'requesting_permission'}
          >
            {loading ? '查找中…' : '立即查找'}
          </button>
        </div>
        <div className="search-help">
          <span id="query-help">支持 1–200 个 Unicode 字符；只做 Mirage 烟雾路线检索。</span>
          <span id="query-count" aria-live="polite">{countCodePoints(query.trim())}/200</span>
        </div>
        {voiceState !== 'idle' && (
          <p className="voice-status" aria-live="polite">
            {voiceState === 'requesting_permission' && '正在请求麦克风权限…'}
            {voiceState === 'recording' && `录音中 ${voiceTimerLabel(voiceSeconds)} / 00:15`}
            {voiceState === 'transcribing' && '正在识别语音…'}
          </p>
        )}
        {!voiceSupported && <p className="voice-status">当前浏览器不支持录音，请改用文字。</p>}
      </form>
      {searchError && <p className="inline-error" role="alert">{searchError}</p>}
      {voiceError && <p className="inline-error" role="alert">{voiceError}</p>}
      <div className="status-card" role="status" aria-live="polite">
        <span className="status-marker" aria-hidden="true" />
        <div>
          <p className="status-title">{mapLoading ? '正在读取路线目录' : mapError ? '路线目录暂时不可用' : routeCount !== undefined && routeCount > 0 ? '路线资料已就绪' : '教学内容准备中'}</p>
          <p className="status-copy">
            {mapLoading ? '正在读取当前公开路线。' : mapError ? '请稍后重试，当前状态不代表没有路线。' : routeCount !== undefined && routeCount > 0 ? '结果只来自本站目录；模糊表述可能由受控意图服务映射到目录，站内无路线时会明确提示。' : '管理员发布路线后，它们会出现在这里。'}
          </p>
        </div>
        <span className="status-stamp">{mapLoading ? 'LOADING' : mapError ? 'RETRY' : routeCount !== undefined && routeCount > 0 ? 'LOCAL CURATED' : 'NO ROUTES YET'}</span>
      </div>
      {mapError && <p className="inline-error" role="alert">{mapError} <button className="text-button inline-retry" type="button" onClick={onMapRetry}>重新读取</button></p>}
    </>
  );
}
