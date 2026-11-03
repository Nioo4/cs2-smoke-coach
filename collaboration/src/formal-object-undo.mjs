import * as Y from 'yjs';
import { applyFormalCommand, legacyAction, legacyStage } from './formal-commands.mjs';
import { canonicalJson, formalTextFields, projectFormalDocument } from './formal-document.mjs';
import { FormalError } from './formal-http-store.mjs';

export const structureFingerprint = (structure) => canonicalJson(structure);

// Content comes only from the Node's one confirmed receipt, never from a client patch.
export function applyObjectUndo(baseline, candidate, receipt, recovery) {
  const current = projectFormalDocument(baseline).structure;
  let desired = structuredClone(receipt.before);
  if (receipt.isDelete) {
    const recoveredId = recovery?.kind === 'stage' ? recovery.snapshot?.stage?.id : recovery?.snapshot?.action?.id;
    if (!recovery || recovery.id !== receipt.recoveryId || recoveredId !== receipt.entityId || recovery.kind !== receipt.kind) throw new FormalError('TACTIC_CONFLICT');
    if (receipt.kind === 'stage') {
      const index = desired.stages.findIndex((stage) => stage.id === receipt.entityId);
      const restored = applyFormalCommand(baseline, candidate, { type: 'recovery.restore', recovery_id: recovery.id, after_stage_id: index ? desired.stages[index - 1].id : null }, recovery);
      const fresh = restored.stages.find((stage) => !current.stages.some((item) => item.id === stage.id));
      if (!fresh) throw new FormalError('TACTIC_CONFLICT');
      desired.stages[index] = fresh;
    } else {
      const stage = desired.stages.find((item) => item.id === receipt.stageId);
      const index = stage.actions.findIndex((action) => action.id === receipt.entityId);
      const original = stage.actions[index];
      const restored = applyFormalCommand(baseline, candidate, { type: 'recovery.restore', recovery_id: recovery.id, stage_id: stage.id }, recovery);
      const fresh = restored.stages.find((item) => item.id === stage.id).actions.find((action) => !current.stages.find((item) => item.id === stage.id).actions.some((item) => item.id === action.id));
      if (!fresh) throw new FormalError('TACTIC_CONFLICT');
      const groupSurvives = current.stages.find((item) => item.id === stage.id).actions.some((action) => action.group_id === original.group_id);
      stage.actions[index] = { ...original, id: fresh.id, group_id: groupSurvives ? original.group_id : fresh.group_id };
    }
  }
  desired = monotonicVersions(current, desired);
  const fields = candidate.getMap('fields'); const allowed = new Set(formalTextFields(desired));
  for (const key of [...fields.keys()]) if (!allowed.has(key)) fields.delete(key);
  for (const key of allowed) if (!(fields.get(key) instanceof Y.Text)) throw new FormalError('TACTIC_CONFLICT');
  candidate.getMap('document').set('structure', desired);
  projectFormalDocument(candidate);
  return desired;
}

function monotonicVersions(current, desired) {
  for (const stage of desired.stages) {
    const previous = current.stages.find((item) => item.id === stage.id);
    stage.version = previous ? previous.version + (canonicalJson({ ...legacyStage(previous), lineup_ids: previous.lineup_ids }) === canonicalJson({ ...legacyStage(stage), lineup_ids: stage.lineup_ids }) ? 0 : 1) : 1;
    for (const [id, point] of Object.entries(stage.initial_positions)) {
      const prior = previous?.initial_positions[id];
      point.version = prior ? prior.version + (prior.x === point.x && prior.y === point.y ? 0 : 1) : 1;
    }
    for (const [index, action] of stage.actions.entries()) {
      const prior = previous?.actions.find((item) => item.id === action.id);
      action.version = prior ? prior.version + (canonicalJson(legacyAction(prior)) === canonicalJson(legacyAction(action)) && previous.actions[index]?.id === action.id ? 0 : 1) : 1;
    }
  }
  return desired;
}

export function undoReceipt(before, after, command, operationId, room) {
  const isDelete = ['stage.delete', 'action.delete'].includes(command.type);
  const entityId = command.type === 'stage.delete' ? command.stage_id : command.action_id;
  const kind = command.type === 'stage.delete' ? 'stage' : 'action';
  const recoveryId = isDelete ? room.recoveries.find((item) => item.entity_id === entityId && item.kind === kind)?.id : null;
  // A missing durable recovery cannot become a working deletion undo button.
  if (isDelete && !recoveryId) return null;
  const beforeKeys = new Set(formalTextFields(before));
  return { before: structuredClone(before), after: structureFingerprint(after), generation: room.structureGeneration,
    sourceId: operationId, recoveryId, isDelete, kind, entityId, stageId: command.stage_id,
    removedFields: formalTextFields(after).filter((key) => !beforeKeys.has(key)).map((key) => [key, room.fieldGenerations.get(key) ?? 0]) };
}
