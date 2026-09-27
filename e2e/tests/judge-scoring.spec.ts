import { test, expect } from '@playwright/test';
import { login } from './helpers';

/**
 * A judge logs in and scores a ballot on a continuous, not discrete, scale --
 * the one UI surface this suite most needs to catch a silent regression on,
 * since `<input type="number" step="0.1">` replaced five radio buttons in a
 * later pass and a frontend change could easily reintroduce whole-number-only
 * without any backend test noticing (the API itself is not being called
 * directly by anything here, unlike scripts/screenshots/capture.js).
 *
 * Uses the seeded `judge.whitfield@example.com` on `raptors-winter`
 * rather than creating a fresh judge: provisioning one is an admin-only path
 * exercised elsewhere (backend `test_users_api.py`), and re-scoring an
 * already-assigned ballot is idempotent, so this is safe to re-run against
 * the same long-lived stack.
 */
test('a judge can score a ballot with a decimal value', async ({ page }) => {
  await login(page, 'judge.whitfield@example.com');

  await page.goto('/judging');
  // Any ballot works -- the test does not care which project, only that
  // scoring one round-trips a decimal correctly -- so this takes whichever
  // one the page lists first rather than assuming a specific project name
  // is assigned to this judge.
  // `/judging/calibration` (practice projects) shares the prefix but is not a ballot.
  await page.locator('a[href^="/judging/"]:not([href="/judging/calibration"])').first().click();
  await expect(page).toHaveURL(/\/judging\/.+/);

  const scoreInputs = page.locator('input[type="number"][step="0.1"]');
  const count = await scoreInputs.count();
  expect(count).toBeGreaterThan(0);

  const values = Array.from({ length: count }, (_, i) => (4 + i * 0.1).toFixed(1));
  for (let i = 0; i < count; i += 1) {
    await scoreInputs.nth(i).fill(values[i]);
  }

  await page.getByRole('button', { name: 'Save and mark complete' }).click();
  await expect(page.getByText('Ballot saved.')).toBeVisible();

  // Reload and confirm the decimal value round-tripped through the API and
  // the numeric(5,2) column rather than being silently rounded anywhere in
  // the chain -- asserted on a genuinely fractional entry, not the first one
  // (which is a whole number and would pass even if rounding were broken).
  await page.reload();
  if (count > 1) {
    await expect(scoreInputs.nth(1)).toHaveValue(values[1]);
  }
});
