import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { runInNewContext } from 'node:vm';
import { advancePlaybackClock, canPausePlayback, interpolatePath, stagePlaybackFrame } from '../src/stage-playback.mjs';
import { groupStageActions } from '../src/stage-playback.mjs';

test('actual observation renderer interpolates direction smoothly and takes the shortest turn', () => {
  const source = readFileSync(new URL('../src/stage-board.js', import.meta.url), 'utf8');
  const state = { playback: { frame: { activeActionIds: ['look'], progress: 0 } } };
  const context = { state, structuredClone };
  runInNewContext(source.slice(source.indexOf('function displayedObserveEnd('), source.indexOf('function renderDrawingControls(')), context);
  const action = { id: 'look', kind: 'observe', role_id: 'a', target: { x: 50, y: 70 } };
  const stage = { initial_positions: { a: { x: 50, y: 50 } }, actions: [action] };
  const start = stage.initial_positions.a;
  assert.equal(context.displayedObserveEnd(stage, action, start, action.target).x, 70);
  state.playback.frame.progress = 0.5;
  const halfway = context.displayedObserveEnd(stage, action, start, action.target);
  assert.ok(Math.abs(halfway.x - (50 + Math.sqrt(200))) < 1e-9);
  assert.ok(Math.abs(halfway.y - (50 + Math.sqrt(200))) < 1e-9);
  state.playback.frame.progress = 1;
  assert.equal(context.displayedObserveEnd(stage, action, start, action.target).y, 70);
  const prior = { id: 'prior', kind: 'observe', role_id: 'a', target: { x: 30, y: 51 } };
  stage.actions.unshift(prior); action.target = { x: 30, y: 49 };
  state.playback.frame.progress = 0.5;
  const wrap = context.displayedObserveEnd(stage, action, start, action.target);
  assert.ok(wrap.x < 30); assert.ok(Math.abs(wrap.y - 50) < 1e-9);
});

test('actual UI playback callbacks preserve background pause, hold, replay and navigation timing', () => {
  const source = readFileSync(new URL('../src/stage-board.js', import.meta.url), 'utf8');
  const stage = { id: 'one', initial_positions: { a: { x: 0, y: 0 } }, actions: [{ id: 'move', group_id: 'group', kind: 'move', role_id: 'a', path: [{ x: 0, y: 0 }, { x: 60, y: 0 }] }] };
  const state = { animationFrame: null, playback: { snapshot: { structure: { stages: [stage, { ...stage, id: 'two' }] } }, mode: 'auto', stageIndex: 0, elapsed: 0, startedAt: 0, paused: false, done: false } };
  let now = 1500;
  let visibility;
  const queued = new Map(); let nextId = 0;
  const document = { hidden: true, addEventListener: (_name, callback) => { visibility = callback; } };
  const context = { state, advancePlaybackClock, canPausePlayback, groupStageActions, stagePlaybackFrame, document,
    performance: { now: () => now }, requestAnimationFrame: (fn) => { queued.set(++nextId, fn); return nextId; }, cancelAnimationFrame: (id) => queued.delete(id),
    renderMap() {}, renderActionHighlights() {}, renderPlaybackControls() {}, updateSaveState() {}, render() {},
  };
  runInNewContext(source.slice(source.indexOf('function startAnimation()'), source.indexOf('function renderActionHighlights()')), context);
  runInNewContext(source.split('\n').find((line) => line.startsWith("document.addEventListener('visibilitychange'")), context);
  const tick = () => { queued.clear(); context.playbackTick(now); };
  tick(); assert.equal(state.playback.frame.positions.a.x, 30);
  visibility(); now = 10000;
  assert.equal(state.playback.paused, true); assert.equal(queued.size, 0);
  context.togglePlaybackPause(); now = 11500; tick();
  assert.equal(state.playback.waiting, true); assert.equal(state.playback.elapsed, 3000);
  now = 11900; visibility();
  now = 20000; context.togglePlaybackPause();
  now = 20599; tick(); assert.equal(state.playback.stageIndex, 0);
  now = 20600; tick(); assert.equal(state.playback.stageIndex, 1);
  assert.equal(state.playback.elapsed, 0);
  now = 23600; tick(); assert.equal(state.playback.done, true);
  context.togglePlaybackPause(); assert.equal(state.playback.paused, false);
  context.replayStage(); assert.equal(state.playback.frame.positions.a.x, 0); assert.equal(state.playback.elapsed, 0);
  context.navigatePlayback(-1); assert.equal(state.playback.stageIndex, 0); assert.equal(state.playback.elapsed, 0);
  state.playback.waiting = true; state.playback.paused = true; state.playback.done = true;
  state.selectedStageId = 'one';
  let selectStage;
  context.$ = () => ({ addEventListener: (_event, callback) => { selectStage = callback; } });
  const start = source.indexOf("$('#stage-list').addEventListener('click'");
  const end = source.indexOf("$('#stage-list').addEventListener('click'", start + 1);
  runInNewContext(source.slice(start, end), context);
  selectStage({ target: { closest: () => ({ dataset: { stageSelect: 'two' } }) } });
  assert.equal(state.playback.stageIndex, 1); assert.equal(state.playback.elapsed, 0);
  assert.equal(state.playback.paused, false); assert.equal(state.playback.waiting, false);
  assert.equal(state.playback.frame.positions.a.x, 0);
  assert.equal(queued.size, 1);
  context.exitPlayback(); assert.equal(state.playback, null); assert.equal(queued.size, 0);
});

