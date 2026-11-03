import { defineConfig } from 'vitest/config';
import react from '@vitejs/plugin-react';

const apiTarget = process.env.VITE_API_PROXY_TARGET ?? 'http://localhost:8080';
const collabTarget = process.env.VITE_COLLAB_PROXY_TARGET ?? 'http://127.0.0.1:4186';
const collabUrl = new URL(collabTarget);
if (!['http:', 'https:', 'ws:', 'wss:'].includes(collabUrl.protocol) || !['127.0.0.1', 'localhost', '[::1]'].includes(collabUrl.hostname)) {
  throw new Error('VITE_COLLAB_PROXY_TARGET must use a loopback development service.');
}

export default defineConfig({
  plugins: [react()],
  resolve: { dedupe: ['yjs'] },
  server: {
    port: 5173,
    strictPort: true,
    proxy: {
      '/api': apiTarget,
      '/media': apiTarget,
      '/ws/tactics': { target: collabTarget, ws: true },
    },
  },
  test: {
    include: ['tests/**/*.{test,spec}.{ts,tsx}'],
    environment: 'jsdom',
    setupFiles: ['./tests/setup.ts'],
    coverage: {
      provider: 'v8',
      reporter: ['text', 'html'],
      thresholds: {
        statements: 80,
        functions: 80,
        lines: 80,
        branches: 75,
      },
      exclude: ['src/main.tsx'],
    },
  },
});
