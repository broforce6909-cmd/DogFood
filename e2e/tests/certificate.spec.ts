import { test, expect } from '@playwright/test';
import { login } from './helpers';

/**
 * The participant self-service certificate wizard, end to end: search for
 * the project, sign in as a team member (a later pass gated this step to
 * team membership -- see tests/test_certificate_access.py on the backend
 * side for the isolation proof; this is the happy path through the same
 * UI), confirm which name is yours, and reach the preview/download step.
 *
 * Uses the seeded `kenji@example.com`, a real team member of "Switchyard" on
 * `raptors-winter` with an already-issued participation certificate --
 * chosen over creating fresh data because issuing a certificate is an
 * organizer-only bulk action tested on the backend
 * (test_certificates.py, test_certificate_access.py); this test's job is the
 * *lookup and download* UI, not proving issuance works too.
 */
test('a team member can find and preview their own certificate', async ({ page }) => {
  await page.goto('/certificate?q=Switchyard');
  await page.getByRole('link', { name: 'Switchyard' }).click();
  await expect(page).toHaveURL(/\/certificate\?submission=/);

  // Not signed in yet: the wizard must ask for a team-member login rather
  // than list any codes, per the ownership fix. Scoped to the main content,
  // not the nav bar, which has its own "Sign in" link visible whenever
  // signed out -- `getByRole` alone matches both.
  await expect(
    page.locator('#main-content').getByRole('link', { name: 'Sign in' }),
  ).toBeVisible();
  const nextUrl = new URL(page.url());
  await login(page, 'kenji@example.com');
  await page.goto(`/certificate?submission=${nextUrl.searchParams.get('submission')}`);

  await page.getByRole('link', { name: 'Kenji Watanabe' }).click();
  await expect(page).toHaveURL(/[?&]code=/);

  // The live preview image and both download links -- the actual point of
  // reaching step 3.
  await expect(page.getByRole('img', { name: /certificate preview/i })).toBeVisible();
  await expect(page.getByRole('link', { name: 'Download PDF' })).toHaveAttribute(
    'href',
    /\/api\/certificates\/.+\/pdf/,
  );
  await expect(page.getByRole('link', { name: 'Download PNG' })).toHaveAttribute(
    'href',
    /\/api\/certificates\/.+\/png/,
  );
});
