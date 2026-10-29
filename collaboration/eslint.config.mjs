const readonly = (names) => Object.fromEntries(names.map((name) => [name, 'readonly']));
const common = {
  languageOptions: { ecmaVersion: 'latest', sourceType: 'module' },
  rules: { 'no-undef': 'error' },
};

export default [
  {
    ...common,
    files: ['src/main.js'],
    languageOptions: {
      ...common.languageOptions,
      globals: readonly(['Blob', 'CSS', 'URL', 'URLSearchParams', 'atob', 'btoa', 'clearInterval', 'console', 'crypto', 'document', 'fetch', 'location', 'setInterval', 'setTimeout', 'structuredClone', 'WebSocket', 'window']),
    },
  },
  {
    ...common,
    files: ['src/stage-board.js'],
    languageOptions: {
      ...common.languageOptions,
      globals: readonly(['Blob', 'CSS', 'FormData', 'URL', 'atob', 'btoa', 'cancelAnimationFrame', 'confirm', 'crypto', 'document', 'fetch', 'location', 'matchMedia', 'performance', 'requestAnimationFrame', 'setTimeout', 'structuredClone', 'WebSocket', 'window']),
    },
  },
  {
    ...common,
    files: ['eslint.config.mjs', 'src/**/*.mjs', 'test/**/*.mjs', 'scripts/**/*.mjs', 'server.mjs'],
    languageOptions: {
      ...common.languageOptions,
      globals: readonly(['AbortSignal', 'Buffer', 'TextEncoder', 'clearInterval', 'clearTimeout', 'console', 'crypto', 'fetch', 'process', 'setInterval', 'setTimeout', 'structuredClone', 'URL']),
    },
  },
];
