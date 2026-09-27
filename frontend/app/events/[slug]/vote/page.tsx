import Link from 'next/link';
import { notFound } from 'next/navigation';

import {
  castVotesAction,
  claimBallotAction,
  withdrawVotesAction,
} from '../../../actions';
import { Empty } from '../../../components';
import { apiOrNull } from '@/lib/api';
import { formatDateTime } from '@/lib/format';
import { getMe } from '@/lib/session';
import type { Ballot, Event } from '@/lib/types';

export const dynamic = 'force-dynamic';

export const metadata = { title: 'Vote -- Dogfood' };

/**
 * The public ballot.
 *
 * Three things this page is careful about, all of them anti-abuse rather than
 * decoration:
 *
 * 1. **The project order comes from the API** and is not re-sorted here. It is
 *    randomised per voter to kill position bias, and re-sorting would undo that.
 * 2. **Quadratic cost is shown next to every input**, because a method nobody
 *    understands is a method that feels rigged. The running total is computed
 *    server-side on save, so the displayed cost is a guide and the API is the rule.
 * 3. **No totals anywhere.** This page never shows how anybody else voted; the
 *    tally is a separate, gated route.
 */
export default async function VotePage({
  params,
  searchParams,
}: {
  params: Promise<{ slug: string }>;
  searchParams: Promise<{ error?: string; saved?: string; withdrawn?: string }>;
}) {
  const { slug } = await params;
  const { error, saved, withdrawn } = await searchParams;

  const event = await apiOrNull<Event>(`/api/events/${slug}`);
  if (!event) notFound();

  const me = await getMe();
  // 401 when there is no ballot yet: the claim step below is the answer, not an error.
  const ballot = await apiOrNull<Ballot>(`/api/events/${slug}/ballot`);

  const quadratic = event.voting_method === 'quadratic';
  const needsEmail = event.voting_access === 'email_gated';
  const needsAccount = event.voting_access === 'authenticated' && !me.authenticated;

  return (
    <main className="shell">
      <section style={{ padding: '24px 0 4px' }}>
        <Link href={`/events/${slug}`} className="muted" style={{ fontSize: 13 }}>
          ← {event.name}
        </Link>
        <p className="eyebrow" style={{ marginTop: 10 }}>
          Community vote
        </p>
        <h1 style={{ fontSize: 30 }}>{event.name}</h1>
        <p className="muted">
          {event.voting_open ? (
            <>
              Voting is <strong>open</strong>
              {event.voting_closes_at && <> until {formatDateTime(event.voting_closes_at)}</>}
            </>
          ) : event.voting_opens_at ? (
            <>Voting opens {formatDateTime(event.voting_opens_at)}</>
          ) : (
            <>Voting has not been scheduled for this event.</>
          )}
        </p>
      </section>

      {error && <p className="notice bad">{error}</p>}
      {saved && <p className="notice">Your ballot is saved.</p>}
      {withdrawn && <p className="notice">Your ballot has been withdrawn.</p>}

      <section className="panel">
        <h2 style={{ fontSize: 18, marginTop: 0 }}>How this vote works</h2>
        {quadratic ? (
          <>
            <p style={{ marginTop: 0 }}>
              You have <strong>{event.vote_credits} credits</strong>. Putting{' '}
              <em>n</em> votes on a project costs <em>n²</em> credits — 1 vote costs 1,
              3 votes cost 9, 10 votes cost 100.
            </p>
            <p className="muted" style={{ fontSize: 13, marginBottom: 0 }}>
              So you can shout about one project or nod at several, and shouting is
              expensive. That is the point: it stops a loud minority deciding the
              outcome. The maths is in{' '}
              <Link href={`/events/${slug}/results`}>the results page</Link> and in
              JUDGING.md.
            </p>
          </>
        ) : (
          <p style={{ marginTop: 0, marginBottom: 0 }}>
            One vote each, on up to <strong>{event.votes_per_voter} projects</strong>.
          </p>
        )}
      </section>

      {/* ---------------------------------------------------------------- */}
      {!ballot ? (
        <section className="panel">
          <h2 style={{ fontSize: 18, marginTop: 0 }}>Get a ballot</h2>
          {needsAccount ? (
            <p>
              This event is open to signed-in voters only.{' '}
              <Link href={`/login?next=/events/${slug}/vote`}>Sign in</Link> or{' '}
              <Link href="/register">register</Link> to vote.
            </p>
          ) : (
            <form action={claimBallotAction} className="stack">
              <input type="hidden" name="slug" value={slug} />
              {needsEmail && (
                <>
                  <label htmlFor="email">Your email</label>
                  <input id="email" name="email" type="email" required />
                  <p className="muted" style={{ fontSize: 13, marginTop: 0 }}>
                    One ballot per address. Nothing is sent to it and nothing verifies
                    it — it stops the same address voting twice, and no more.
                  </p>
                </>
              )}
              <button type="submit" className="button primary">
                {needsEmail ? 'Continue' : 'Start voting'}
              </button>
            </form>
          )}
        </section>
      ) : (
        <>
          <section className="panel">
            <div className="stat-row">
              {quadratic ? (
                <>
                  <Stat label="Credits" value={String(ballot.credits_total)} />
                  <Stat label="Spent" value={String(ballot.credits_spent)} />
                  <Stat label="Left" value={String(ballot.credits_remaining)} />
                </>
              ) : (
                <>
                  <Stat label="Picks used" value={String(ballot.votes.length)} />
                  <Stat label="Allowed" value={String(ballot.max_projects)} />
                </>
              )}
              <Stat label="Projects" value={String(ballot.projects.length)} />
            </div>
            {ballot.votes.length > 0 && (
              <p className="muted" style={{ fontSize: 13, marginBottom: 0 }}>
                Currently backing:{' '}
                {ballot.votes
                  .map((v) => `${v.submission_name} (${v.credits})`)
                  .join(', ')}
                .
              </p>
            )}
          </section>

          {!ballot.voting_open && (
            <p className="notice">
              Voting is closed, so this ballot is read-only. Your votes are recorded as
              they were.
            </p>
          )}

          <form action={castVotesAction}>
            <input type="hidden" name="slug" value={slug} />
            {ballot.projects.length === 0 ? (
              <Empty>No projects have been entered in this event yet.</Empty>
            ) : (
              <div className="stack">
                {ballot.projects.map((project) => {
                  const current = ballot.votes.find((v) => v.submission_id === project.id);
                  return (
                    <div key={project.id} className="card spread">
                      <div>
                        <strong>
                          <Link href={`/projects/${project.id}`}>{project.name}</Link>
                        </strong>
                        {project.track && <span className="tag">{project.track.name}</span>}
                        <div className="muted" style={{ fontSize: 13 }}>
                          {project.tagline ?? project.team_name}
                        </div>
                      </div>
                      <div style={{ textAlign: 'right', whiteSpace: 'nowrap' }}>
                        <label
                          htmlFor={`vote-${project.id}`}
                          className="muted"
                          style={{ fontSize: 12, display: 'block', margin: 0 }}
                        >
                          {quadratic ? 'votes (cost = n²)' : 'vote'}
                        </label>
                        <input
                          id={`vote-${project.id}`}
                          name={`vote:${project.id}`}
                          type="number"
                          min={0}
                          max={quadratic ? Math.floor(Math.sqrt(ballot.credits_total)) : 1}
                          step={1}
                          defaultValue={current?.credits ?? 0}
                          disabled={!ballot.voting_open}
                          style={{ width: 90 }}
                        />
                        {current && quadratic && (
                          <div className="muted" style={{ fontSize: 12 }}>
                            costs {current.cost}
                          </div>
                        )}
                      </div>
                    </div>
                  );
                })}
              </div>
            )}

            {ballot.voting_open && ballot.projects.length > 0 && (
              <div className="row" style={{ marginTop: 16 }}>
                <button type="submit" className="button primary">
                  Save my ballot
                </button>
                <span className="muted" style={{ fontSize: 13 }}>
                  Leave a project at 0 to not back it. You can change this until voting
                  closes.
                </span>
              </div>
            )}
          </form>

          {ballot.voting_open && ballot.votes.length > 0 && (
            <form action={withdrawVotesAction} style={{ marginTop: 16 }}>
              <input type="hidden" name="slug" value={slug} />
              <button type="submit" className="button">
                Withdraw my whole ballot
              </button>
            </form>
          )}
        </>
      )}
    </main>
  );
}

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div className="stat">
      <div className="stat-value">{value}</div>
      <div className="stat-label">{label}</div>
    </div>
  );
}
