import { test, expect } from '@playwright/test';
import { login } from './helpers';

/**
 * Judge calibration, end to end: an organizer adds a practice project with
 * expected scores, a judge scores it low, and the organizer's report flags that
 * judge as harsh -- while the judge's own page never shows an expected score.
 *
 * Uses seeded staff on `raptors-winter`, a unique project name per run, and
 * removes its own project at the end. The judge is `judge.whitfield`, the same
 * seeded judge the scoring test uses. One side effect it cannot undo: because a
 * verdict needs every practice project scored, it also (idempotently) scores the
 * seeded practice project as that judge, so on a long-lived local stack Whitfield
 * stops showing as "not started" on the calibration report. A fresh stack (CI,
 * a fresh clone) is unaffected.
 */
const slow = expect.configure({ timeout: 20_000 });

test('a harsh judge is flagged on the organizer calibration report', async ({ browser, baseURL }) => {
  // Four server-rendered pages across two sessions, several with multiple API
  // calls: give first (cold) renders room instead of the 5s default.
  test.setTimeout(90_000);
  const name = `E2E Practice ${Date.now()}`;
  const organizer = await (await browser.newContext({ baseURL })).newPage();
  const judge = await (await browser.newContext({ baseURL })).newPage();

  await login(organizer, 'organizer@example.com');
  await organizer.goto('/organizer/raptors-winter/judging/calibration');
  await organizer.fill('#cal-name', name);
  await organizer.fill('#cal-description', 'A known project for the E2E suite.');
  const expected = organizer.locator('input[name^="expected:"]');
  const count = await expected.count();
  slow(count).toBeGreaterThan(0);
  for (let i = 0; i < count; i += 1) {
    await expected.nth(i).fill('4.5');
  }
  await organizer.getByRole('button', { name: 'Add practice project' }).click();
  await slow(organizer.getByText(name)).toBeVisible();

  // The judge scores every criterion far below the expected 4.5.
  await login(judge, 'judge.whitfield@example.com');
  await judge.goto('/judging/calibration');
  await slow(judge.locator('form', { hasText: name })).toBeVisible();
  await slow(judge.getByText('4.5')).toHaveCount(0);
  // A verdict needs *every* practice project scored (the seed ships one of its
  // own), so score each one low rather than only this run's.
  const cards = judge.locator('form', { has: judge.locator('input[type="number"]') });
  const total = await cards.count();
  for (let c = 0; c < total; c += 1) {
    const card = cards.nth(c);
    const inputs = card.locator('input[type="number"]');
    const n = await inputs.count();
    for (let i = 0; i < n; i += 1) {
      await inputs.nth(i).fill('1.5');
    }
    await card.getByRole('button', { name: 'Save practice scores' }).click();
    // Not the shared "saved" notice: after the first save the page already
    // shows it, so it would pass before this save had landed. The card's own
    // "Scored" marker only appears once *this* project's scores are in.
    await slow(cards.nth(c).getByText(/· Scored$/)).toBeVisible();
  }

  await organizer.reload();
  const row = organizer.locator('tr', { hasText: 'Dale Whitfield' });
  await slow(row).toBeVisible();
  await slow(row).toContainText('Harsh');

  // Leave the stack as found.
  await organizer
    .locator('div.criterion', { hasText: name })
    .getByRole('button', { name: 'Remove' })
    .click();
  await slow(organizer.getByText(name)).toHaveCount(0);
});
