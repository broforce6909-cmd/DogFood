import Link from 'next/link';
import { notFound } from 'next/navigation';

import {
  archiveEventAction,
  createPrizeAction,
  createQuestionAction,
  createTrackAction,
  deletePrizeAction,
  deleteQuestionAction,
  deleteTrackAction,
  disqualifySubmissionAction,
  postAnnouncementAction,
  reinstateSubmissionAction,
  removeRegistrationAction,
  updateEventAction,
  updatePrizeAction,
} from '../../actions';
import { Empty, FormError } from '../../components';
import { apiOrNull } from '@/lib/api';
import { formatDateTime, untilDeadline } from '@/lib/format';
import { getMe } from '@/lib/session';
import {
  atLeast,
  type Announcement,
  type Event,
  type Page,
  type Registration,
  type Submission,
  type Team,
} from '@/lib/types';

// This page needs true whole-event counts (entered/draft/teams-without-a-
// project), not one page of them, so it asks for the pagination system's own
// maximum page size rather than paging through -- see app/pagination.py's
// `MAX_PER_PAGE`. Correct up through the exact scale this project's own load
// pass measured (200 submissions, ARCHITECTURE.md); a genuinely larger event
// would need a dedicated counts endpoint rather than a bigger page.
const OVERVIEW_PAGE_SIZE = 200;

export const dynamic = 'force-dynamic';

export async function generateMetadata({ params }: { params: Promise<{ slug: string }> }) {
  const { slug } = await params;
  const event = await apiOrNull<Event>(`/api/events/${slug}`);
  return { title: event ? `Manage: ${event.name} -- Dogfood` : 'Manage event -- Dogfood' };
}

/** `datetime-local` wants `YYYY-MM-DDTHH:mm`; the API speaks ISO with a zone. */
function forInput(iso: string | null): string {
  return iso ? iso.slice(0, 16) : '';
}

