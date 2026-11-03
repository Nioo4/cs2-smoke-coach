import { Link, Route, Routes } from 'react-router-dom';
import { lazy, Suspense } from 'react';

import DisclaimerPage from '../pages/DisclaimerPage';
import HomePage from '../pages/HomePage';
import PrivacyPage from '../pages/PrivacyPage';
import AdminPage from '../pages/AdminPage';
const TacticsGate = lazy(() => import('../tactics/TacticsGate'));

function NotFoundPage() {
  return (
    <main className="page-shell compact-page">
      <p className="eyebrow">404 · 页面不存在</p>
      <h1>找不到这页。</h1>
      <Link className="text-link" to="/">
        返回教学页
      </Link>
    </main>
  );
}

export default function AppRouter() {
  return (
    <Suspense fallback={<p className="page-shell" role="status">正在载入战术本…</p>}><Routes>
      <Route path="/" element={<HomePage />} />
      <Route path="/privacy" element={<PrivacyPage />} />
      <Route path="/disclaimer" element={<DisclaimerPage />} />
      <Route path="/admin" element={<AdminPage />} />
      <Route path="/tactics" element={<TacticsGate />} />
      <Route path="/tactics/:tacticId" element={<TacticsGate />} />
      <Route path="/tactics/:tacticId/present" element={<TacticsGate />} />
      <Route path="/account" element={<TacticsGate />} />
      <Route path="/teams" element={<TacticsGate key="teams" />} />
      <Route path="/teams/:teamId" element={<TacticsGate key="team-detail" />} />
      <Route path="/join" element={<TacticsGate key="join" />} />
      <Route path="/share" element={<TacticsGate key="share" publicShare />} />
      <Route path="*" element={<NotFoundPage />} />
    </Routes></Suspense>
  );
}
