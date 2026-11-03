import { useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import { ApiError } from '../api/client';
import { accountRequest, type Session } from './api';
import type { TacticProjection } from './editorTypes';
import TacticPlaybackPanel from './TacticPlaybackPanel';
import './author-presentation.css';

interface SavedView { tactic: { id: string; title: string; revision: number }; epoch: string; revision: number; projection: TacticProjection }
export default function TacticPresentation({ tacticId, userId }: { tacticId: string; userId: string }) {
  const [refresh, setRefresh] = useState(0);
  const requestKey = JSON.stringify([tacticId, userId, refresh]);
  const [result, setResult] = useState<{ key: string; view?: SavedView; error?: string }>({ key: '' });
  useEffect(() => {
    const controller = new AbortController();
    async function load() {
      try {
        const session = await accountRequest<Session>('auth/session', 'GET', undefined, undefined, controller.signal);
        if (session.user.id !== userId) throw new Error('account_changed');
        const view = await accountRequest<SavedView>(`tactics/${encodeURIComponent(tacticId)}/present`, 'GET', undefined, undefined, controller.signal);
        if (!Number.isSafeInteger(view.revision) || view.tactic.id !== tacticId || view.projection.structure.document_id !== tacticId || view.projection.structure.schema_version !== 2) throw new Error('invalid_saved_view');
        if (!controller.signal.aborted) setResult({ key: requestKey, view });
      } catch (error) {
        if (controller.signal.aborted) return;
        const message = error instanceof ApiError && error.status === 404 ? '找不到这份战术，可能已被删除或不属于当前账户。'
          : error instanceof ApiError && [401, 403].includes(error.status) ? '登录已失效或没有访问权限，请重新登录后查看。'
          : error instanceof Error && error.message === 'account_changed' ? '当前登录账户已改变，请重新加载页面。'
          : '暂时无法读取已保存的战术，请重试。';
        // Failed reads replace, rather than retain, any previous account's snapshot.
        setResult({ key: requestKey, error: message });
      }
    }
    void load();
    return () => controller.abort();
  }, [tacticId, userId, refresh, requestKey]);
  const loaded = result.key === requestKey;
  return <section className="author-presentation" aria-label="已保存战术演示">
    <header className="author-presentation-header">
      <div className="author-presentation-identity"><p className="eyebrow">AUTHOR PRESENTATION · 作者演示</p>
        <div className="author-presentation-title"><h1 title={loaded ? result.view?.tactic.title : undefined}>{loaded && result.view ? result.view.tactic.title : '作者演示'}</h1>{loaded && result.view && <span className="author-presentation-version">已保存版本 {result.view.revision}</span>}</div>
        <p className="author-presentation-note">这里只演示已保存版本，不实时跟随草稿修改。点击刷新读取最新保存版本。</p>
      </div>
      <div className="author-presentation-actions"><Link className="button" to={`/tactics/${encodeURIComponent(tacticId)}`}>返回战术详情</Link>
        <button className="button" disabled={!loaded} onClick={() => setRefresh(value => value + 1)}>刷新已保存版本</button>
      </div>
    </header>
    {!loaded ? <p role="status">正在读取已保存版本…</p> : result.error ? <p role="alert">{result.error}</p> : result.view && <>
      <TacticPlaybackPanel key={requestKey} confirmed={result.view.projection} revision={result.view.revision} epoch={result.view.epoch} hasUnconfirmedWork={false} />
    </>}
  </section>;
}
