import { test, expect } from '@playwright/test';
import { freshEmail } from './helpers';

/**
 * The one journey every other tier depends on: a brand-new person registers,
 * forms a team, starts a draft, fills it out, and submits it into the public
 * gallery. Runs against `dogfood` (submissions open in the seed), with a
 * unique email each run so this is safe to re-run against the same
 * long-lived stack without colliding with a previous run's account.
 *
 * Deliberately fresh data rather than a seeded team: this is the one test in
 * the suite whose entire point is proving the *creation* path works end to
 * end, not just that already-seeded state renders.
 */
test('a new participant can register, form a team, and submit a project', async ({ page }) => {
  const email = freshEmail('participant');
  const password = 'e2e-test-pw-1';

  await page.goto('/register');
  await page.fill('#display_name', 'E2E Participant');
  await page.fill('#email', email);
  await page.fill('#password', password);
  await page.click('button[type="submit"]');
  await expect(page).toHaveURL(/\/dashboard/);

  await page.goto('/events/dogfood');
  await page.fill('#discord_username', 'e2e_participant');
  await page.getByRole('button', { name: 'Register' }).click();
  await expect(page).toHaveURL(/\/events\/dogfood/);

  // Unique per run, the same reasoning as `freshEmail`: this suite can run
  // repeatedly against the same long-lived stack, and a fixed name here
  // would make the gallery assertion below ambiguous the second time.
  const runId = Date.now();
  const teamName = `E2E Team ${runId}`;
  const projectName = `E2E Test Project ${runId}`;
  await page.fill('#team-name', teamName);
  await page.getByRole('button', { name: 'Create team' }).click();
  await expect(page).toHaveURL(/\/teams\//);
  await expect(page.getByRole('heading', { name: teamName })).toBeVisible();

  await page.fill('#project-name', projectName);
  await page.getByRole('button', { name: 'Start a draft' }).click();
  await expect(page).toHaveURL(/\/submissions\/.+\/edit/);

  // Required fields plus the one required custom question dogfood's
  // seed carries -- omitting it is exactly what would trip the submit
  // endpoint's completeness check (`REQUIRED_TO_SUBMIT` plus any required
  // `event_questions` row), so answering it here is what proves that path
  // rather than a submission that happened to need nothing extra.
  await page.fill('#tagline', 'A project created entirely by the E2E suite');
  await page.fill('#description', 'Exercises the full participant journey end to end.');
  await page.selectOption('#track_id', { index: 1 });
  await page.fill('#discord_usernames', 'e2e_participant');
  await page.fill('#repo_url', 'https://github.com/example/e2e-test-project');
  const questionBox = page.locator('textarea[name^="answer:"]').first();
  if (await questionBox.count()) {
    await questionBox.fill('Nothing -- this is a synthetic E2E fixture.');
  }
  await page.getByRole('button', { name: 'Save draft' }).click();
  await expect(page.getByRole('button', { name: 'Submit project' })).toBeEnabled();

  // A successful submit redirects straight to the public project page, not
  // back to the edit form.
  await page.getByRole('button', { name: 'Submit project' }).click();
  await expect(page).toHaveURL(/\/projects\/.+/);
  await expect(page.getByRole('heading', { name: projectName })).toBeVisible();

  await page.goto('/events/dogfood/gallery');
  await expect(page.getByText(projectName)).toBeVisible();
});
