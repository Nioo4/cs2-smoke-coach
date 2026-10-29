import { useEffect, useRef, useState, type RefObject } from 'react';

import type { LineupDetail, Notice } from '../types/api';
import { NoticeList } from './ErrorNotice';

/* These two tiny media operations are intentionally exported for the sibling selection panel. */
/* eslint-disable react-refresh/only-export-components */

export function durationLabel(durationMs: number): string {
  return `${(durationMs / 1000).toFixed(1)} 秒`;
}

export function releaseVideo(video: HTMLVideoElement | null): void {
  if (!video) return;
  video.pause();
  video.removeAttribute('src');
  try {
    video.load();
  } catch {
    // A detached test video may not implement load(). The source is still removed.
  }
}

export default function VideoResultPanel({
  detail,
  notices,
  showBack,
  loading,
  videoRef,
  failedLineupIds,
  onMediaFailed,
  onBack,
  onOpenOther,
  onQuickSwitch,
}: {
  detail: LineupDetail;
  notices: Notice[];
  showBack: boolean;
  loading: boolean;
  videoRef: RefObject<HTMLVideoElement | null>;
  failedLineupIds: Set<string>;
  onMediaFailed: (lineupId: string) => void;
  onBack: () => void;
  onOpenOther: (id: string) => void;
  onQuickSwitch: () => void;
}) {
  const [mediaFailed, setMediaFailed] = useState(false);
  const [videoKey, setVideoKey] = useState(0);
  const localVideo = useRef<HTMLVideoElement | null>(null);
  useEffect(() => {
    const node = localVideo.current;
    return () => releaseVideo(node);
  }, [detail.id, videoKey, videoRef]);
  const checked = detail.check_info.last_checked_at?.slice(0, 10);
  return (
    <div className="drawer-body detail-body">
      <NoticeList notices={notices} />
      {showBack && <button className="back-to-list" type="button" onClick={onBack}>← 返回起点列表</button>}
      <div className="route-relation" aria-label={`${detail.origin.name_zh} 到 ${detail.target.name_zh}`}>
        <div className="relation-point origin-point"><span>ORIGIN</span><strong>{detail.origin.name_zh}</strong></div>
        <span className="relation-line" aria-hidden="true">→</span>
        <div className="relation-point target-point"><span>TARGET</span><strong>{detail.target.name_zh}</strong></div>
      </div>
      <div className="video-frame">
        <video
          key={videoKey}
          ref={(node) => {
            localVideo.current = node;
            videoRef.current = node;
          }}
          className="route-video"
          controls
          autoPlay={false}
          preload="metadata"
          poster={detail.primary_video.poster_url}
          src={detail.primary_video.url}
          aria-label={`${detail.origin.name_zh} 到 ${detail.target.name_zh} 教学视频`}
          onError={() => {
            releaseVideo(localVideo.current);
            setMediaFailed(true);
            onMediaFailed(detail.id);
          }}
        />
        {mediaFailed && (
          <div className="video-error" role="alert">
            <strong>教学视频暂不可用</strong>
            <div className="video-error-actions"><button className="text-button" type="button" onClick={() => {
              setMediaFailed(false);
              setVideoKey((value) => value + 1);
            }}>重试</button>{detail.other_lineups.some((lineup) => !failedLineupIds.has(lineup.id)) && <button className="text-button" type="button" disabled={loading} onClick={onQuickSwitch}>一键换路线</button>}</div>
          </div>
        )}
      </div>
      <p className="video-caption">点击原生播放控件开始 · {durationLabel(detail.primary_video.duration_ms)}</p>
      <div className="steps-list">
        <Step label="站位" value={detail.steps.setup} />
        <Step label="瞄点" value={detail.steps.aim} />
        <Step label="投掷" value={detail.steps.throw} />
        <Step label="结果" value={detail.steps.result} />
      </div>
      <dl className="route-facts">
        <div><dt>来源</dt><dd>本站人工整理</dd></div>
        {checked && <div><dt>最后检查</dt><dd>{checked}</dd></div>}
        {detail.check_info.game_version_note && <div><dt>版本说明</dt><dd>{detail.check_info.game_version_note}</dd></div>}
      </dl>
      {detail.other_lineups.length > 0 && (
        <details className="other-routes">
          <summary>其他可用路线（{detail.other_lineups.length}）</summary>
          <div>
            {detail.other_lineups.map((lineup) => (
              <button className="other-route-button" type="button" key={lineup.id} disabled={loading} onClick={() => onOpenOther(lineup.id)}>
                <span>{lineup.variant_hint} · {durationLabel(lineup.primary_video.duration_ms)}</span>
                <span>打开 →</span>
              </button>
            ))}
          </div>
        </details>
      )}
    </div>
  );
}

function Step({ label, value }: { label: string; value: string }) {
  return (
    <div className="step-row">
      <span className="step-label">{label}</span>
      <p>{value || '—'}</p>
    </div>
  );
}
