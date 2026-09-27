import { defineConfig, devices } from '@playwright/test';

/**
 * Critical-path browser E2E, deliberately separate from `tests/` (backend
 * pytest, API-level) and `frontend`'s typecheck/build. Those two answer "does
 * the code work"; this answers "can a person actually click through it" --
 * the one thing neither catches when a frontend change silently breaks a
 * flow the API itself still serves correctly.
 *
 * Runs against a real, already-up `docker compose` stack (see
 * `.github/workflows/ci.yml`'s `e2e-tests` job and README.md#tests) --
 * nothing here starts its own server, the same reasoning the screenshot
 * script in `scripts/screenshots/` already follows.
 */
export default defineConfig({
  testDir: './tests',
  fullyParallel: true,
  // A shared GitHub Actions runner is not a beefy box either: 5 tests each
  // driving a real Chromium instance against a Next.js SSR page is enough to
  // starve a small runner if they all fire at once. Capped, not serialized --
  // this is still real concurrency, just bounded to what a 2-core runner can
  // actually serve without every navigation racing the same CPU.
  workers: process.env.CI ? 2 : undefined,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 1 : 0,
  reporter: process.env.CI ? [['html', { open: 'never' }], ['github']] : 'list',
  timeout: 30_000,
  use: {
    baseURL: process.env.E2E_BASE_URL ?? 'http://localhost:3000',
    trace: 'on-first-retry',
    screenshot: 'only-on-failure',
    video: 'retain-on-failure',
  },
  projects: [
    {
      name: 'chromium',
      use: { ...devices['Desktop Chrome'] },
    },
  ],
});
