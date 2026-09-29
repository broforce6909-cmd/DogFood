import Link from 'next/link';
import { notFound } from 'next/navigation';

import {
  createCriterionAction,
  deleteCriterionAction,
  inviteJudgeAction,
  publishTallyAction,
  runAssignmentAction,
  setJudgeActiveAction,
} from '../../../actions';
import { Empty } from '../../../components';
import { api, PUBLIC_BASE } from '@/lib/api';
import { formatDateTime } from '@/lib/format';
import { getMe } from '@/lib/session';
import { atLeast, type Criterion, type Event, type Judge, type Progress } from '@/lib/types';

export const dynamic = 'force-dynamic';

export const metadata = { title: 'Judging -- Dogfood' };

/**
 * The organizer's judging console: rubric, judges, assignment, live progress.
 *
 * The question this page exists to answer is "who has not started", because that
 * is the one that predicts whether judging lands on time. It is therefore the
 * first thing on the page, not a number buried in a table.
 */
export default async function OrganizerJudgingPage({
  params,
  searchParams,
}: {
  params: Promise<{ slug: string }>;
  searchParams: Promise<{
    error?: string;
    assigned?: string;
    spread?: string;
    short?: string;
    dry?: string;
  }>;
}) {
  const { slug } = await params;
  const query = await searchParams;

  const me = await getMe();
  // Hiding the page is a courtesy; every request it makes would be refused too.
  if (!atLeast(me.role, 'organizer')) notFound();

  const [event, progress, criteria, judges] = await Promise.all([
    api<Event>(`/api/events/${slug}`),
    api<Progress>(`/api/events/${slug}/judging/progress`),
    api<Criterion[]>(`/api/events/${slug}/criteria`),
    api<Judge[]>(`/api/events/${slug}/judges`),
  ]);

  const weightTotal = criteria.reduce((sum, c) => sum + c.weight, 0);

  return (
    <main className="shell">
      <section style={{ padding: '24px 0 4px' }}>
        <Link href={`/organizer/${slug}`} className="muted" style={{ fontSize: 13 }}>
          ← {event.name}
        </Link>
        <p className="eyebrow" style={{ marginTop: 10 }}>
          Judging
        </p>
        <h1 style={{ fontSize: 30 }}>{event.name}</h1>
        <p className="muted">
          {progress.judging_open ? (
            <>
              Judging is <strong>open</strong>
              {progress.judging_closes_at && ` until ${formatDateTime(progress.judging_closes_at)}`}
            </>
          ) : progress.judging_opens_at ? (
            <>Judging opens {formatDateTime(progress.judging_opens_at)}</>
          ) : (
            <>
              Judging has not been scheduled. Set the judging window on{' '}
              <Link href={`/organizer/${slug}`}>the event page</Link> — until then, no
              judge can score anything.
            </>
          )}
        </p>
        <div className="row">
          <Link className="button" href={`/organizer/${slug}/judging/calibration`}>
            Judge calibration
          </Link>
        </div>
      </section>

      {query.error && <p className="notice bad">{query.error}</p>}
      {query.assigned && (
        <p className="notice">
          {query.dry ? 'Dry run: ' : ''}
          {query.assigned} ballot(s) {query.dry ? 'would be' : ''} assigned, judge load spread{' '}
          {query.spread}
          {query.short !== '0' && `, ${query.short} submission(s) short of the target`}.
        </p>
      )}

      {/* ---------------------------------------------------------------- */}
      <section className="panel">
        <h2 style={{ fontSize: 20, marginTop: 0 }}>Progress</h2>
        <div className="stat-row">
          <Stat label="Ballots in" value={`${progress.assignments_complete}/${progress.assignments_total}`} />
          <Stat label="Complete" value={`${progress.percent_complete}%`} />
          <Stat
            label="Projects fully reviewed"
            value={`${progress.submissions_fully_reviewed}/${progress.submissions_total}`}
          />
          <Stat label="Judges" value={String(progress.judges.length)} />
        </div>

        {progress.not_started.length > 0 ? (
          <p className="notice bad" style={{ marginBottom: 0 }}>
            <strong>Not started:</strong> {progress.not_started.join(', ')}
          </p>
        ) : progress.assignments_total > 0 ? (
          <p className="notice" style={{ marginBottom: 0 }}>
            Every judge has started.
          </p>
        ) : null}

        {progress.judges.length > 0 && (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Judge</th>
                  <th>Track</th>
                  <th>Assigned</th>
                  <th>Complete</th>
                  <th>In progress</th>
                  <th>Not started</th>
                </tr>
              </thead>
              <tbody>
                {progress.judges.map((j) => (
                  <tr key={j.judge_id}>
                    <td>{j.judge_name}</td>
                    <td className="muted">{j.track ?? 'all tracks'}</td>
                    <td>{j.assigned}</td>
                    <td>{j.complete}</td>
                    <td>{j.in_progress}</td>
                    <td>{j.pending}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>

      {/* ---------------------------------------------------------------- */}
      <section className="panel">
        <h2 style={{ fontSize: 20, marginTop: 0 }}>Rubric</h2>
        <p className="muted" style={{ fontSize: 13 }}>
          Weights are relative: 1 and 2 mean the same as 10 and 20. Editing a weight
          re-ranks from now on and never rewrites a ballot already cast.
          {criteria.length > 0 && ` Current total: ${weightTotal}.`}
        </p>

        {criteria.length === 0 ? (
          <Empty>No criteria yet. Judges cannot score until there is a rubric.</Empty>
        ) : (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Criterion</th>
                  <th>Key</th>
                  <th>Weight</th>
                  <th>Range</th>
                  <th>Share</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {criteria.map((c) => (
                  <tr key={c.id}>
                    <td>
                      {c.name}
                      {c.description && (
                        <div className="muted" style={{ fontSize: 12 }}>
                          {c.description}
                        </div>
                      )}
                    </td>
                    <td className="muted">{c.key}</td>
                    <td>{c.weight}</td>
                    <td className="muted">
                      {c.min_score}–{c.max_score}
                    </td>
                    <td className="muted">
                      {weightTotal > 0 ? `${Math.round((100 * c.weight) / weightTotal)}%` : '—'}
                    </td>
                    <td>
                      <form action={deleteCriterionAction}>
                        <input type="hidden" name="slug" value={slug} />
                        <input type="hidden" name="criterion_id" value={c.id} />
                        <button type="submit" className="button small">
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

        <details>
          <summary>Add a criterion</summary>
          <form action={createCriterionAction} className="grid-form">
            <input type="hidden" name="slug" value={slug} />
            <label htmlFor="key">Key</label>
            <input id="key" name="key" required placeholder="impact" />
            <label htmlFor="name">Name</label>
            <input id="name" name="name" required placeholder="Impact" />
            <label htmlFor="description">Description</label>
            <input id="description" name="description" placeholder="What the judge should weigh" />
            <label htmlFor="weight">Weight</label>
            <input id="weight" name="weight" type="number" step="0.5" min="0.5" defaultValue="1" />
            <label htmlFor="min_score">Min</label>
            <input id="min_score" name="min_score" type="number" defaultValue="1" />
            <label htmlFor="max_score">Max</label>
            <input id="max_score" name="max_score" type="number" defaultValue="5" />
            <div />
            <button type="submit" className="button primary">
              Add criterion
            </button>
          </form>
        </details>
      </section>

      {/* ---------------------------------------------------------------- */}
      <section className="panel">
        <h2 style={{ fontSize: 20, marginTop: 0 }}>Judges</h2>
        {judges.length === 0 ? (
          <Empty>No judges invited yet.</Empty>
        ) : (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Judge</th>
                  <th>Email</th>
                  <th>Track</th>
                  <th>Active</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {judges.map((j) => (
                  <tr key={j.id}>
                    <td>{j.user.display_name}</td>
                    <td className="muted">{j.email}</td>
                    <td className="muted">{j.track?.name ?? 'all tracks'}</td>
                    <td>{j.is_active ? 'yes' : 'no'}</td>
                    <td>
                      <form action={setJudgeActiveAction}>
                        <input type="hidden" name="slug" value={slug} />
                        <input type="hidden" name="judge_id" value={j.id} />
                        <input type="hidden" name="is_active" value={j.is_active ? 'no' : 'yes'} />
                        <button type="submit" className="button small">
                          {j.is_active ? 'Deactivate' : 'Reactivate'}
                        </button>
                      </form>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}

        <details>
          <summary>Invite a judge</summary>
          <p className="muted" style={{ fontSize: 13 }}>
            The account has to exist already — there is no email being sent, and
            minting accounts from an invite form is a spam vector.
          </p>
          <form action={inviteJudgeAction} className="grid-form">
            <input type="hidden" name="slug" value={slug} />
            <label htmlFor="email">Email</label>
            <input id="email" name="email" type="email" required />
            <label htmlFor="track_id">Track</label>
            <select id="track_id" name="track_id" defaultValue="">
              <option value="">All tracks</option>
              {event.tracks.map((t) => (
                <option key={t.id} value={t.id}>
                  {t.name}
                </option>
              ))}
            </select>
            <div />
            <button type="submit" className="button primary">
              Invite
            </button>
          </form>
        </details>
      </section>

      {/* ---------------------------------------------------------------- */}
      <section className="panel">
        <h2 style={{ fontSize: 20, marginTop: 0 }}>Assignment</h2>
        <p className="muted" style={{ fontSize: 13 }}>
          Balanced, conflict-free and idempotent: a judge is never given their own
          team&rsquo;s project, track judges only see their track, and running this again
          tops the event up instead of doubling it. Try a dry run first.
        </p>
        <form action={runAssignmentAction} className="grid-form">
          <input type="hidden" name="slug" value={slug} />
          <label htmlFor="reviews_per_submission">Reviews per project</label>
          <input
            id="reviews_per_submission"
            name="reviews_per_submission"
            type="number"
            min="1"
            max="20"
            defaultValue="3"
          />
          <label htmlFor="seed">Seed (optional)</label>
          <input id="seed" name="seed" type="number" placeholder="for a reproducible plan" />
          <div />
          <div className="row">
            <button type="submit" name="dry_run" value="yes" className="button">
              Dry run
            </button>
            <button type="submit" name="dry_run" value="no" className="button primary">
              Assign
            </button>
          </div>
        </form>
      </section>

      {/* ---------------------------------------------------------------- */}
      <section className="panel">
        <h2 style={{ fontSize: 20, marginTop: 0 }}>Community vote</h2>
        <p className="muted" style={{ fontSize: 13 }}>
          Totals are hidden from everyone but you until you publish them. A tally the
          public can watch move during the window is a tally they can work out how to
          game, so this starts closed and both directions are written to the audit log.
        </p>
        <div className="row">
          <span className={event.results_public ? 'pill ok' : 'pill warn'}>
            {event.results_public ? 'Published' : 'Hidden'}
          </span>
          <form action={publishTallyAction}>
            <input type="hidden" name="slug" value={slug} />
            <input
              type="hidden"
              name="public"
              value={event.results_public ? 'no' : 'yes'}
            />
            <button type="submit" className="button primary">
              {event.results_public ? 'Make private again' : 'Publish the totals'}
            </button>
          </form>
          <Link className="button" href={`/events/${slug}/results`}>
            What the public sees
          </Link>
        </div>
      </section>

      {/* ---------------------------------------------------------------- */}
      <section className="panel">
        <h2 style={{ fontSize: 20, marginTop: 0 }}>Results and export</h2>
        <p>
          <Link href={`/organizer/${slug}/results`}>
            Open the results, raw against normalized →
          </Link>
        </p>
        <p className="muted" style={{ fontSize: 13 }}>
          CSV, organizer-only. These are plain links to the API, so they work from
          curl with a bearer token too.
        </p>
        <ul className="inline-list">
          {['teams', 'submissions', 'directory', 'judges', 'assignments', 'scores', 'results', 'votes', 'audit'].map((entity) => (
            <li key={entity}>
              <a href={`${PUBLIC_BASE}/api/events/${slug}/export/${entity}.csv`}>{entity}.csv</a>
            </li>
          ))}
        </ul>
      </section>
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
