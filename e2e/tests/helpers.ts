import type { Page } from '@playwright/test';

/** Every seeded fixture account -- see README.md's "Sign in" table -- uses
 * this password. Never used for an account this suite creates itself. */
export const SEEDED_PASSWORD = 'dogfood2026';

export const API_BASE = process.env.E2E_API_BASE_URL ?? 'http://localhost:8000';

/** Drives the real login form, the same one a person fills in. Waits for the
 * post-submit redirect to actually leave `/login` before returning -- not just
 * for the nav bar's "Sign out" link, which can already be showing from a
 * *previous* account's still-active session on this same page (this helper is
 * sometimes called twice in one test, to switch accounts), and would then
 * resolve immediately without waiting for this login's own navigation.
 *
 * The submit button is targeted by its accessible name, not by
 * `button[type="submit"]` -- that selector also matches the nav bar's own
 * "Sign out" button (same type, same page, present whenever a previous
 * session is still active), and a plain `page.click()` takes the first DOM
 * match rather than enforcing uniqueness, so it would submit the wrong form. */
export async function login(page: Page, email: string, password = SEEDED_PASSWORD): Promise<void> {
  await page.goto('/login');
  await page.fill('#email', email);
  await page.fill('#password', password);
  await Promise.all([
    page.waitForURL((url) => url.pathname !== '/login'),
    page.getByRole('button', { name: 'Sign in' }).click(),
  ]);
  await page.getByRole('button', { name: 'Sign out' }).waitFor();
}

/** A unique-enough email per test run so registering a brand-new account
 * never collides with a previous run against the same long-lived stack. */
export function freshEmail(label: string): string {
  return `e2e-${label}-${Date.now()}-${Math.floor(Math.random() * 10_000)}@example.com`;
}
