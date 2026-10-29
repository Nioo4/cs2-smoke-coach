import { createContext, useContext, useMemo, useRef, useState, type ReactNode } from 'react';

import { adminApi, encodeBasicUtf8 } from '../api/client';

interface AdminAuthContextValue {
  authorization: string | null;
  authGeneration: number;
  login: (username: string, password: string) => Promise<void>;
  clear: (expectedAuthorization?: string, expectedGeneration?: number) => void;
}

const AdminAuthContext = createContext<AdminAuthContextValue | null>(null);

export function AdminAuthProvider({ children }: { children: ReactNode }) {
  const [authorization, setAuthorization] = useState<string | null>(null);
  const [authGeneration, setAuthGeneration] = useState(0);
  const generation = useRef(0);
  const currentAuthorization = useRef<string | null>(null);
  const currentAuthGeneration = useRef(0);
  const value = useMemo<AdminAuthContextValue>(() => ({
    authorization,
    authGeneration,
    async login(username, password) {
      const attempt = ++generation.current;
      const nextAuthorization = encodeBasicUtf8(username, password);
      try {
        await adminApi.session(nextAuthorization);
      } catch (error) {
        if (attempt !== generation.current) return;
        throw error;
      }
      if (attempt === generation.current) {
        const nextGeneration = currentAuthGeneration.current + 1;
        currentAuthorization.current = nextAuthorization;
        currentAuthGeneration.current = nextGeneration;
        setAuthGeneration(nextGeneration);
        setAuthorization(nextAuthorization);
      }
    },
    clear(expectedAuthorization, expectedGeneration) {
      if (expectedGeneration !== undefined && currentAuthGeneration.current !== expectedGeneration) return;
      if (expectedAuthorization !== undefined && currentAuthorization.current !== expectedAuthorization) return;
      generation.current += 1;
      currentAuthorization.current = null;
      currentAuthGeneration.current += 1;
      setAuthGeneration(currentAuthGeneration.current);
      setAuthorization(null);
    },
  }), [authorization, authGeneration]);
  return <AdminAuthContext.Provider value={value}>{children}</AdminAuthContext.Provider>;
}

// The provider and its hook intentionally share one small module; the hook is the
// stable public entry point for the page-level authentication context.
// eslint-disable-next-line react-refresh/only-export-components
export function useAdminAuth(): AdminAuthContextValue {
  const value = useContext(AdminAuthContext);
  if (!value) throw new Error('useAdminAuth must be used inside AdminAuthProvider');
  return value;
}
