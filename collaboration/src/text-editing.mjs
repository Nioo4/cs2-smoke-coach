import * as Y from 'yjs';
import { LOCAL_TEXT_ORIGIN } from './text-history.mjs';

export function captureComposition(text) {
  const base = text.toString();
  return {
    base,
    boundaries: Array.from({ length: base.length + 1 }, (_, index) => Y.createRelativePositionFromTypeIndex(text, index)),
    characters: Array.from({ length: base.length }, (_, index) => ({
      value: base[index],
      start: Y.createRelativePositionFromTypeIndex(text, index),
      end: Y.createRelativePositionFromTypeIndex(text, index + 1, -1),
    })),
  };
}

export function applyCompositionEdit(doc, text, composition, desired, selectionStart = desired.length, selectionEnd = selectionStart) {
  if (!composition || typeof desired !== 'string') return { ok: false };
  const { prefix, suffix } = diffRange(composition.base, desired);
  const removedEnd = composition.base.length - suffix;
  const insertedEnd = desired.length - suffix;
  const insertion = desired.slice(prefix, insertedEnd);
  const insertionPosition = absolute(composition.boundaries[prefix], doc);
  if (!validPosition(insertionPosition, text)) return { ok: false };

  const targets = [];
  for (let index = prefix; index < removedEnd; index += 1) {
    const character = composition.characters[index];
    const start = absolute(character?.start, doc);
    const end = absolute(character?.end, doc);
    if (!validPosition(start, text) || !validPosition(end, text) || end.index < start.index) return { ok: false };
    if (end.index === start.index) continue;
    if (end.index !== start.index + 1 || text.toString().slice(start.index, end.index) !== character.value) return { ok: false };
    targets.push(start.index);
  }
  if (new Set(targets).size !== targets.length || targets.some((index) => index < insertionPosition.index)) return { ok: false };

  if (targets.length || insertion) {
    doc.transact(() => {
      targets.sort((left, right) => right - left).forEach((index) => text.delete(index, 1));
      if (insertion) text.insert(insertionPosition.index, insertion);
    }, LOCAL_TEXT_ORIGIN);
  }

  const selection = {
    start: mapSelection(selectionStart, composition, prefix, removedEnd, insertedEnd, insertionPosition.index, insertion, text),
    end: mapSelection(selectionEnd, composition, prefix, removedEnd, insertedEnd, insertionPosition.index, insertion, text),
  };
  return { ok: true, value: text.toString(), selection };
}

export function applyTextDiff(doc, text, desired) {
  const current = text.toString();
  const { prefix, suffix } = diffRange(current, desired);
  if (prefix === current.length && prefix === desired.length) return;
  doc.transact(() => {
    if (current.length > prefix + suffix) text.delete(prefix, current.length - prefix - suffix);
    const insertion = desired.slice(prefix, desired.length - suffix);
    if (insertion) text.insert(prefix, insertion);
  }, LOCAL_TEXT_ORIGIN);
}

function mapSelection(value, composition, prefix, removedEnd, insertedEnd, insertionAt, insertion, text) {
  const index = Math.max(0, Math.min(Number.isSafeInteger(value) ? value : insertedEnd, insertedEnd + (composition.base.length - removedEnd)));
  if (index >= prefix && index <= insertedEnd && insertion.length) {
    const offset = index - prefix;
    return Y.createRelativePositionFromTypeIndex(text, insertionAt + offset, offset === insertion.length ? -1 : 0);
  }
  if (index <= prefix) return composition.boundaries[index];
  return composition.boundaries[removedEnd + index - insertedEnd];
}

function absolute(relative, doc) {
  return relative ? Y.createAbsolutePositionFromRelativePosition(relative, doc) : null;
}

function validPosition(position, text) {
  return position?.type === text && Number.isSafeInteger(position.index) && position.index >= 0 && position.index <= text.length;
}

function diffRange(before, after) {
  let prefix = 0;
  while (prefix < before.length && prefix < after.length && before[prefix] === after[prefix]) prefix += 1;
  let suffix = 0;
  while (suffix < before.length - prefix && suffix < after.length - prefix && before[before.length - suffix - 1] === after[after.length - suffix - 1]) suffix += 1;
  return { prefix, suffix };
}
