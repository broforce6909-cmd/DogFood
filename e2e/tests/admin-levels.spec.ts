import { test, expect } from '@playwright/test';
import { login } from './helpers';

/**
 * The three admin levels, as a person sees them on the Accounts page. The API
 * tests (`tests/test_admin_levels.py`) are where the rules are pinned down;
 * this checks the page reflects them (the auditor's write refusal is `test_an_auditor_cannot_change_anything`).
 *
 * Uses the seeded `manager@example.com` and `auditor@example.com`.
 */
test('the Accounts page reflects the signed-in admin level', async ({ browser, baseURL }) => {
  test.setTimeout(90_000);
  const slow = expect.configure({ timeout: 20_000 });

  const open = async (email: string) => {
    const page = await (await browser.newContext({ baseURL })).newPage();
    await login(page, email);
    await page.goto('/admin');
    return page;
  };

  const owner = await open('admin@example.com');
  await slow(owner.getByRole('heading', { name: 'Provision an account' })).toBeVisible();
  await slow(owner.getByLabel('Admin level (admins only)')).toBeVisible();

  const manager = await open('manager@example.com');
  await slow(manager.getByText(/signed in as a manager/)).toBeVisible();
  await slow(manager.getByRole('heading', { name: 'Provision an account' })).toHaveCount(0);

  const auditor = await open('auditor@example.com');
  await slow(auditor.getByText(/signed in as an auditor \(read-only\)/)).toBeVisible();
  await slow(auditor.getByRole('heading', { name: 'Provision an account' })).toHaveCount(0);
});
