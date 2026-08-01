import { defineConfig } from 'vitest/config';
import path from 'path';

// No @vitejs/plugin-react here: its Babel transform is a dev-server concern
// (Fast Refresh), and under Vite 7 / rolldown it emits deprecation warnings for
// `esbuild` / `optimizeDeps.esbuildOptions` that we cannot resolve from our side.
// Vitest transforms JSX natively with the automatic runtime, which is all the
// test run needs. The production build still goes through Next.js, untouched.
// (CLAUDE.md #21 — the suite must be warning-free.)
export default defineConfig({
  test: {
    environment: 'jsdom',
    globals: true,
    setupFiles: ['./src/test/setup.ts'],
    include: ['src/**/*.{test,spec}.{js,jsx,ts,tsx}'],
    coverage: {
      provider: 'v8',
      reporter: ['text', 'json', 'html'],
      include: ['src/hooks/**/*.{js,jsx,ts,tsx}', 'src/components/**/*.{js,jsx,ts,tsx}'],
      exclude: ['src/components/ui/**'],
    },
  },
  resolve: {
    alias: {
      '@': path.resolve(__dirname, './src'),
    },
  },
});
