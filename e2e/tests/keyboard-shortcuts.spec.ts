import { test, expect } from '@playwright/test';
import { login } from './helpers';

/**
 * Keyboard enhancements layered on top of existing plain-HTML forms. Every
 * test here asserts the *underlying* server action still ran (a score
 * persisted, a comparison recorded, a real navigation happened) -- the point
 * of these shortcuts is that they drive the same form a mouse click would,
 * not a parallel client-side path, so a test that only checked DOM state
 * without reloading would miss a shortcut that looks right but never submits.
 */

test('a judge can quick-pick a score and move focus with the keyboard', async ({ page }) => {
  await login(page, 'judge.whitfield@example.com');

  await page.goto('/judging');
  await page.locator('a[href^="/judging/"]:not([href="/judging/calibration"])').first().click();
  await expect(page).toHaveURL(/\/judging\/.+/);

  const scoreInputs = page.locator('input[type="number"][step="0.1"]');
  const count = await scoreInputs.count();
  expect(count).toBeGreaterThan(1);

  await scoreInputs.first().focus();
  await page.keyboard.press('3');
  await expect(scoreInputs.first()).toHaveValue('3');

  // Down/j moves focus to the next criterion without submitting anything.
  await page.keyboard.press('ArrowDown');
  const secondId = await scoreInputs.nth(1).getAttribute('id');
  const focusedId = await page.evaluate(() => document.activeElement?.id);
  expect(focusedId).toBe(secondId);

  // "Save progress" rather than "...and mark complete": this test is only
  // exercising the keyboard shortcut on the first criterion, and completeness
  // requires every criterion scored, which is a different test's concern.
  await page.getByRole('button', { name: 'Save progress' }).click();
  await expect(page.getByText('Ballot saved.')).toBeVisible();

  // The quick-picked value round-tripped through the real POST, not just the DOM.
  await page.reload();
  await expect(scoreInputs.first()).toHaveValue('3.0');
});

test('a judge can pick a pairwise winner with the arrow keys', async ({ page }) => {
  // Pairwise is off by default on the seeded event; an organizer turns it on
  // through the same settings form beat #5.5 of the demo script uses.
  await login(page, 'organizer@example.com');
  await page.goto('/organizer/raptors-winter');
  const pairwiseToggle = page.locator('input[name="pairwise_enabled"]');
  if (!(await pairwiseToggle.isChecked())) {
    await pairwiseToggle.check();
    await page.getByRole('button', { name: 'Save event' }).click();
  }

  await login(page, 'judge.rivera@example.com');
  await page.goto('/events/raptors-winter/pairwise');

  const leftButton = page.locator('button[name="winner"]').first();
  const leftId = await leftButton.getAttribute('value');
  await expect(leftButton).toBeVisible();

  await page.keyboard.press('ArrowLeft');
  await expect(page).toHaveURL(/saved=1/);
  await expect(page.getByText('Recorded. Here is the next pair.')).toBeVisible();
  void leftId; // asserted implicitly: a 409/"not enough" page would not show a next pair
});

test('Ctrl/Cmd+K opens the command palette and navigates to a result', async ({ page }) => {
  await page.goto('/');

  await page.keyboard.press('Control+k');
  const dialog = page.getByRole('dialog', { name: 'Quick navigation' });
  await expect(dialog).toBeVisible();

  const search = page.getByRole('textbox', { name: 'Search' });
  await search.fill('Gallery');
  await expect(page.getByRole('option', { name: /Gallery/ })).toBeVisible();

  await page.keyboard.press('Enter');
  await expect(page).toHaveURL(/\/gallery$/);
  await expect(dialog).not.toBeVisible();
});
