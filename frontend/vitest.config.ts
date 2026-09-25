import { fileURLToPath } from 'node:url';

import react from '@vitejs/plugin-react';
import { defineConfig } from 'vitest/config';

/**
 * Test runner configuration.
 *
 * The repository shipped without one, so every guarantee the console makes -
 * that a 403 does not sign an operator out, that an unreachable source never
 * renders as an empty list - was held in place by nothing but review. These
 * are the properties an incident console is judged on; they need a harness.
 *
 * jsdom rather than node, because the modules under test branch on
 * `typeof window`: `baseUrl()` picks a different origin on the server, and
 * `readSession()` returns an empty session outright. Testing them under node
 * would exercise the wrong half of every one of those branches.
 */
export default defineConfig({
  plugins: [react()],
  resolve: {
    // Mirrors the `@/*` path mapping in tsconfig.json. Kept as an explicit
    // alias rather than derived from the tsconfig, so a test resolving
    // differently from the application is a visible edit, not a silent one.
    alias: { '@': fileURLToPath(new URL('./src', import.meta.url)) },
  },
  test: {
    environment: 'jsdom',
    setupFiles: ['./vitest.setup.ts'],
    include: ['src/**/*.test.ts', 'src/**/*.test.tsx'],
    // Globals stay off: every test imports what it uses. This keeps the ESLint
    // config free of a test-only globals override, so the same rules apply to
    // test and application code alike.
    globals: false,
    // Module-level state is the hazard here - `api.ts` holds the bearer token
    // and the unauthorized-subscriber set in module scope, and `vi.stubGlobal`
    // replaces `fetch` for the whole file. Anything left standing leaks into
    // the next test and turns a real failure into a mystery.
    restoreMocks: true,
    clearMocks: true,
    unstubEnvs: true,
    unstubGlobals: true,
  },
});
