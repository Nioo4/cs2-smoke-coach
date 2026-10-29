import { Link, Route, Routes } from 'react-router-dom';

import DisclaimerPage from '../pages/DisclaimerPage';
import HomePage from '../pages/HomePage';
import PrivacyPage from '../pages/PrivacyPage';
import AdminPage from '../pages/AdminPage';

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
    <Routes>
      <Route path="/" element={<HomePage />} />
      <Route path="/privacy" element={<PrivacyPage />} />
      <Route path="/disclaimer" element={<DisclaimerPage />} />
      <Route path="/admin" element={<AdminPage />} />
      <Route path="*" element={<NotFoundPage />} />
    </Routes>
  );
}
