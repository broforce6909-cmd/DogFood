import { test, expect } from '@playwright/test';

/**
 * Published results, as a visitor with no account at all -- the seeded
 * `raptors-summer` is archived with its community vote already
 * published, so this is the one journey in the suite that needs no login and
 * no setup, and is the most robust to re-run of all of them for exactly that
 * reason.
 */
test('a visitor can view published community results with no account', async ({ page }) => {
  await page.goto('/events/raptors-summer/results');

  await expect(page.getByRole('heading', { name: 'Results' })).toBeVisible();
  // At least one ranked row -- proves the published totals actually render,
  // not just that the page loaded without an error.
  await expect(page.locator('table').getByText(/^\d+$/).first()).toBeVisible();
});
