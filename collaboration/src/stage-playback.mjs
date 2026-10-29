export const DEFAULT_GROUP_DURATION_MS = 3_000;

export function groupStageActions(stage) {
  const groups = [];
  for (const action of stage.actions) {
    const current = groups.at(-1);
    if (!current || current.id !== action.group_id) groups.push({ id: action.group_id, actions: [action] });
    else current.actions.push(action);
  }
  return groups;
}

export function interpolatePath(path, progress) {
  if (!Array.isArray(path) || path.length < 2) throw new Error('invalid_playback_path');
  const points = path.map(({ x, y }) => ({ x, y }));
  const lengths = points.slice(1).map((point, index) => Math.hypot(point.x - points[index].x, point.y - points[index].y));
  const total = lengths.reduce((sum, length) => sum + length, 0);
  if (total === 0) return points[0];
  let remaining = Math.max(0, Math.min(1, progress)) * total;
  for (let index = 0; index < lengths.length; index += 1) {
    const length = lengths[index];
    if (remaining <= length || index === lengths.length - 1) {
      const amount = length ? Math.min(1, remaining / length) : 1;
      return {
        x: points[index].x + (points[index + 1].x - points[index].x) * amount,
        y: points[index].y + (points[index + 1].y - points[index].y) * amount,
      };
    }
    remaining -= length;
  }
  return points.at(-1);
}

export function stagePlaybackFrame(stage, elapsedMs, groupDurationMs = DEFAULT_GROUP_DURATION_MS) {
  const groups = groupStageActions(stage);
  const positions = structuredClone(stage.initial_positions);
  if (!groups.length) return { positions, activeGroupId: null, activeActionIds: [], complete: true, progress: 1 };
  const safeElapsed = Math.max(0, Number.isFinite(elapsedMs) ? elapsedMs : 0);
  const groupIndex = Math.min(groups.length - 1, Math.floor(safeElapsed / groupDurationMs));
  const groupElapsed = safeElapsed - groupIndex * groupDurationMs;

  for (let index = 0; index < groupIndex; index += 1) applyGroupEnd(positions, groups[index]);
  const active = groups[groupIndex];
  const progress = Math.max(0, Math.min(1, groupElapsed / groupDurationMs));
  for (const action of active.actions) {
    if (action.kind === 'move') positions[action.role_id] = interpolatePath(action.path, progress);
  }
  if (progress >= 1) applyGroupEnd(positions, active);
  return {
    positions,
    activeGroupId: safeElapsed >= groups.length * groupDurationMs ? null : active.id,
    activeActionIds: safeElapsed >= groups.length * groupDurationMs ? [] : active.actions.map((action) => action.id),
    complete: safeElapsed >= groups.length * groupDurationMs,
    progress: safeElapsed >= groups.length * groupDurationMs ? 1 : progress,
  };
}

function applyGroupEnd(positions, group) {
  for (const action of group.actions) if (action.kind === 'move') positions[action.role_id] = structuredClone(action.path.at(-1));
}
