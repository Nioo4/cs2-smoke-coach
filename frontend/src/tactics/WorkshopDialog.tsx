import { useEffect, useRef, type ReactNode, type RefObject } from 'react';
import './workshop-business.css';

export default function WorkshopDialog({ title, onClose, children, suspended = false, returnFocus, className = '' }: { title: string; onClose: () => void; children: ReactNode; suspended?: boolean; returnFocus?: RefObject<HTMLElement | null>; className?: string }) {
  const ref = useRef<HTMLDialogElement>(null);
  const composing = useRef(false);
  useEffect(() => {
    const dialog = ref.current;
    const previous = returnFocus?.current ?? document.activeElement as HTMLElement | null;
    if (!suspended) { if (dialog?.showModal) dialog.showModal(); else dialog?.setAttribute('open', ''); }
    return () => { dialog?.close?.(); dialog?.removeAttribute('open'); previous?.focus(); };
  }, [suspended, returnFocus]);
  return <dialog ref={ref} className={`workshop-dialog ${className}`.trim()} aria-label={title}
    onCompositionStartCapture={() => { composing.current = true; }} onCompositionEndCapture={() => { composing.current = false; }}
    onKeyDownCapture={event => { if (event.key === 'Escape' && (composing.current || event.nativeEvent.isComposing)) { event.preventDefault(); event.stopPropagation(); } }}
    onCancel={event => { event.preventDefault(); if (!composing.current) onClose(); }}>
    <header><h2>{title}</h2><button className="button" aria-label={`关闭${title}`} onClick={onClose}>关闭</button></header>{children}
  </dialog>;
}
