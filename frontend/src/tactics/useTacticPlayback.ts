import { useEffect, useRef, useState } from 'react';
import { advancePlaybackClock, canPausePlayback, groupStageActions, stagePlaybackFrame } from '../../../collaboration/src/stage-playback.mjs';
import type { PlaybackFrame, TacticProjection } from './editorTypes';

interface Playback {
  snapshot: TacticProjection; revision: number; epoch: string; mode: 'auto' | 'stage'; stageIndex: number;
  elapsed: number; startedAt: number; paused: boolean; done: boolean; waiting: boolean; frame: PlaybackFrame;
}
export function useTacticPlayback(confirmed: TacticProjection, revision: number, epoch: string, hasUnconfirmedWork: boolean) {
  const [play, setPlay] = useState<Playback | null>(null);
  const current = useRef<Playback | null>(null);
  const animating = Boolean(play && !play.paused && (!play.done || play.waiting));
  function publish(next: Playback | null) { current.current = next; setPlay(next ? { ...next } : null); }
  useEffect(() => {
    if (!animating) return;
    let frameId = 0;
    function tick(now: number) {
      const value = current.current;
      if (!value || value.paused) return;
      const stage = value.snapshot.structure.stages[value.stageIndex];
      advancePlaybackClock(value, now);
      const duration = groupStageActions(stage).length * 3000;
      value.frame = stagePlaybackFrame(stage, Math.min(duration, value.elapsed));
      value.done = value.elapsed >= duration;
      value.waiting = value.done && value.mode === 'auto' && value.stageIndex < value.snapshot.structure.stages.length - 1;
      if (value.waiting && value.elapsed >= duration + 1000) {
        value.stageIndex += 1; value.elapsed = 0; value.startedAt = now; value.done = false; value.waiting = false;
        value.frame = stagePlaybackFrame(value.snapshot.structure.stages[value.stageIndex], 0);
      }
      setPlay({ ...value });
      if (!value.done || value.waiting) frameId = requestAnimationFrame(tick);
    }
    frameId = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(frameId);
    // The loop reads the current playback from its ref; only lifecycle changes restart it.
  }, [animating]);
  function start(mode: 'auto' | 'stage', selectedStageId?: string) {
    if (hasUnconfirmedWork || !confirmed.structure.stages.length) return false;
    const snapshot = structuredClone(confirmed);
    const stageIndex = Math.max(0, snapshot.structure.stages.findIndex(stage => stage.id === selectedStageId));
    publish({ snapshot, revision, epoch, mode, stageIndex, elapsed: 0, startedAt: performance.now(), paused: false, done: false, waiting: false, frame: stagePlaybackFrame(snapshot.structure.stages[stageIndex], 0) });
    return true;
  }
  function navigate(direction: number) {
    const value = current.current; if (!value) return;
    const index = Math.max(0, Math.min(value.snapshot.structure.stages.length - 1, value.stageIndex + direction));
    if (index === value.stageIndex && direction !== 0) return;
    publish({ ...value, stageIndex: index, elapsed: 0, startedAt: performance.now(), paused: false, done: false, waiting: false, frame: stagePlaybackFrame(value.snapshot.structure.stages[index], 0) });
  }
  function togglePause() {
    const value = current.current; if (!canPausePlayback(value) || !value) return;
    if (!value.paused) advancePlaybackClock(value, performance.now());
    publish({ ...value, startedAt: performance.now(), paused: !value.paused, frame: stagePlaybackFrame(value.snapshot.structure.stages[value.stageIndex], value.elapsed) });
  }
  return { play, start, navigate, replay: () => navigate(0), togglePause, canPause: canPausePlayback(play), exit: () => publish(null), changed: Boolean(play && (play.revision !== revision || play.epoch !== epoch)) };
}
