/**
 * Dates, rendered in UTC and said out loud.
 *
 * Everything the API stores is `timestamptz` in UTC, and every deadline is
 * compared against the server clock. Rendering in the viewer's local timezone
 * would be friendlier and would also mean two people on a call disagreeing
 * about when submissions close, so the suffix is always shown.
 */

const DATE_TIME = new Intl.DateTimeFormat('en-GB', {
  timeZone: 'UTC',
  day: 'numeric',
  month: 'short',
  year: 'numeric',
  hour: '2-digit',
  minute: '2-digit',
});

const DATE_ONLY = new Intl.DateTimeFormat('en-GB', {
  timeZone: 'UTC',
  day: 'numeric',
  month: 'short',
  year: 'numeric',
});

export function formatDateTime(iso: string | null | undefined): string {
  if (!iso) return '--';
  return `${DATE_TIME.format(new Date(iso))} UTC`;
}

export function formatDate(iso: string | null | undefined): string {
  if (!iso) return '--';
  return DATE_ONLY.format(new Date(iso));
}

export function formatRange(startIso: string, endIso: string): string {
  return `${formatDate(startIso)} - ${formatDate(endIso)}`;
}

/** "in 6 days", "in 4 hours", "closed 30 days ago". Coarse on purpose. */
export function untilDeadline(iso: string, now: Date = new Date()): string {
  const ms = new Date(iso).getTime() - now.getTime();
  const past = ms < 0;
  const abs = Math.abs(ms);

  const days = Math.floor(abs / 86_400_000);
  const hours = Math.floor(abs / 3_600_000);
  const minutes = Math.floor(abs / 60_000);

  const amount =
    days >= 1 ? plural(days, 'day') : hours >= 1 ? plural(hours, 'hour') : plural(minutes, 'minute');

  return past ? `closed ${amount} ago` : `in ${amount}`;
}

function plural(count: number, unit: string): string {
  return `${count} ${unit}${count === 1 ? '' : 's'}`;
}

/** Initials for the generated thumbnail placeholder. */
export function initials(name: string): string {
  return name
    .split(/\s+/)
    .filter(Boolean)
    .slice(0, 2)
    .map((word) => word[0]?.toUpperCase() ?? '')
    .join('');
}

/**
 * A second layer against stored `javascript:`-URI XSS via a link field.
 *
 * The backend rejects anything but `http://`/`https://` for `repo_url`,
 * `live_url`, `demo_video_url` and `website_url` (see `app/schemas._http_url`),
 * so this should never fire against data the API wrote. It exists anyway
 * because "the backend already validates it" is exactly the assumption that
 * makes a second bug in the same place a full compromise instead of a caught
 * one -- rows seeded before the validator existed, a future write path that
 * forgets to go through the schema, or a CSV import are all ways stale or
 * unvalidated data could reach this component. `<a href={href}>` with an
 * unchecked `href` is where a `javascript:` URI actually executes, on click,
 * in this page's own origin -- `rel="noopener noreferrer"` does not stop that,
 * because those attributes only change navigation behaviour and a
 * `javascript:` URI is not a navigation.
 *
 * Returns `null` for anything that is not `http://` or `https://`, so a caller
 * can render plain text instead of a link rather than a broken or dangerous one.
 */
export function safeHref(url: string | null | undefined): string | null {
  if (!url) return null;
  return /^https?:\/\//i.test(url.trim()) ? url : null;
}
