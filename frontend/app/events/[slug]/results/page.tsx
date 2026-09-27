import Link from 'next/link';
import { notFound } from 'next/navigation';

import { Empty } from '../../../components';
import { apiOrNull } from '@/lib/api';
import { getMe } from '@/lib/session';
import { atLeast, type Event, type Tally } from '@/lib/types';

export const dynamic = 'force-dynamic';

export const metadata = { title: 'Community results -- Dogfood' };

/**
 * The community tally.
 *
 * Gated by the API, not by this page: `apiOrNull` returns null on the 403 that
 * `Action.READ_TALLY` raises while results are unpublished, and the page renders
 * the closed state. An organizer gets the numbers during the window because the
 * same predicate says so -- there is no second rule here.
 *
 * The `caveat` is rendered prominently rather than in a footnote. An open-link
 * tally is a popularity signal, not a count of people, and a reader who is not
 * told that will assume otherwise.
 */
export default async function PublicResultsPage({
  params,
}: {
  params: Promise<{ slug: string }>;
}) {
  const { slug } = await params;

  const event = await apiOrNull<Event>(`/api/events/${slug}`);
  if (!event) notFound();

  const me = await getMe();
  const tally = await apiOrNull<Tally>(`/api/events/${slug}/voting/results`);

  return (
    <main className="shell">
      <section style={{ padding: '24px 0 4px' }}>
        <Link href={`/events/${slug}`} className="muted" style={{ fontSize: 13 }}>
          ← {event.name}
        </Link>
        <p className="eyebrow" style={{ marginTop: 10 }}>
          Community vote
        </p>
        <h1 style={{ fontSize: 30 }}>Results</h1>
      </section>

      {tally === null ? (
        <section className="panel">
          <h2 style={{ fontSize: 18, marginTop: 0 }}>Not published yet</h2>
          <p>
            {event.voting_open
              ? 'Voting is still open, and the totals are hidden until the organizers publish them.'
              : 'Voting has closed. The organizers have not published the totals yet.'}
          </p>
          <p className="muted" style={{ fontSize: 13, marginBottom: 0 }}>
            Results are hidden during the voting window on purpose: a tally you can
            watch move is a tally you can work out how to game.
            {event.voting_open && (
              <>
                {' '}
                <Link href={`/events/${slug}/vote`}>Cast your own vote →</Link>
              </>
            )}
          </p>
        </section>
      ) : (
        <>
          {tally.caveat && (
            <p className="notice">
              <strong>Read these numbers carefully.</strong> {tally.caveat}
            </p>
          )}

          {!tally.public && atLeast(me.role, 'organizer') && (
            <p className="notice bad">
              <strong>Organizer preview.</strong> These totals are not public yet. You
              are seeing them because you run this event.
            </p>
          )}

          <section className="panel">
            <div className="stat-row">
              <Stat label="Ballots cast" value={String(tally.total_voters)} />
              <Stat label="Projects" value={String(tally.rows.length)} />
              <Stat
                label="Method"
                value={tally.method === 'quadratic' ? 'Quadratic' : 'One vote each'}
              />
              <Stat label="Voting" value={tally.voting_open ? 'Open' : 'Closed'} />
            </div>
            {tally.method === 'quadratic' && (
              <p className="muted" style={{ fontSize: 13, marginBottom: 0 }}>
                <strong>Votes</strong> is total influence. <strong>Credits</strong> is
                what voters paid for it — <em>n</em> votes on one project costs{' '}
                <em>n²</em>. Two projects on the same number of votes can have cost
                their backers very different amounts, and that difference is the shape
                of the support rather than its size.
              </p>
            )}
          </section>

          <section className="panel">
            {tally.rows.length === 0 ? (
              <Empty>No projects have been entered in this event.</Empty>
            ) : (
              <div className="table-wrap">
                <table>
                  <thead>
                    <tr>
                      <th>#</th>
                      <th>Project</th>
                      <th>Team</th>
                      <th>Track</th>
                      <th>Votes</th>
                      <th>Backers</th>
                      {tally.method === 'quadratic' && <th>Credits</th>}
                    </tr>
                  </thead>
                  <tbody>
                    {tally.rows.map((row) => (
                      <tr key={row.submission_id}>
                        <td>
                          <strong>{row.rank}</strong>
                        </td>
                        <td>
                          <Link href={`/projects/${row.submission_id}`}>
                            {row.submission_name}
                          </Link>
                        </td>
                        <td className="muted">{row.team_name}</td>
                        <td className="muted">{row.track ?? '—'}</td>
                        <td>
                          <strong>{row.votes}</strong>
                        </td>
                        <td className="muted">{row.voters}</td>
                        {tally.method === 'quadratic' && (
                          <td className="muted">{row.credits}</td>
                        )}
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
            <p className="muted" style={{ fontSize: 13, marginBottom: 0 }}>
              Equal totals share a rank, and the next rank skips. Deciding a placing on
              a tie-break nobody published would be worse than showing the tie.
            </p>
          </section>

          <p className="muted" style={{ fontSize: 13 }}>
            This is the <strong>community</strong> vote. The judges&rsquo; scores are a
            separate result and stay with the organizers.
          </p>
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
