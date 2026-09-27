import Link from 'next/link';

import { registerAction } from '../actions';
import { FormError } from '../components';

export const dynamic = 'force-dynamic';

export const metadata = { title: 'Register -- Dogfood' };

export default async function RegisterPage({
  searchParams,
}: {
  searchParams: Promise<{ error?: string; next?: string }>;
}) {
  const { error, next } = await searchParams;

  return (
    <main className="shell narrow">
      <p className="eyebrow">Register</p>
      <h1>Create an account.</h1>
      <p className="muted">
        Registration creates a participant. Judge, organizer and admin are handed out by an
        admin -- there is no path from this form to a privileged role.
      </p>
      <FormError message={error} />

      <form action={registerAction} className="panel stack">
        <input type="hidden" name="next" value={next ?? '/dashboard'} />
        <div className="field">
          <label htmlFor="display_name">Name</label>
          <input id="display_name" name="display_name" type="text" required maxLength={120} />
        </div>
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
            autoComplete="new-password"
            minLength={8}
            required
          />
          <p className="muted small" style={{ margin: '6px 0 0' }}>
            At least 8 characters. Stored as an Argon2id hash, never in the clear.
          </p>
        </div>
        <button className="primary" type="submit">
          Create account
        </button>
      </form>

      <p className="muted small" style={{ marginTop: 16 }}>
        Already registered? <Link href="/login">Sign in</Link>.
      </p>
    </main>
  );
}
