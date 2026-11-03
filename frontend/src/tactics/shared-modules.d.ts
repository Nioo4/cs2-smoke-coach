declare module '*stage-playback.mjs' {
  export function groupStageActions(stage: import('./editorTypes').TacticStage): { id: string; actions: import('./editorTypes').TacticAction[] }[];
  export function stagePlaybackFrame(stage: import('./editorTypes').TacticStage, elapsed: number, duration?: number): import('./editorTypes').PlaybackFrame;
  export function canPausePlayback(play: { done: boolean; waiting: boolean } | null): boolean;
  export function advancePlaybackClock(play: { paused: boolean; elapsed: number; startedAt: number }, now: number): number;
}
declare module '*stage-board-interactions.mjs' {
  export function mapPointFromRect(x: number, y: number, rect: { left: number; top: number; width: number; height: number }): import('./editorTypes').Point;
}
declare module '*text-editing.mjs' {
  export function captureComposition(text: import('yjs').Text): object;
  export function applyTextDiff(doc: import('yjs').Doc, text: import('yjs').Text, desired: string): void;
  export function applyCompositionEdit(doc: import('yjs').Doc, text: import('yjs').Text, composition: object, desired: string, start?: number, end?: number): { ok: boolean; value?: string; selection?: { start: import('yjs').RelativePosition; end: import('yjs').RelativePosition } };
}