test('pausing during the automatic inter-stage hold preserves the remaining hold and can resume', () => {
  const play = { elapsed: 0, startedAt: 100, paused: false, done: false, waiting: false };
  assert.equal(advancePlaybackClock(play, 3100), 3000);
  play.done = true; play.waiting = true;
  assert.equal(canPausePlayback(play), true);
  assert.equal(advancePlaybackClock(play, 3500), 3400);
  play.paused = true;
  assert.equal(advancePlaybackClock(play, 20000), 3400);
  play.paused = false; play.startedAt = 20000;
  assert.equal(advancePlaybackClock(play, 20599), 3999);
  assert.equal(advancePlaybackClock(play, 20600), 4000);
  play.waiting = false;
  assert.equal(canPausePlayback(play), false);
});

test('playback clock counts each frame once and pause/resume preserves the exact move position', () => {
  const play = { elapsed: 0, startedAt: 0, paused: false, done: false };
  assert.equal(advancePlaybackClock(play, 500), 500);
  assert.equal(advancePlaybackClock(play, 1500), 1500);
  const path = [{ x: 0, y: 0 }, { x: 60, y: 0 }];
  const position = interpolatePath(path, play.elapsed / 3000);
  play.paused = true;
  advancePlaybackClock(play, 10000);
  assert.deepEqual(interpolatePath(path, play.elapsed / 3000), position);
  play.paused = false; play.startedAt = 10000;
  assert.deepEqual(interpolatePath(path, advancePlaybackClock(play, 10500) / 3000), { x: 40, y: 0 });
});

test('route interpolation follows cumulative path distance rather than segment count', () => {
  const point = interpolatePath([{ x: 0, y: 0 }, { x: 80, y: 0 }, { x: 80, y: 20 }], 0.8);
  assert.deepEqual(point, { x: 80, y: 0 });
});

test('parallel action groups animate together and sequential groups hold the final state', () => {
  const stage = {
    initial_positions: { a: { x: 0, y: 0 }, b: { x: 10, y: 10 } },
    actions: [
      { id: 'a1', group_id: 'g1', kind: 'move', role_id: 'a', path: [{ x: 0, y: 0 }, { x: 60, y: 0 }] },
      { id: 'b1', group_id: 'g1', kind: 'move', role_id: 'b', path: [{ x: 10, y: 10 }, { x: 70, y: 10 }] },
      { id: 'a2', group_id: 'g2', kind: 'move', role_id: 'a', path: [{ x: 60, y: 0 }, { x: 60, y: 60 }] },
    ],
  };
  const halfway = stagePlaybackFrame(stage, 1_500);
  assert.deepEqual(halfway.positions, { a: { x: 30, y: 0 }, b: { x: 40, y: 10 } });
  assert.equal(halfway.activeGroupId, 'g1');
  assert.deepEqual(halfway.activeActionIds, ['a1', 'b1']);
  const next = stagePlaybackFrame(stage, 4_500);
  assert.deepEqual(next.positions, { a: { x: 60, y: 30 }, b: { x: 70, y: 10 } });
  assert.equal(next.activeGroupId, 'g2');
  const end = stagePlaybackFrame(stage, 9_000);
  assert.deepEqual(end.positions, { a: { x: 60, y: 60 }, b: { x: 70, y: 10 } });
  assert.equal(end.complete, true);
  assert.deepEqual(end.activeActionIds, []);
});
