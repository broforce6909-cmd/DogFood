import Link from 'next/link';
import { notFound, redirect } from 'next/navigation';

import { saveBallotAction } from '../../actions';
import { api, apiOrNull } from '@/lib/api';
import { formatDateTime } from '@/lib/format';
import { getMe } from '@/lib/session';
import type { Assignment, Criterion } from '@/lib/types';
import { KeyboardScoring } from './keyboard-scoring';

export const dynamic = 'force-dynamic';

// A judge scoring 30 projects has 30 tabs that all used to read "Dogfood --
// Hackathon Portal". Next.js memoizes this fetch against the identical one the
// page body makes below, so this is not a second round trip to the API.
export async function generateMetadata({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  const ballot = await apiOrNull<Assignment>(`/api/judging/assignments/${id}`);
  return { title: ballot ? `Ballot: ${ballot.submission.name} -- Dogfood` : 'Ballot -- Dogfood' };
}

/**
 * One ballot.
 *
 * A plain HTML form: a number input per criterion, a feedback box, and a save.
 * The score inputs are named `score:<criterion_id>` so a rubric of any length
 * posts without JavaScript. Scoring is continuous -- 0.1 steps within the
 * criterion's own min/max -- rather than a fixed set of whole-number choices,
 * so a number input is the right control here rather than radio buttons: a
 * slider would need JavaScript to show the value as it moves, which this page
 * deliberately has none of.
 *
 * The page hides the save button when `can_score` is false. That is a courtesy —
 * the API refuses the write regardless, and `test_judging_isolation.py` proves it
 * by posting directly.
 */
export default async function BallotPage({
  params,
  searchParams,
}: {
  params: Promise<{ id: string }>;
  searchParams: Promise<{ error?: string; saved?: string }>;
}) {
  const { id } = await params;
  const { error, saved } = await searchParams;

  const me = await getMe();
  if (!me.authenticated) redirect(`/login?next=/judging/${id}`);

  // 403 for a peer's ballot. Rendering "not found" is the right thing for a UI
  // to say about a thing the viewer may not have.
  const ballot = await apiOrNull<Assignment>(`/api/judging/assignments/${id}`);
  if (!ballot) notFound();

  const criteria = await api<Criterion[]>(`/api/events/${ballot.event_slug}/criteria`);
  const existing = new Map(ballot.scores.map((s) => [s.criterion_id, s.value]));
  const weightTotal = criteria.reduce((sum, c) => sum + c.weight, 0);

  // Same queue order `/judging` renders, so left/right on the keyboard walks
  // the identical list a click on "next" in that list would.
  const queue = await api<Assignment[]>('/api/judging/queue');
  const queueIndex = queue.findIndex((a) => a.id === ballot.id);
  const prevHref = queueIndex > 0 ? `/judging/${queue[queueIndex - 1].id}` : null;
  const nextHref =
    queueIndex !== -1 && queueIndex < queue.length - 1
      ? `/judging/${queue[queueIndex + 1].id}`
      : null;

  return (
    <main className="shell">
      <section style={{ padding: '24px 0 4px' }}>
        <Link href="/judging" className="muted" style={{ fontSize: 13 }}>
          ← Back to your ballots
        </Link>
        <p className="eyebrow" style={{ marginTop: 10 }}>
          {ballot.submission.track?.name ?? 'No track'} · {ballot.event_slug}
        </p>
        <h1 style={{ fontSize: 30, marginBottom: 4 }}>{ballot.submission.name}</h1>
        <p className="muted">{ballot.submission.tagline}</p>
        <p className="muted" style={{ fontSize: 13 }}>
          Assigned {formatDateTime(ballot.assigned_at)}
          {ballot.completed_at && ` · submitted ${formatDateTime(ballot.completed_at)}`}
        </p>
      </section>

      {error && <p className="notice bad">{error}</p>}
      {saved && <p className="notice">Ballot saved.</p>}

      {!ballot.can_score && (
        <p className="notice">
          This ballot is read-only: judging is closed for this event, or your judge
          record is inactive. Scores already recorded are shown below.
        </p>
      )}

      <section className="panel">
        <h2 style={{ fontSize: 18, marginTop: 0 }}>The project</h2>
        <dl className="kv">
          <dt>Team</dt>
          <dd>{ballot.submission.team_name}</dd>
          {ballot.submission.tech_tags.length > 0 && (
            <>
              <dt>Tech</dt>
              <dd>
                {ballot.submission.tech_tags.map((t) => (
                  <span key={t} className="tag">
                    {t}
                  </span>
                ))}
              </dd>
            </>
          )}
        </dl>
        <p>
          <Link href={`/projects/${ballot.submission.id}`}>Open the full project page →</Link>
        </p>
      </section>

      <form action={saveBallotAction} className="panel">
        <input type="hidden" name="assignment_id" value={ballot.id} />
        <h2 style={{ fontSize: 18, marginTop: 0 }}>Your scores</h2>
        <p className="muted" style={{ fontSize: 13 }}>
          Weights are shown as the organizer set them. Your ballot is weighted by
          its own total ({weightTotal}), so the result stays on the rubric&rsquo;s scale.
        </p>

        {criteria.length === 0 && (
          <p className="notice bad">
            This event has no rubric yet. An organizer needs to add criteria before
            it can be scored.
          </p>
        )}

        <KeyboardScoring
          criterionIds={criteria.map((c) => c.id)}
          canScore={ballot.can_score}
          prevHref={prevHref}
          nextHref={nextHref}
        />

        {criteria.map((c) => {
          const current = existing.get(c.id);
          return (
            <fieldset key={c.id} className="criterion">
              <legend>
                {c.name} <span className="muted">× {c.weight}</span>
              </legend>
              {c.description && (
                <p className="muted" style={{ fontSize: 13, margin: '2px 0 8px' }}>
                  {c.description}
                </p>
              )}
              <label htmlFor={`score:${c.id}`} className="muted" style={{ fontSize: 13 }}>
                Score ({c.min_score}–{c.max_score}, 0.1 steps)
              </label>
              <input
                id={`score:${c.id}`}
                type="number"
                name={`score:${c.id}`}
                min={c.min_score}
                max={c.max_score}
                step="0.1"
                inputMode="decimal"
                defaultValue={current !== undefined ? current.toFixed(1) : ''}
                disabled={!ballot.can_score}
                style={{ width: 90 }}
              />
            </fieldset>
          );
        })}

        <label htmlFor="comment">Written feedback</label>
        <p className="muted" style={{ fontSize: 13, marginTop: 0 }}>
          Goes to the team whether they place or not.
        </p>
        <textarea
          id="comment"
          name="comment"
          rows={5}
          defaultValue={ballot.comment ?? ''}
          disabled={!ballot.can_score}
          placeholder="What worked, what you would push on next."
        />

        {ballot.can_score && criteria.length > 0 && (
          <div className="row">
            <button type="submit" name="complete" value="yes" className="button primary">
              Save and mark complete
            </button>
            <button type="submit" name="complete" value="no" className="button">
              Save progress
            </button>
          </div>
        )}
        <p className="muted" style={{ fontSize: 12 }}>
          A ballot is only counted as complete when every criterion has a score.
        </p>
      </form>
    </main>
  );
}
