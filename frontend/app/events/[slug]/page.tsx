import Link from 'next/link';
import { notFound } from 'next/navigation';

import { createTeamAction, registerForEventAction } from '../../actions';
import { FormError, Pagination } from '../../components';
import { apiOrNull } from '@/lib/api';
import { formatDateTime, formatRange, untilDeadline } from '@/lib/format';
import { getMe } from '@/lib/session';
import type { Announcement, Event, Page, Registration, Team } from '@/lib/types';

export const dynamic = 'force-dynamic';

export async function generateMetadata({ params }: { params: Promise<{ slug: string }> }) {
  const { slug } = await params;
  const event = await apiOrNull<Event>(`/api/events/${slug}`);
  return { title: event ? `${event.name} -- Dogfood` : 'Event -- Dogfood' };
}

export default async function EventPage({
  params,
  searchParams,
}: {
  params: Promise<{ slug: string }>;
  searchParams: Promise<{ error?: string; page?: string }>;
}) {
  const { slug } = await params;
  const { error, page: pageParam } = await searchParams;
  const page = Number(pageParam) || 1;

  // An unpublished event answers 404 to anybody who is not staff, so this is
  // both "no such event" and "not for you" -- deliberately indistinguishable.
  const event = await apiOrNull<Event>(`/api/events/${slug}`);
  if (!event) notFound();

  const [me, teams, myTeam, myRegistration, announcements] = await Promise.all([
    getMe(),
    apiOrNull<Page<Team>>(`/api/events/${slug}/teams?page=${page}`),
    // A dedicated lookup, not a scan of the (now paged) list above: "am I on a
    // team" must not depend on which page happens to include me.
    apiOrNull<Team | null>(`/api/events/${slug}/teams/mine`),
    apiOrNull<Registration | null>(`/api/events/${slug}/registrations/me`),
    // The API's own visibility rule (`visible_to_visitors`) already filters
    // this list per caller -- a visitor gets back only what they are meant
    // to see, so this page renders whatever comes back with no second check.
    apiOrNull<Announcement[]>(`/api/events/${slug}/announcements`),
  ]);

  return (
    <main className="shell">
      <p className="eyebrow">Event</p>
      <div className="spread">
        <h1>{event.name}</h1>
        {!event.is_published && <span className="pill warn">Unpublished</span>}
        {event.archived_at && <span className="pill">Archived</span>}
      </div>
      {event.archived_at && (
        <p className="notice" style={{ maxWidth: 640 }}>
          This event has ended and is archived. The gallery and results below are a
          permanent record; registration, submissions and voting are closed.
        </p>
      )}
      <p className="muted" style={{ maxWidth: 640 }}>
        {event.tagline}
      </p>

      <div className="row" style={{ margin: '18px 0 28px' }}>
        {event.voting_open && (
          <Link className="button primary" href={`/events/${event.slug}/vote`}>
            Vote
          </Link>
        )}
        {(event.results_public || event.voting_opens_at) && (
          <Link className="button" href={`/events/${event.slug}/results`}>
            Community results
          </Link>
        )}
        {event.results_public && (
          <Link className="button" href={`/events/${event.slug}/leaderboard`}>
            Leaderboard
          </Link>
        )}
        <Link className="button primary" href={`/events/${event.slug}/gallery`}>
          Browse projects
        </Link>
        {event.submissions_open ? (
          <span className="pill ok">Submissions open · closes {untilDeadline(event.submission_deadline)}</span>
        ) : (
          <span className="pill warn">
            Submissions closed · {untilDeadline(event.submission_deadline)}
          </span>
        )}
      </div>

      <div className="grid" style={{ gridTemplateColumns: 'minmax(0, 2fr) minmax(260px, 1fr)' }}>
        <div className="stack">
          {announcements && announcements.length > 0 && (
            <section className="panel">
              <h2>Announcements</h2>
              <div className="stack" style={{ gap: 14 }}>
                {announcements.map((announcement) => (
                  <div key={announcement.id}>
                    <p style={{ margin: 0, fontWeight: 600 }}>{announcement.title}</p>
                    <p className="muted small" style={{ margin: '2px 0 6px' }}>
                      {formatDateTime(announcement.posted_at)}
                      {announcement.posted_by_display_name &&
                        ` · ${announcement.posted_by_display_name}`}
                    </p>
                    <p className="prose" style={{ margin: 0 }}>
                      {announcement.body}
                    </p>
                  </div>
                ))}
              </div>
            </section>
          )}

          {event.description && (
            <section className="panel">
              <h2>About</h2>
              <p className="prose muted" style={{ margin: 0 }}>
                {event.description}
              </p>
            </section>
          )}

          {event.tracks.length > 0 && (
            <section className="panel">
              <h2>Tracks</h2>
              <div className="table-wrap">
                <table>
                  <tbody>
                    {event.tracks.map((track) => (
                      <tr key={track.id}>
                        <td style={{ width: '32%' }}>
                          <Link href={`/events/${event.slug}/gallery?track=${track.key}`}>
                            {track.name}
                          </Link>
                        </td>
                        <td className="muted">{track.description ?? ''}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </section>
          )}

          {event.prizes.length > 0 && (
            <section className="panel">
              <h2>Prizes</h2>
              <div className="table-wrap">
                <table>
                  <tbody>
                    {event.prizes.map((prize) => {
                      const track = event.tracks.find((t) => t.id === prize.track_id);
                      return (
                        <tr key={prize.id}>
                          <td style={{ width: '32%' }}>
                            {prize.title}
                            {track && (
                              <>
                                {' '}
                                <span className="pill">{track.name}</span>
                              </>
                            )}
                          </td>
                          <td className="muted">{prize.description ?? ''}</td>
                          <td style={{ textAlign: 'right', whiteSpace: 'nowrap' }}>
                            {prize.value ?? ''}
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            </section>
          )}

          {event.questions.length > 0 && (
            <section className="panel">
              <h2>Submission questions</h2>
              <p className="muted small">
                Asked of every project in this event, on top of the standard fields.
              </p>
              <ol className="muted small" style={{ margin: 0, paddingLeft: 18 }}>
                {event.questions.map((question) => (
                  <li key={question.id} style={{ marginBottom: 4 }}>
                    {question.prompt}
                    {question.required && <span className="pill warn" style={{ marginLeft: 8 }}>Required</span>}
                  </li>
                ))}
              </ol>
            </section>
          )}
        </div>

        <aside className="stack">
          <section className="panel">
            <h3>Dates</h3>
            <dl className="meta small">
              <dt>Event</dt>
              <dd>{formatRange(event.starts_at, event.ends_at)}</dd>
              <dt>Registration</dt>
              <dd>{formatDateTime(event.registration_opens_at)}</dd>
              <dt>Submissions</dt>
              <dd>{formatDateTime(event.submission_opens_at)}</dd>
              <dt>Deadline</dt>
              <dd>
                <strong>{formatDateTime(event.submission_deadline)}</strong>
              </dd>
              {event.judging_opens_at && (
                <>
                  <dt>Judging</dt>
                  <dd>{formatDateTime(event.judging_opens_at)}</dd>
                </>
              )}
              <dt>Team size</dt>
              <dd>up to {event.max_team_size}</dd>
            </dl>
          </section>

          <section className="panel">
            <h3>Registration</h3>
            {!me.authenticated ? (
              <p className="muted small">
                <Link href={`/login?next=/events/${event.slug}`}>Sign in</Link> to register.
              </p>
            ) : myRegistration ? (
              <p className="small" style={{ margin: 0 }}>
                You are registered as <strong>{myRegistration.discord_username}</strong> on
                Discord
                {myRegistration.is_team_leader && ' -- registered as a team leader'}.
              </p>
            ) : event.registration_open ? (
              <form action={registerForEventAction} className="stack">
                <input type="hidden" name="event_slug" value={event.slug} />
                <div className="field">
                  <label htmlFor="discord_username">Discord username</label>
                  <input id="discord_username" name="discord_username" type="text" required minLength={2} />
                </div>
                <div className="field">
                  <label htmlFor="reg-email">Email (optional -- defaults to your account email)</label>
                  <input id="reg-email" name="email" type="email" />
                </div>
                <label className="row small" style={{ color: 'inherit' }}>
                  <input id="is_team_leader" name="is_team_leader" type="checkbox" style={{ width: 'auto' }} />
                  I intend to lead a team
                </label>
                <div className="field">
                  <label htmlFor="leader_name">Leader name</label>
                  <input id="leader_name" name="leader_name" type="text" />
                  <p className="muted small" style={{ margin: '2px 0 0' }}>
                    Only used if &ldquo;I intend to lead a team&rdquo; is checked.
                  </p>
                </div>
                <button className="primary" type="submit">
                  Register
                </button>
              </form>
            ) : (
              <p className="muted small" style={{ margin: 0 }}>
                Registration for this event has closed.
              </p>
            )}
          </section>

          <section className="panel">
            <h3>Your team</h3>
            <FormError message={error} />
            {!me.authenticated ? (
              <p className="muted small">
                <Link href={`/login?next=/events/${event.slug}`}>Sign in</Link> to form a team.
              </p>
            ) : myTeam ? (
              <p className="small" style={{ margin: 0 }}>
                You are on <Link href={`/teams/${myTeam.id}`}>{myTeam.name}</Link>.
              </p>
            ) : event.registration_open ? (
              <form action={createTeamAction} className="stack">
                <input type="hidden" name="event_slug" value={event.slug} />
                <div className="field">
                  <label htmlFor="team-name">Team name</label>
                  <input id="team-name" name="name" type="text" required minLength={2} />
                </div>
                <button className="primary" type="submit">
                  Create team
                </button>
                <p className="muted small" style={{ margin: 0 }}>
                  Or join an existing one with an invite link.
                </p>
              </form>
            ) : (
              <p className="muted small" style={{ margin: 0 }}>
                Registration for this event has closed.
              </p>
            )}
          </section>

          {teams && teams.items.length > 0 && (
            <section className="panel">
              <h3>Teams</h3>
              <ul className="muted small" style={{ margin: 0, paddingLeft: 18 }}>
                {teams.items.map((team) => (
                  <li key={team.id}>
                    <Link href={`/teams/${team.id}`}>{team.name}</Link>{' '}
                    <span className="muted">({team.members.length})</span>
                  </li>
                ))}
              </ul>
              <Pagination
                page={teams.page}
                pages={teams.pages}
                total={teams.total}
                basePath={`/events/${slug}`}
                noun="team"
              />
            </section>
          )}
        </aside>
      </div>
    </main>
  );
}
