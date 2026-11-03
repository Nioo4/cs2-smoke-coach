import { groupStageActions } from '../../../collaboration/src/stage-playback.mjs';
import type { Point, TacticStage } from './editorTypes';

export function actionDraftStart(stage: TacticStage, roleId: string, mode: 'after' | 'parallel'): Point {
  const groups = groupStageActions(stage);
  const prior = mode === 'parallel' ? groups.slice(0, -1) : groups;
  let point: Point = stage.initial_positions[roleId];
  for (const group of prior) for (const action of group.actions) if (action.kind === 'move' && action.role_id === roleId) point = action.path.at(-1)!;
  return { x: point.x, y: point.y };
}