export default async function ManageEventPage({
  params,
  searchParams,
}: {
  params: Promise<{ slug: string }>;
  searchParams: Promise<{ error?: string }>;
}) {
  const me = await getMe();
  if (!atLeast(me.role, 'organizer')) notFound();

  const { slug } = await params;
  const { error } = await searchParams;

  const event = await apiOrNull<Event>(`/api/events/${slug}`);
  if (!event) notFound();

  const [submissionsPage, teamsPage, registrationsPage, announcements] = await Promise.all([
    apiOrNull<Page<Submission>>(
      `/api/events/${slug}/submissions?per_page=${OVERVIEW_PAGE_SIZE}`,
    ),
    apiOrNull<Page<Team>>(`/api/events/${slug}/teams?per_page=${OVERVIEW_PAGE_SIZE}`),
    apiOrNull<Page<Registration>>(
      `/api/events/${slug}/registrations?per_page=${OVERVIEW_PAGE_SIZE}`,
    ),
    apiOrNull<Announcement[]>(`/api/events/${slug}/announcements`),
  ]);
  const submissions = submissionsPage?.items ?? [];
  const teams = teamsPage?.items ?? [];
  const registrations = registrationsPage?.items ?? [];

  const entered = submissions.filter((s) => s.status === 'submitted');
  const drafts = submissions.filter((s) => s.status === 'draft');
  const teamsWithoutProject = teams.filter((team) => !team.submission_id);

  return (
    <main className="shell">
      <p className="eyebrow">
        <Link href="/organizer">Organizer</Link> · {event.slug}
      </p>
      <div className="spread">
        <h1>{event.name}</h1>
        <div className="row">
          {event.is_published ? (
            <span className="pill ok">Published</span>
          ) : (
            <span className="pill warn">Draft</span>
          )}
          <Link className="button" href={`/events/${event.slug}`}>
            Public page
          </Link>
          <Link className="button primary" href={`/organizer/${event.slug}/judging`}>
            Judging
          </Link>
          <Link className="button" href={`/organizer/${event.slug}/audit`}>
            Audit log
          </Link>
          <Link className="button" href={`/organizer/${event.slug}/integrations`}>
            Integrations
          </Link>
        </div>
      </div>
      <FormError message={error} />

      {event.archived_at && (
        <div className="notice" style={{ margin: '12px 0' }}>
          <strong>This event is archived.</strong> It is frozen and read-only: its gallery,
          results and certificates still work, but nothing here can be changed until it is
          unarchived (archived {formatDateTime(event.archived_at)}).
          <form action={archiveEventAction} style={{ marginTop: 8 }}>
            <input type="hidden" name="slug" value={event.slug} />
            <input type="hidden" name="verb" value="unarchive" />
            <button type="submit" className="button small">
              Unarchive
            </button>
          </form>
        </div>
      )}

      <div className="grid" style={{ margin: '18px 0 28px' }}>
        <div className="card">
          <p className="eyebrow">Entered</p>
          <h2 style={{ margin: 0 }}>{entered.length}</h2>
        </div>
        <div className="card">
          <p className="eyebrow">Still drafting</p>
          <h2 style={{ margin: 0 }}>{drafts.length}</h2>
        </div>
        <div className="card">
          <p className="eyebrow">Teams</p>
          <h2 style={{ margin: 0 }}>{teams?.length ?? 0}</h2>
        </div>
        <div className="card">
          <p className="eyebrow">Deadline</p>
          <h3 style={{ margin: 0 }}>{untilDeadline(event.submission_deadline)}</h3>
          <p className="muted small" style={{ margin: '4px 0 0' }}>
            {formatDateTime(event.submission_deadline)}
          </p>
        </div>
      </div>

      {/* -- announcements ------------------------------------------------ */}

      <section className="panel">
        <h2>Announcements</h2>
        <form action={postAnnouncementAction} className="stack" style={{ marginBottom: 16 }}>
          <input type="hidden" name="slug" value={event.slug} />
          <div className="field">
            <label htmlFor="announcement-title">Title</label>
            <input id="announcement-title" name="title" required maxLength={200} />
          </div>
          <div className="field">
            <label htmlFor="announcement-body">Message</label>
            <textarea id="announcement-body" name="body" required rows={3} />
          </div>
          <label className="row" style={{ alignItems: 'center', gap: 6 }}>
            <input type="checkbox" name="visible_to_visitors" />
            Also show to signed-out visitors
          </label>
          <button type="submit" className="button primary">
            Post
          </button>
        </form>
        {!announcements || announcements.length === 0 ? (
          <Empty>No announcements posted yet.</Empty>
        ) : (
          <div className="stack" style={{ gap: 12 }}>
            {announcements.map((announcement) => (
              <div key={announcement.id} className="table-wrap" style={{ padding: '10px 0' }}>
                <div className="spread">
                  <p style={{ margin: 0, fontWeight: 600 }}>{announcement.title}</p>
                  <span className={`pill ${announcement.visible_to_visitors ? 'ok' : ''}`}>
                    {announcement.visible_to_visitors ? 'everyone' : 'participants'}
                  </span>
                </div>
                <p className="muted small" style={{ margin: '2px 0 6px' }}>
                  {formatDateTime(announcement.posted_at)}
                  {announcement.posted_by_display_name &&
                    ` · ${announcement.posted_by_display_name}`}
                </p>
                <p style={{ margin: 0 }}>{announcement.body}</p>
              </div>
            ))}
          </div>
        )}
      </section>

      {/* -- submissions ------------------------------------------------- */}

      <section className="panel">
        <h2>Submissions</h2>
        <p className="muted small">
          Drafts are listed here and nowhere else. The gallery shows entered projects only, and
          that is a rule of the API rather than of this page.
        </p>
        {(submissions ?? []).length === 0 ? (
          <Empty>Nothing submitted yet.</Empty>
        ) : (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Project</th>
                  <th>Team</th>
                  <th>Track</th>
                  <th>Status</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {(submissions ?? []).map((submission) => (
                  <tr key={submission.id}>
                    <td>
                      <Link href={`/projects/${submission.id}`}>{submission.name}</Link>
                    </td>
                    <td className="muted">{submission.team_name}</td>
                    <td className="muted">{submission.track?.name ?? '--'}</td>
                    <td>
                      <span
                        className={`pill ${
                          submission.status === 'submitted'
                            ? 'ok'
                            : submission.status === 'disqualified'
                              ? 'bad'
                              : 'warn'
                        }`}
                      >
                        {submission.status}
                      </span>
                    </td>
                    <td>
                      {submission.status === 'disqualified' ? (
                        <form action={reinstateSubmissionAction} className="row">
                          <input type="hidden" name="slug" value={event.slug} />
                          <input type="hidden" name="submission_id" value={submission.id} />
                          <input
                            name="reason"
                            required
                            placeholder="reason (recorded in the audit log)"
                            style={{ width: 240 }}
                          />
                          <button type="submit" className="button small">
                            Reinstate
                          </button>
                        </form>
                      ) : (
                        <form action={disqualifySubmissionAction} className="row">
                          <input type="hidden" name="slug" value={event.slug} />
                          <input type="hidden" name="submission_id" value={submission.id} />
                          <input
                            name="reason"
                            required
                            placeholder="reason (recorded in the audit log)"
                            style={{ width: 240 }}
                          />
                          <button type="submit" className="quiet small">
                            Disqualify
                          </button>
                        </form>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        {teamsWithoutProject.length > 0 && (
          <p className="muted small" style={{ marginTop: 12 }}>
            {teamsWithoutProject.length} team{teamsWithoutProject.length === 1 ? '' : 's'} with no
            project: {teamsWithoutProject.map((team) => team.name).join(', ')}.
          </p>
        )}
      </section>

      {/* -- registrations ------------------------------------------------ */}

      <section className="panel" style={{ marginTop: 16 }}>
        <h2>Registrations</h2>
        <p className="muted small">
          Who said they were coming, ahead of forming or joining a team -- registering is additive
          and never required to enter a project.
        </p>
        {registrations.length === 0 ? (
          <Empty>Nobody has registered yet.</Empty>
        ) : (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Name</th>
                  <th>Email</th>
                  <th>Discord</th>
                  <th>Team leader?</th>
                  <th>Has team</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {registrations.map((registration) => (
                  <tr key={registration.id}>
                    <td>{registration.user_display_name}</td>
                    <td className="muted">{registration.email}</td>
                    <td className="muted">{registration.discord_username}</td>
                    <td className="muted">
                      {registration.is_team_leader
                        ? registration.leader_name ?? 'yes'
                        : 'no'}
                    </td>
                    <td className="muted">{registration.has_team ? 'yes' : 'no'}</td>
                    <td>
                      <form action={removeRegistrationAction} className="row">
                        <input type="hidden" name="event_slug" value={event.slug} />
                        <input type="hidden" name="registration_id" value={registration.id} />
                        <input
                          name="reason"
                          required
                          placeholder="reason (recorded in the audit log)"
                          style={{ width: 240 }}
                        />
                        <button type="submit" className="quiet small">
                          Remove
                        </button>
                      </form>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>

      {/* -- tracks ------------------------------------------------------- */}

      <section className="panel" style={{ marginTop: 16 }}>
        <h2>Tracks</h2>
        {event.tracks.length > 0 && (
          <div className="table-wrap">
            <table>
              <tbody>
                {event.tracks.map((track) => (
                  <tr key={track.id}>
                    <td>{track.name}</td>
                    <td className="muted small">{track.key}</td>
                    <td style={{ textAlign: 'right' }}>
                      <form action={deleteTrackAction}>
                        <input type="hidden" name="event_slug" value={event.slug} />
                        <input type="hidden" name="track_id" value={track.id} />
                        <button className="quiet small" type="submit">
                          Delete
                        </button>
                      </form>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        <form action={createTrackAction} className="field-row" style={{ marginTop: 14 }}>
          <input type="hidden" name="event_slug" value={event.slug} />
          <div>
            <label htmlFor="track-name">Name</label>
            <input id="track-name" name="name" required />
          </div>
          <div>
            <label htmlFor="track-key">Key</label>
            <input id="track-key" name="key" required pattern="[a-z0-9]+(-[a-z0-9]+)*" />
          </div>
          <div style={{ alignSelf: 'end' }}>
            <button type="submit">Add track</button>
          </div>
        </form>
      </section>

      {/* -- prizes ------------------------------------------------------- */}

      <section className="panel" style={{ marginTop: 16 }}>
        <h2>Prizes</h2>
        {event.prizes.length > 0 && (
          <div className="table-wrap">
            <table>
              <tbody>
                {event.prizes.map((prize) => (
                  <tr key={prize.id}>
                    <td style={{ padding: 0 }}>
                      <form
                        action={updatePrizeAction}
                        className="field-row"
                        style={{ padding: '8px 0' }}
                      >
                        <input type="hidden" name="event_slug" value={event.slug} />
                        <input type="hidden" name="prize_id" value={prize.id} />
                        {/* The PATCH route replaces the whole row (`PrizeIn` is one
                            shape for create and update alike) -- carried through
                            unedited here so a plain rename cannot silently wipe a
                            description this compact form has no field for. */}
                        <input type="hidden" name="description" value={prize.description ?? ''} />
                        <input
                          name="position"
                          type="number"
                          defaultValue={prize.position}
                          style={{ width: 64 }}
                          aria-label="Tier / position"
                        />
                        <input name="title" defaultValue={prize.title} required />
                        <input
                          name="value"
                          defaultValue={prize.value ?? ''}
                          placeholder="$800"
                        />
                        <select name="track_id" defaultValue={prize.track_id ?? ''}>
                          <option value="">All tracks</option>
                          {event.tracks.map((track) => (
                            <option key={track.id} value={track.id}>
                              {track.name}
                            </option>
                          ))}
                        </select>
                        <button className="quiet small" type="submit">
                          Save
                        </button>
                      </form>
                    </td>
                    <td style={{ textAlign: 'right' }}>
                      <form action={deletePrizeAction}>
                        <input type="hidden" name="event_slug" value={event.slug} />
                        <input type="hidden" name="prize_id" value={prize.id} />
                        <button className="quiet small" type="submit">
                          Delete
                        </button>
                      </form>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        <form action={createPrizeAction} className="field-row" style={{ marginTop: 14 }}>
          <input type="hidden" name="event_slug" value={event.slug} />
          <div>
            <label htmlFor="prize-position">Tier</label>
            <input
              id="prize-position"
              name="position"
              type="number"
              defaultValue={event.prizes.length}
              style={{ width: 64 }}
            />
          </div>
          <div>
            <label htmlFor="prize-title">Title</label>
            <input id="prize-title" name="title" required />
          </div>
          <div>
            <label htmlFor="prize-value">Value</label>
            <input id="prize-value" name="value" placeholder="$800" />
          </div>
          <div>
            <label htmlFor="prize-track">Track</label>
            <select id="prize-track" name="track_id" defaultValue="">
              <option value="">All tracks</option>
              {event.tracks.map((track) => (
                <option key={track.id} value={track.id}>
                  {track.name}
                </option>
              ))}
            </select>
          </div>
          <div style={{ alignSelf: 'end' }}>
            <button type="submit">Add prize</button>
          </div>
        </form>
      </section>

      {/* -- questions ---------------------------------------------------- */}

      <section className="panel" style={{ marginTop: 16 }}>
        <h2>Custom questions</h2>
        <p className="muted small">
          Asked of every project in this event. A required question blocks submission until it is
          answered. Deleting one deletes its answers.
        </p>
        {event.questions.length > 0 && (
          <div className="table-wrap">
            <table>
              <tbody>
                {event.questions.map((question) => (
                  <tr key={question.id}>
                    <td>{question.prompt}</td>
                    <td className="muted small">{question.kind}</td>
                    <td>{question.required && <span className="pill warn">Required</span>}</td>
                    <td style={{ textAlign: 'right' }}>
                      <form action={deleteQuestionAction}>
                        <input type="hidden" name="event_slug" value={event.slug} />
                        <input type="hidden" name="question_id" value={question.id} />
                        <button className="quiet small" type="submit">
                          Delete
                        </button>
                      </form>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        <form action={createQuestionAction} className="stack" style={{ marginTop: 14 }}>
          <input type="hidden" name="event_slug" value={event.slug} />
          <div className="field">
            <label htmlFor="question-prompt">Prompt</label>
            <input id="question-prompt" name="prompt" required />
          </div>
          <div className="field-row">
            <div>
              <label htmlFor="question-kind">Kind</label>
              <select id="question-kind" name="kind" defaultValue="textarea">
                <option value="textarea">Long text</option>
                <option value="text">Short text</option>
                <option value="url">URL</option>
                <option value="checkbox">Checkbox</option>
              </select>
            </div>
            <div style={{ alignSelf: 'end' }}>
              <label className="row small" style={{ color: 'inherit' }}>
                <input name="required" type="checkbox" style={{ width: 'auto' }} />
                Required to submit
              </label>
            </div>
            <div style={{ alignSelf: 'end' }}>
              <button type="submit">Add question</button>
            </div>
          </div>
        </form>
      </section>

      {/* -- settings ----------------------------------------------------- */}

      <section className="panel" style={{ marginTop: 16 }}>
        <h2>Settings</h2>
        <form action={updateEventAction} className="stack">
          <input type="hidden" name="slug" value={event.slug} />
          <div className="field">
            <label htmlFor="event-name">Name</label>
            <input id="event-name" name="name" defaultValue={event.name} required />
          </div>
          <div className="field">
            <label htmlFor="event-tagline">Tagline</label>
            <input id="event-tagline" name="tagline" defaultValue={event.tagline ?? ''} />
          </div>
          <div className="field">
            <label htmlFor="event-description">Description</label>
            <textarea
              id="event-description"
              name="description"
              defaultValue={event.description ?? ''}
            />
          </div>
          <div className="field-row">
            <div>
              <label htmlFor="starts_at">Starts</label>
              <input
                id="starts_at"
                name="starts_at"
                type="datetime-local"
                defaultValue={forInput(event.starts_at)}
                required
              />
            </div>
            <div>
              <label htmlFor="ends_at">Ends</label>
              <input
                id="ends_at"
                name="ends_at"
                type="datetime-local"
                defaultValue={forInput(event.ends_at)}
                required
              />
            </div>
          </div>
          <div className="field-row">
            <div>
              <label htmlFor="registration_opens_at">Registration opens</label>
              <input
                id="registration_opens_at"
                name="registration_opens_at"
                type="datetime-local"
                defaultValue={forInput(event.registration_opens_at)}
                required
              />
            </div>
            <div>
              <label htmlFor="submission_opens_at">Submissions open</label>
              <input
                id="submission_opens_at"
                name="submission_opens_at"
                type="datetime-local"
                defaultValue={forInput(event.submission_opens_at)}
                required
              />
            </div>
            <div>
              <label htmlFor="submission_deadline">Deadline</label>
              <input
                id="submission_deadline"
                name="submission_deadline"
                type="datetime-local"
                defaultValue={forInput(event.submission_deadline)}
                required
              />
            </div>
          </div>
          <div className="field-row">
            <div>
              <label htmlFor="max_team_size">Maximum team size</label>
              <input
                id="max_team_size"
                name="max_team_size"
                type="number"
                min={1}
                defaultValue={event.max_team_size}
              />
            </div>
            <div style={{ alignSelf: 'end' }}>
              <label className="row small" style={{ color: 'inherit' }}>
                <input
                  name="is_published"
                  type="checkbox"
                  defaultChecked={event.is_published}
                  style={{ width: 'auto' }}
                />
                Published
              </label>
            </div>
          </div>

          <h3 style={{ margin: '18px 0 0', fontSize: 15 }}>Voting</h3>
          <p className="muted small" style={{ marginTop: 2 }}>
            How the community vote works for this event. Quadratic voting and
            open-link/email-gated access are both real options here, not just seed
            data -- this form is the only place to turn them on.
          </p>
          <div className="field-row">
            <div>
              <label htmlFor="voting_access">Who may vote</label>
              <select id="voting_access" name="voting_access" defaultValue={event.voting_access}>
                <option value="authenticated">Signed-in accounts only</option>
                <option value="open_link">Open link (no account)</option>
                <option value="email_gated">Email address (unverified)</option>
              </select>
            </div>
            <div>
              <label htmlFor="voting_method">Method</label>
              <select id="voting_method" name="voting_method" defaultValue={event.voting_method}>
                <option value="single">Single: pick up to N projects</option>
                <option value="quadratic">Quadratic: n votes cost n² credits</option>
              </select>
            </div>
          </div>
          <div className="field-row">
            <div>
              <label htmlFor="vote_credits">Quadratic credits</label>
              <input
                id="vote_credits"
                name="vote_credits"
                type="number"
                min={1}
                defaultValue={event.vote_credits}
              />
            </div>
            <div>
              <label htmlFor="votes_per_voter">Single-vote: projects per voter</label>
              <input
                id="votes_per_voter"
                name="votes_per_voter"
                type="number"
                min={1}
                defaultValue={event.votes_per_voter}
              />
            </div>
          </div>
          <div className="field-row">
            <div>
              <label htmlFor="voting_opens_at">Voting opens</label>
              <input
                id="voting_opens_at"
                name="voting_opens_at"
                type="datetime-local"
                defaultValue={forInput(event.voting_opens_at)}
              />
            </div>
            <div>
              <label htmlFor="voting_closes_at">Voting closes</label>
              <input
                id="voting_closes_at"
                name="voting_closes_at"
                type="datetime-local"
                defaultValue={forInput(event.voting_closes_at)}
              />
            </div>
            <div style={{ alignSelf: 'end' }}>
              <label className="row small" style={{ color: 'inherit' }}>
                <input
                  name="comments_enabled"
                  type="checkbox"
                  defaultChecked={event.comments_enabled}
                  style={{ width: 'auto' }}
                />
                Comments enabled
              </label>
            </div>
          </div>

          <h3 style={{ margin: '18px 0 0', fontSize: 15 }}>Pairwise judging</h3>
          <p className="muted small" style={{ marginTop: 2 }}>
            The Gavel-style alternative to the scored rubric: judges see two
            projects and pick the better one, and a ranking is recovered by
            fitting Bradley-Terry over every comparison. Additive -- turning
            this on does not disable the rubric, and an event may run both.
          </p>
          <label className="row small" style={{ color: 'inherit' }}>
            <input
              name="pairwise_enabled"
              type="checkbox"
              defaultChecked={event.pairwise_enabled}
              style={{ width: 'auto' }}
            />
            Enable pairwise judging for this event
          </label>

          <h3 style={{ margin: '18px 0 0', fontSize: 15 }}>Winners and community vote</h3>
          <p className="muted small" style={{ marginTop: 2 }}>
            The top N ranks by judge score become outright winners. The next M
            ranks move into a separate community-voting round scoped to just
            those projects. Both 0 (the default) means this event publishes a
            plain judge ranking with no split.
          </p>
          <div className="field-row">
            <div>
              <label htmlFor="winner_slots">Outright winners (N)</label>
              <input
                id="winner_slots"
                name="winner_slots"
                type="number"
                min={0}
                defaultValue={event.winner_slots}
              />
            </div>
            <div>
              <label htmlFor="community_vote_slots">Community-vote finalists (M)</label>
              <input
                id="community_vote_slots"
                name="community_vote_slots"
                type="number"
                min={0}
                defaultValue={event.community_vote_slots}
              />
            </div>
          </div>

          <button className="primary" type="submit" style={{ marginTop: 14 }}>
            Save event
          </button>
        </form>
      </section>

      {!event.archived_at && (
        <section className="panel" style={{ marginTop: 18 }}>
          <h2 style={{ fontSize: 18, marginTop: 0 }}>Archive this event</h2>
          <p className="muted" style={{ fontSize: 13 }}>
            Freezes the event and takes it out of the default event lists. Nothing is deleted:
            the gallery, results and certificates keep working for anyone with the link. While
            it is archived nobody can change anything on it -- registration, submissions,
            scoring, voting, settings -- until you unarchive it. It is also the safe
            alternative to deleting an event.
          </p>
          <form action={archiveEventAction}>
            <input type="hidden" name="slug" value={event.slug} />
            <input type="hidden" name="verb" value="archive" />
            <button type="submit" className="button">
              Archive this event
            </button>
          </form>
        </section>
      )}
    </main>
  );
}
