import Link from 'next/link';

import { loginAction } from '../actions';
import { FormError } from '../components';

export const dynamic = 'force-dynamic';

export const metadata = { title: 'Sign in -- Dogfood' };

// The four account-holding roles, in the same ascending order `/admin`'s own
// role picker uses. `visitor` is deliberately not a tab here -- it is what
// an unauthenticated request resolves to, never a role an account is
// assigned, so there is nothing to sign in "as".
const TABS = [
  {
    key: 'participant',
    label: 'Participant',
    hint: 'Build a project, join a team, vote.',
  },
  { key: 'judge', label: 'Judge', hint: 'Score projects assigned to you.' },
  {
    key: 'organizer',
    label: 'Organizer',
    hint: 'Run an event: tracks, prizes, judging, announcements.',
  },
  { key: 'admin', label: 'Admin', hint: 'Platform administration and event management.' },
] as const;

type TabKey = (typeof TABS)[number]['key'];

/**
 * The tabs are a check, not a gate: a role never comes from what was clicked.
 *
 * When a tab has been *explicitly* chosen (`?as=`), it is posted along with the
 * credentials as `expected_role`, and the API -- after verifying the password,
 * before creating any session -- refuses an account whose real role is a
 * different one, saying which tab to use instead. Nobody is signed in by that
 * refusal. An account's actual role is still decided server-side on every
 * request, as everywhere else in this app.
 *
 * With no explicit choice (a plain `/login`, or a deep link with only `?next=`)
 * nothing is enforced: the default highlight on "Participant" is a starting
 * point, not a selection, and treating it as one would turn every judge or
 * organizer following a link into an error.
 */
export default async function LoginPage({
  searchParams,
}: {
  searchParams: Promise<{ error?: string; next?: string; as?: string }>;
}) {
  const { error, next, as } = await searchParams;
  const chosen = TABS.some((t) => t.key === as) ? (as as TabKey) : null;
  const active: TabKey = chosen ?? 'participant';
  const nextSuffix = next ? `&next=${encodeURIComponent(next)}` : '';

  return (
    <main className="shell narrow">
      <p className="eyebrow">Sign in</p>
      <h1>Welcome back.</h1>
      <FormError message={error} />

      <div className="row" role="tablist" aria-label="Sign in as" style={{ marginBottom: 4 }}>
        {TABS.map((tab) => (
          <Link
            key={tab.key}
            href={`/login?as=${tab.key}${nextSuffix}`}
            role="tab"
            aria-selected={active === tab.key}
            className={active === tab.key ? 'button primary small' : 'button small'}
          >
            {tab.label}
          </Link>
        ))}
      </div>
      <p className="muted small" style={{ marginTop: 0, marginBottom: 16 }}>
        {TABS.find((t) => t.key === active)!.hint}{' '}
        {chosen
          ? 'Pick the tab that matches your account; if it does not, you will be told which one to use instead of being signed in.'
          : 'Choose the tab for your account type.'}
      </p>
      <p className="muted small" style={{ marginTop: 0, marginBottom: 16 }}>
        <Link href="/gallery">Continue as Visitor</Link> -- browse the public gallery, no
        account needed.
      </p>

      <form action={loginAction} className="panel stack">
        <input type="hidden" name="next" value={next ?? '/dashboard'} />
        {chosen && <input type="hidden" name="as" value={chosen} />}
        <div className="field">
          <label htmlFor="email">Email</label>
          <input id="email" name="email" type="email" autoComplete="email" required />
        </div>
        <div className="field">
          <label htmlFor="password">Password</label>
          <input
            id="password"
            name="password"
            type="password"
            autoComplete="current-password"
            required
          />
        </div>
        <button className="primary" type="submit">
          Sign in
        </button>
      </form>

      <p className="muted small" style={{ marginTop: 16 }}>
        No account? <Link href="/register">Register</Link>. On a seeded install every fixture
        account uses the password <code>dogfood2026</code> -- try{' '}
        <code>organizer@example.com</code> or <code>sam@example.com</code>.
      </p>
    </main>
  );
}
