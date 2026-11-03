import { useEffect, useRef, useState } from 'react';
import * as Y from 'yjs';
import { applyCompositionEdit, applyTextDiff, captureComposition } from '../../../collaboration/src/text-editing.mjs';
import type { TacticCollaborationClient } from './collaborationClient';
import { decodeBytes } from './collaborationClient';

export default function TacticTextField({ client, field, label, stageId, disabled, maxLength = 2000 }: { client: TacticCollaborationClient; field: string; label: string; stageId: string; disabled: boolean; maxLength?: number }) {
  const input = useRef<HTMLTextAreaElement>(null);
  const composition = useRef<object | null>(null);
  const bufferedEdit = useRef<object | null>(null);
  const [invalidDraft, setInvalidDraft] = useState(false);
  const selection = useRef<{ start: Y.RelativePosition; end: Y.RelativePosition } | null>(null);
  const [scroll, setScroll] = useState(0);
  const text = client.doc.getMap<Y.Text>('fields').get(field);
  useEffect(() => {
    if (!text || !input.current) return;
    const mountedInput = input.current;
    const capture = () => {
      if (composition.current || bufferedEdit.current || document.activeElement !== mountedInput) return;
      selection.current = { start: Y.createRelativePositionFromTypeIndex(text, Math.min(mountedInput.selectionStart, text.length)), end: Y.createRelativePositionFromTypeIndex(text, Math.min(mountedInput.selectionEnd, text.length)) };
    };
    const update = () => {
      if (composition.current || bufferedEdit.current || !input.current) return;
      const node = input.current;
      if (node.value === text.toString()) return;
      node.value = text.toString();
      if (selection.current && document.activeElement === node) node.setSelectionRange(Y.createAbsolutePositionFromRelativePosition(selection.current.start, client.doc)?.index ?? 0, Y.createAbsolutePositionFromRelativePosition(selection.current.end, client.doc)?.index ?? 0);
    };
    input.current.value = text.toString(); text.observe(update); client.doc.on('beforeTransaction', capture);
    return () => {
      text.unobserve(update);
      client.doc.off('beforeTransaction', capture);
      if (composition.current) client.setLocalDraft({ field, value: mountedInput.value, composing: true }, field);
    };
  }, [client, text, field]);
  const editors = client.state.connected ? client.state.participants.filter(person => person.connection_id !== client.state.connectionId && person.field === field) : [];
  const required = field === 'tactic.title' || field.endsWith('.name') || field.startsWith('action.');
  function retainInvalid(value: string, base?: object) {
    if (text && !bufferedEdit.current) bufferedEdit.current = base ?? captureComposition(text);
    client.setLocalDraft({ field, value, invalid: true }, field); setInvalidDraft(true);
  }
  function commitBuffered(value: string) {
    if (!text || !bufferedEdit.current) return false;
    const result = applyCompositionEdit(client.doc, text, bufferedEdit.current, value);
    if (!result.ok) { client.setLocalDraft({ field, value, conflict: true }, field); return true; }
    bufferedEdit.current = null; client.setLocalDraft(null, field); setInvalidDraft(false);
    if (input.current) input.current.value = text.toString();
    return true;
  }
  function presence() {
    if (input.current && text) {
      selection.current = { start: Y.createRelativePositionFromTypeIndex(text, Math.min(input.current.selectionStart, text.length)), end: Y.createRelativePositionFromTypeIndex(text, Math.min(input.current.selectionEnd, text.length)) };
      client.presence(stageId, field, input.current.selectionStart);
    }
  }
  return <label className="tactic-text-field">{label}
    <span className="tactic-text-surface">
    <textarea ref={input} disabled={disabled || !text} maxLength={maxLength} rows={field.endsWith('.name') || field === 'tactic.title' ? 1 : 3}
      onScroll={event => setScroll(event.currentTarget.scrollTop)}
      onFocus={presence} onSelect={presence} onBlur={() => client.presence(stageId)}
      onCompositionStart={() => { if (text) { composition.current = bufferedEdit.current ?? captureComposition(text); client.setLocalDraft({ field, value: input.current?.value, composing: true }, field); } }}
      onCompositionEnd={event => {
        if (!text || !composition.current) return;
        if (required && !event.currentTarget.value.trim()) { retainInvalid(event.currentTarget.value, composition.current); composition.current = null; return; }
        const result = applyCompositionEdit(client.doc, text, composition.current, event.currentTarget.value, event.currentTarget.selectionStart, event.currentTarget.selectionEnd);
        composition.current = null;
        if (!result.ok) { client.setLocalDraft({ field, value: event.currentTarget.value, conflict: true }, field); return; }
        bufferedEdit.current = null; setInvalidDraft(false); client.setLocalDraft(null, field); event.currentTarget.value = text.toString();
        if (result.selection) event.currentTarget.setSelectionRange(Y.createAbsolutePositionFromRelativePosition(result.selection.start, client.doc)?.index ?? 0, Y.createAbsolutePositionFromRelativePosition(result.selection.end, client.doc)?.index ?? 0);
        presence();
      }}
      onChange={event => {
        if (!text) return;
        if (composition.current) { client.setLocalDraft({ field, value: event.currentTarget.value, composing: true }, field); return; }
        if (required && !event.currentTarget.value.trim()) { retainInvalid(event.currentTarget.value); return; }
        if (commitBuffered(event.currentTarget.value)) { presence(); return; }
        applyTextDiff(client.doc, text, event.currentTarget.value); presence();
      }} />
    {text && editors.map(person => {
      let position: Y.AbsolutePosition | null = null;
      try { if (person.relative_cursor) position = Y.createAbsolutePositionFromRelativePosition(Y.decodeRelativePosition(decodeBytes(person.relative_cursor)), client.doc); } catch { /* Invalid remote cursors are never rendered. */ }
      if (!position || position.type !== text) return null;
      const color = /^#[0-9a-f]{6}$/i.test(person.color) ? person.color : '#315d57';
      return <span key={person.connection_id} className="tactic-peer-overlay" aria-hidden="true"><span className="tactic-peer-mirror" style={{ transform: `translateY(-${scroll}px)` }}>{text.toString().slice(0, position.index)}<span className="tactic-peer-caret" style={{ borderColor: color }}><span style={{ background: color }}>{person.name}</span></span>{text.toString().slice(position.index)}</span></span>;
    })}
    </span>
    {invalidDraft && <small role="status">此项不能为空，当前输入仅保留在本页，填写后才会提交。</small>}
    {editors.map(person => <small key={person.connection_id}>{person.name} 正在编辑</small>)}
  </label>;
}
