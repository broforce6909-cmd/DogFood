import { test, expect } from '@playwright/test';
import { SEEDED_PASSWORD } from './helpers';

/**
 * The sign-in role tabs are a check: an explicitly chosen tab that does not match
 * the account's real role is refused with a message naming the right tab, and
 * nobody is signed in. The right tab signs in as usual. (`tests/test_auth_api.py`
 * pins down the API rules -- including that a wrong password never reveals a role;
 * this checks the page wiring.)
 */
test('the wrong sign-in tab refuses with a message, the right one signs in', async ({ page }) => {
  test.setTimeout(60_000);
  const slow = expect.configure({ timeout: 15_000 });

  await page.goto('/login?as=participant');
  await page.fill('#email', 'organizer@example.com');
  await page.fill('#password', SEEDED_PASSWORD);
  await page.click('button[type="submit"]');

  await slow(page.getByText(/This account is an Organizer account, not a Participant one/)).toBeVisible();
  await slow(page.getByText(/try the Organizer tab/)).toBeVisible();
  await expect(page).toHaveURL(/\/login\?as=participant/);
  await expect(page.getByRole('button', { name: 'Sign out' })).toHaveCount(0);

  await page.goto('/login?as=organizer');
  await page.fill('#email', 'organizer@example.com');
  await page.fill('#password', SEEDED_PASSWORD);
  await page.click('button[type="submit"]');
  await slow(page.getByRole('button', { name: 'Sign out' })).toBeVisible();
});
