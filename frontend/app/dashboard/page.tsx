import Link from 'next/link';
import { redirect } from 'next/navigation';

import { Empty } from '../components';
import { api } from '@/lib/api';
import { formatDateTime, untilDeadline } from '@/lib/format';
import { getMe } from '@/lib/session';
import { atLeast, type EventSummary, type Submission } from '@/lib/types';

export const dynamic = 'force-dynamic';

export const metadata = { title: 'Dashboard -- Dogfood' };

/**
 * `/dashboard` is the one stable post-login destination -- the login page's
 * `next` defaults here, and it is where a stale bookmark or a typed URL
 * still lands. Rather than showing every role the participant view below
 * (which is what happened before this existed: a judge or organizer with
 * `/dashboard` open just saw "Nothing yet"), this fans out to the landing
 * page each role actually uses. No new pages -- `/organizer` and `/judging`
 * already exist and already gate themselves; this only decides which one to
 * send someone to.
 *
 * The API, not this redirect, is what actually enforces any of it: a judge
 * who edits the URL back to `/dashboard` sees the participant view below
 * (empty, since a judge has no submissions), not an error -- the same
 * "hide, don't gate" courtesy the nav's role-conditional links already give.
 */
export default async function DashboardPage() {
  const me = await getMe();
  if (!me.authenticated) redirect('/login?next=/dashboard');
  if (atLeast(me.role, 'organizer')) redirect('/organizer');
  if (me.role === 'judge') redirect('/judging');

  const [submissions, events] = await Promise.all([
    api<Submission[]>('/api/me/submissions'),
    api<EventSummary[]>('/api/events'),
  ]);

  const enteredEventIds = new Set(submissions.map((s) => s.event_id));
  const openElsewhere = events.filter(
    (event) => !enteredEventIds.has(event.id) && new Date(event.submission_deadline) > new Date(),
  );

  return (
    <main className="shell">
      <p className="eyebrow">Signed in as {me.user?.email}</p>
      <h1>Your projects</h1>
      <p className="muted">
        Drafts are private to your team until you submit them. You can keep editing right up to
        the deadline -- and not one request after it.
      </p>

      {submissions.length === 0 ? (
        <Empty>
          Nothing yet. Join an event, form a team, and start a draft.
        </Empty>
      ) : (
        <div className="stack" style={{ marginTop: 20 }}>
          {submissions.map((submission) => (
            <section key={submission.id} className="panel">
              <div className="spread">
                <div>
                  <h2 style={{ margin: 0 }}>
                    <Link href={`/projects/${submission.id}`}>{submission.name}</Link>
                  </h2>
                  <p className="muted small" style={{ margin: '4px 0 0' }}>
                    {submission.team_name} · {submission.event_slug}
                  </p>
                </div>
                <div className="row">
                  {submission.status === 'submitted' ? (
                    <span className="pill ok">Submitted</span>
                  ) : (
                    <span className="pill warn">Draft</span>
                  )}
                  {submission.can_edit && (
                    <Link className="button" href={`/submissions/${submission.id}/edit`}>
                      Edit
                    </Link>
                  )}
                </div>
              </div>

              {submission.status === 'draft' && !submission.can_submit && (
                <p className="muted small" style={{ margin: '12px 0 0' }}>
                  This draft can no longer be submitted -- the deadline for {submission.event_slug}{' '}
                  has passed. It stays here, and it stays out of the gallery.
                </p>
              )}
            </section>
          ))}
        </div>
      )}

      {openElsewhere.length > 0 && (
        <>
          <hr />
          <h2>Open events</h2>
          <div className="grid">
            {openElsewhere.map((event) => (
              <Link key={event.id} className="card" href={`/events/${event.slug}`}>
                <h3 style={{ margin: 0 }}>{event.name}</h3>
                <p className="muted small" style={{ margin: '6px 0 0' }}>
                  Deadline {formatDateTime(event.submission_deadline)}
                  <br />
                  {untilDeadline(event.submission_deadline)}
                </p>
              </Link>
            ))}
          </div>
        </>
      )}
    </main>
  );
}
