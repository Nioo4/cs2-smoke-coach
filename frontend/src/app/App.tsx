import AppRouter from './router';
import { AdminAuthProvider } from './adminAuth';

export default function App() {
  return (
    <AdminAuthProvider>
      {import.meta.env.MODE === 'test' && (
        <p className="test-environment-banner" role="note">
          自动化测试环境 · 数据仅用于测试，非真实教学。
        </p>
      )}
      <AppRouter />
    </AdminAuthProvider>
  );
}
