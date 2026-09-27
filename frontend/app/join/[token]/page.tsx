import Link from 'next/link';

import { joinTeamAction } from '../../actions';
import { FormError } from '../../components';
import { getMe } from '@/lib/session';

export const dynamic = 'force-dynamic';

export const metadata = { title: 'Join a team -- Dogfood' };

/**
 * The other end of an invite link.
 *
 * Deliberately a form with a button rather than a join-on-load: a link in a
 * chat window gets previewed, prefetched and scanned by things that are not the
 * person it was sent to, and none of those should join a team.
 */
export default async function JoinPage({
  params,
  searchParams,
}: {
  params: Promise<{ token: string }>;
  searchParams: Promise<{ error?: string }>;
}) {
  const { token } = await params;
  const { error } = await searchParams;
  const me = await getMe();

  return (
    <main className="shell narrow">
      <p className="eyebrow">Invitation</p>
      <h1>Join a team.</h1>
      <FormError message={error} />

      {me.authenticated ? (
        <form action={joinTeamAction} className="panel stack">
          <input type="hidden" name="token" value={token} />
          <p className="muted small" style={{ margin: 0 }}>
            You are signed in as <strong>{me.user?.display_name}</strong>. Accepting puts you on
            this team for its event -- you can only be on one team per event.
          </p>
          <button className="primary" type="submit">
            Accept invitation
          </button>
        </form>
      ) : (
        <div className="panel">
          <p className="muted">You need an account before you can join a team.</p>
          <div className="row">
            <Link className="button primary" href={`/login?next=/join/${token}`}>
              Sign in
            </Link>
            <Link className="button" href={`/register?next=/join/${token}`}>
              Register
            </Link>
          </div>
        </div>
      )}
    </main>
  );
}
