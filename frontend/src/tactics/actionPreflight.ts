import type { TacticAction, TacticStage } from './editorTypes';

// Mirrors only stage-board-schema.mjs:191–194 / tactics_document.py usesRole.
// Full command/schema validation stays on the server (the command module uses node:crypto).
function usedRole(action: TacticAction): string | null {
  return action.kind === 'text' ? null : action.kind === 'utility' ? action.thrower_role_id : action.role_id;
}

export function parallelRoleConflict(stage: TacticStage, command: Record<string, unknown>): string | null {
  let role: unknown;
  let group: string | undefined;
  let source: TacticAction | undefined;
  if (command.type === 'action.add') {
    if (command.group_mode !== 'parallel' || command.kind === 'text' || !['move', 'wait', 'observe', 'utility'].includes(String(command.kind))) return null;
    role = command.kind === 'utility' ? command.thrower_role_id : command.role_id;
    group = stage.actions.at(-1)?.group_id;
  } else if (command.type === 'action.update' || command.type === 'action.group') {
    source = stage.actions.find(action => action.id === command.action_id);
    if (!source || source.kind === 'text') return null;
    role = usedRole(source); group = source.group_id;
    if (command.type === 'action.update') {
      if (!command.changes || typeof command.changes !== 'object' || Array.isArray(command.changes)) return null;
      const changes = command.changes as Record<string, unknown>;
      const key = source.kind === 'utility' ? 'thrower_role_id' : 'role_id';
      if (Object.hasOwn(changes, key)) role = changes[key];
    } else {
      const groups = [...new Set(stage.actions.map(action => action.group_id))];
      if (!Number.isInteger(command.group_index) || Number(command.group_index) < 0 || Number(command.group_index) >= groups.length) return null;
      group = groups[Number(command.group_index)];
    }
  } else return null;
  if (typeof role !== 'string' || !role || !group) return null;
  return stage.actions.some(action => action.id !== source?.id && action.group_id === group && usedRole(action) === role) ? role : null;
}
