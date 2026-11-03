import * as Y from 'yjs';
import { buildStageCommand } from './stage-commands.mjs';
import { STAGE_DOCUMENT_ID } from './stage-board-schema.mjs';
import { canonicalJson, formalTextFields, FORMAL_UUID, projectFormalDocument } from './formal-document.mjs';

export function legacyStage(stage) {
  const next = structuredClone(stage); delete next.version; delete next.lineup_ids;
  for (const point of Object.values(next.initial_positions)) delete point.version;
  for (const action of next.actions) delete action.version;
  return next;
}

export function legacyAction(action) { const next = structuredClone(action); delete next.version; return next; }

export function applyFormalCommand(baseline, candidate, command, recovery = null) {
  const { structure: source } = projectFormalDocument(baseline);
  let next; let sources = {};
  if (command.type === 'stage.lineups.set') {
    if (Object.keys(command).sort().join(',') !== 'lineup_ids,stage_id,type' || !Array.isArray(command.lineup_ids) || command.lineup_ids.length > 10 || new Set(command.lineup_ids).size !== command.lineup_ids.length || command.lineup_ids.some((id) => !FORMAL_UUID.test(id))) throw new Error('invalid_lineup_command');
    next = structuredClone(source);
    const stage = next.stages.find((item) => item.id === command.stage_id);
    if (!stage) throw new Error('stage_not_found');
    stage.lineup_ids = [...command.lineup_ids]; stage.version += 1;
  } else if (command.type === 'tactic.side.set') {
    if (Object.keys(command).sort().join(',') !== 'side,type' || !['T', 'CT'].includes(command.side)) throw new Error('invalid_side_command');
    next = structuredClone(source); next.side = command.side;
  } else {
    const archived = recovery ? { ...structuredClone(recovery), snapshot: recovery.kind === 'stage' ? { stage: legacyStage(recovery.snapshot.stage) } : { ...recovery.snapshot, action: legacyAction(recovery.snapshot.action) } } : null;
    const legacy = { schema_kind: 'stage-board', schema_version: 1, document_id: STAGE_DOCUMENT_ID, map: 'mirage', roles: structuredClone(source.roles), stages: source.stages.map(legacyStage), recovery: archived ? [archived] : [] };
    const prepared = buildStageCommand(legacy, command, baseline);
    sources = prepared.fieldSources;
    next = { ...source, roles: prepared.structure.roles, stages: prepared.structure.stages.map((stage) => {
      const previous = source.stages.find((item) => item.id === stage.id);
      return { ...stage, version: previous ? previous.version + (canonicalJson(legacyStage(previous)) === canonicalJson(stage) ? 0 : 1) : 1,
        lineup_ids: previous?.lineup_ids ?? (command.type === 'stage.copy' ? source.stages.find((item) => item.id === command.stage_id)?.lineup_ids : recovery?.kind === 'stage' ? recovery.snapshot.stage.lineup_ids : null) ?? [],
        initial_positions: Object.fromEntries(Object.entries(stage.initial_positions).map(([id, point]) => {
          const prior = previous?.initial_positions[id];
          return [id, { ...point, version: prior ? prior.version + (prior.x === point.x && prior.y === point.y ? 0 : 1) : 1 }];
        })),
        actions: stage.actions.map((action, index) => {
          const prior = previous?.actions.find((item) => item.id === action.id);
          return { ...action, version: prior ? prior.version + (canonicalJson(legacyAction(prior)) === canonicalJson(action) && previous.actions[index]?.id === action.id ? 0 : 1) : 1 };
        }),
      };
    }) };
  }
  const root = candidate.getMap('fields'); const old = baseline.getMap('fields');
  const keys = new Set(formalTextFields(next));
  for (const key of [...root.keys()]) if (!keys.has(key)) root.delete(key);
  for (const key of keys) {
    if (root.has(key)) continue;
    const source = sources[key];
    const value = source?.kind === 'literal' ? source.value : source?.kind === 'active' ? old.get(source.key)?.toString() : source?.kind === 'recovery' ? recovery?.texts[source.key] : undefined;
    if (typeof value !== 'string') throw new Error('invalid_formal_text_source');
    const text = new Y.Text(); if (value) text.insert(0, value); root.set(key, text);
  }
  candidate.getMap('document').set('structure', next);
  projectFormalDocument(candidate);
  return next;
}
