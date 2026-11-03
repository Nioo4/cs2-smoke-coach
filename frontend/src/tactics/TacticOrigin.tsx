import { useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import { resolveTacticOrigin } from './tacticOriginResolver';

interface Origin { label: string; to: string }

export default function TacticOrigin({ notebookId, scope, connected, retry }: { notebookId: string | null; scope: string; connected: boolean; retry: () => void }) {
  const key = JSON.stringify([notebookId, scope, connected]);
  const [result, setResult] = useState<{ key: string; origin?: Origin; error?: boolean }>({ key: '' });
  useEffect(() => {
    if (!notebookId || !connected) return;
    const controller = new AbortController();
    resolveTacticOrigin(notebookId, controller.signal).then(origin => { if (!controller.signal.aborted) setResult({ key, origin }); })
      .catch(() => { if (!controller.signal.aborted) setResult({ key, error: true }); });
    return () => controller.abort();
  }, [key, notebookId, connected]);
  const origin = connected && notebookId && result.key === key && !result.error ? result.origin : null;
  return origin ? <span className="editor-origin"><Link to={origin.to}>返回{origin.label}</Link><span title={origin.label}>{origin.label}</span></span>
    : <span className="editor-origin"><span>归属{notebookId && connected && result.key !== key ? '核对中…' : '未知'}</span><button className="button" disabled={!connected} onClick={retry}>重新核对归属</button></span>;
}
