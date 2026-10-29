import { useEffect, useRef, useState } from 'react';

import type { Notice, RouteChoice } from '../types/api';
import { durationLabel, releaseVideo } from './VideoResultPanel';
import { NoticeList } from './ErrorNotice';

export default function SelectionPanel({ routes, notices, loading, onOpen }: { routes: RouteChoice[]; notices: Notice[]; loading: boolean; onOpen: (id: string) => void }) {
  const videos = useRef(new Map<string, HTMLVideoElement>());
  const [failedLineupIds, setFailedLineupIds] = useState<Set<string>>(() => new Set());
  const [retryVersions, setRetryVersions] = useState<Record<string, number>>({});

  const pauseOtherVideos = (current: HTMLVideoElement) => {
    for (const video of videos.current.values()) {
      if (video !== current) video.pause();
    }
  };

  useEffect(() => {
    const videoMap = videos.current;
    return () => {
      for (const video of videoMap.values()) releaseVideo(video);
      videoMap.clear();
    };
  }, []);

  return (
    <div className="drawer-body">
      <NoticeList notices={notices} />
      <p className="drawer-intro">每组只展示公开默认路线。视频默认暂停，请主动播放；“查看完整教学”会打开文字步骤与其他路线。</p>
      <div className="route-choice-list">
        {routes.map(({ target, origin, lineup }) => (
          <article className="origin-card route-choice-card" key={`${target.slug}:${origin.slug}:${lineup.id}`}>
            <div className="route-relation" aria-label={`${origin.name_zh} 到 ${target.name_zh}`}>
              <div className="relation-point origin-point"><span>ORIGIN</span><strong>{origin.name_zh}</strong></div>
              <span className="relation-line" aria-hidden="true">→</span>
              <div className="relation-point target-point"><span>TARGET</span><strong>{target.name_zh}</strong></div>
            </div>
            <video
              key={`${lineup.id}:${retryVersions[lineup.id] ?? 0}`}
              ref={(node) => {
                if (node) {
                  videos.current.set(lineup.id, node);
                  try { node.currentTime = 0; } catch { /* detached media */ }
                } else {
                  videos.current.delete(lineup.id);
                }
              }}
              className="route-choice-video"
              controls
              autoPlay={false}
              preload="none"
              poster={lineup.primary_video.poster_url}
              src={lineup.primary_video.url}
              aria-label={`${origin.name_zh} 到 ${target.name_zh} 的路线预览`}
              onLoadedMetadata={(event) => {
                try { event.currentTarget.currentTime = 0; } catch { /* media may be detached */ }
              }}
              onError={() => setFailedLineupIds((current) => new Set(current).add(lineup.id))}
              onPlayCapture={(event) => pauseOtherVideos(event.currentTarget)}
            />
            {failedLineupIds.has(lineup.id) && (
              <div className="route-choice-video-error" role="alert">
                <span>教学视频暂不可用</span>
                <button
                  className="button button-secondary"
                  type="button"
                  onClick={() => {
                    setFailedLineupIds((current) => {
                      const next = new Set(current);
                      next.delete(lineup.id);
                      return next;
                    });
                    setRetryVersions((current) => ({
                      ...current,
                      [lineup.id]: (current[lineup.id] ?? 0) + 1,
                    }));
                  }}
                >
                  重试
                </button>
              </div>
            )}
            <div className="route-choice-actions">
              <span>{lineup.variant_hint} · {durationLabel(lineup.primary_video.duration_ms)}</span>
              <button
                className="button button-secondary"
                type="button"
                aria-label={`${origin.name_zh} → ${target.name_zh}：查看完整教学`}
                disabled={loading}
                onClick={() => onOpen(lineup.id)}
              >
                查看完整教学
              </button>
            </div>
          </article>
        ))}
      </div>
    </div>
  );
}
