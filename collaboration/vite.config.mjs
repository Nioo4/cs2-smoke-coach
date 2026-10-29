import { defineConfig } from 'vite';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
export default defineConfig({
  root: here,
  publicDir: path.resolve(here, '../frontend/public'),
  build: {
    outDir: 'dist', emptyOutDir: true,
    rollupOptions: { input: { collaboration: path.resolve(here, 'index.html'), stageBoard: path.resolve(here, 'stage-board.html') } },
  },
  server: { host: '127.0.0.1', port: 4173, strictPort: true },
});
