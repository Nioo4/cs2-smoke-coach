import { useContext } from 'react';
import { UNSAFE_DataRouterContext, useBlocker } from 'react-router-dom';

function DataGuard({ hasUnconfirmedWork, exportDraft }: { hasUnconfirmedWork: () => boolean; exportDraft: () => void }) {
  const blocker = useBlocker(({ currentLocation, nextLocation }) => hasUnconfirmedWork() && (currentLocation.pathname !== nextLocation.pathname || currentLocation.search !== nextLocation.search));
  if (blocker.state !== 'blocked') return null;
  return <section role="alertdialog" aria-label="未确认内容离页保护" className="tactic-navigation-guard"><p>本页还有未确认内容。继续编辑，或先导出恢复副本再离开；导出不代表已保存。</p><button className="button" onClick={() => blocker.reset()}>返回继续编辑</button><button className="button" onClick={() => { exportDraft(); blocker.proceed(); }}>导出恢复副本后离开</button></section>;
}
export default function TacticNavigationGuard(props: { hasUnconfirmedWork: () => boolean; exportDraft: () => void }) {
  const dataRouter = useContext(UNSAFE_DataRouterContext);
  return dataRouter ? <DataGuard {...props} /> : null;
}
