import { test, expect } from '@playwright/test';
import { login } from './helpers';

/**
 * Community voting, quadratic mode: casting *n* votes on a project costs
 * *n^2* credits, and the ballot page shows that cost live. Uses the seeded
 * `ines@example.com` ("a clean voter, with no conflict of interest") against
 * `raptors-winter`, which the seed leaves with voting open -- casting a
 * ballot here is a plain upsert (`test_a_voter_can_revise_their_ballot`'s own
 * backend guarantee), so re-running this against the same long-lived stack
 * just revises the same ballot rather than failing on a duplicate.
 */
test('a participant can cast quadratic votes and see the cost', async ({ page }) => {
  await login(page, 'ines@example.com');

  await page.goto('/events/raptors-winter/vote');

  // First run against a fresh stack: this voter has no ballot yet and needs
  // to claim one before the project list appears. Once claimed it stays
  // claimed, so a later re-run against the same long-lived stack skips
  // straight past this -- both are valid, so the test does not assume which.
  const startVoting = page.getByRole('button', { name: 'Start voting' });
  if (await startVoting.isVisible().catch(() => false)) {
    await startVoting.click();
  }

  await expect(page.getByText(/credits/i).first()).toBeVisible();

  const firstVoteInput = page.locator('input[name^="vote:"]').first();
  await firstVoteInput.fill('2');
  await page.getByRole('button', { name: 'Save my ballot' }).click();

  // 2 votes costs 2^2 = 4 credits under quadratic voting -- the one piece of
  // maths this page exists to get right, so the test checks the number, not
  // just that the page did not error.
  await expect(page.getByText(/costs 4\b/)).toBeVisible();
});
