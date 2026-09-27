import { test, expect } from '@playwright/test';
import { API_BASE, login } from './helpers';

/**
 * Event archival through the real UI, on a throwaway event so no seeded one is
 * touched: archive it, see the frozen banner and the "Archived" section on the
 * organizer list, unarchive it, then delete it. (`tests/test_event_archival.py`
 * is where the rules -- including that every write verb is refused while
 * archived -- are pinned down; this checks the page wiring.)
 */
test('an organizer can archive an event and bring it back', async ({ page }) => {
  test.setTimeout(90_000);
  const slow = expect.configure({ timeout: 20_000 });
  const slug = `e2e-archive-${Date.now()}`;
  const now = Date.now();
  const iso = (days: number) => new Date(now + days * 86_400_000).toISOString();

  await login(page, 'organizer@example.com');
  const created = await page.request.post(`${API_BASE}/api/events`, {
    data: {
      slug,
      name: `E2E Archive ${slug}`,
      starts_at: iso(1),
      ends_at: iso(3),
      registration_opens_at: iso(0),
      submission_opens_at: iso(1),
      submission_deadline: iso(3),
      max_team_size: 4,
      is_published: true,
    },
  });
  expect(created.status()).toBe(201);

  try {
    await page.goto(`/organizer/${slug}`);
    await page.getByRole('button', { name: 'Archive this event' }).click();
    await slow(page.getByText('This event is archived.')).toBeVisible();
    await slow(page.getByRole('button', { name: 'Unarchive' })).toBeVisible();

    // Frozen: the API refuses a write even from a signed-in organizer.
    const blocked = await page.request.patch(`${API_BASE}/api/events/${slug}`, {
      data: { name: 'Renamed while archived' },
    });
    expect(blocked.status()).toBe(403);

    // Out of the default list, present under "Archived".
    await page.goto('/organizer');
    const archivedSection = page.locator('section', { hasText: 'Archived' });
    await slow(archivedSection.getByRole('link', { name: `E2E Archive ${slug}` })).toBeVisible();
    await expect(page.locator('table').getByText(slug)).toHaveCount(0);

    // The public page still works and says so.
    await page.goto(`/events/${slug}`);
    await slow(page.getByText(/archived\. The gallery and results below/)).toBeVisible();

    await page.goto(`/organizer/${slug}`);
    await page.getByRole('button', { name: 'Unarchive' }).click();
    await slow(page.getByText('This event is archived.')).toHaveCount(0);
    await slow(page.getByRole('button', { name: 'Archive this event' })).toBeVisible();
  } finally {
    await page.request.post(`${API_BASE}/api/events/${slug}/unarchive`);
    await page.request.delete(`${API_BASE}/api/events/${slug}`);
  }
});
