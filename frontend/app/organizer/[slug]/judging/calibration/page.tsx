import Link from 'next/link';
import { notFound } from 'next/navigation';

import { addCalibrationProjectAction, removeCalibrationProjectAction } from '../../../../actions';
import { Empty } from '../../../../components';
import { api } from '@/lib/api';
import { getMe } from '@/lib/session';
import {
  atLeast,
  type CalibrationProject,
  type CalibrationReport,
  type Criterion,
  type Event,
} from '@/lib/types';

export const dynamic = 'force-dynamic';

export const metadata = { title: 'Judge calibration -- Dogfood' };

const VERDICT_LABEL: Record<string, string> = {
  not_started: 'Not started',
  incomplete: 'Incomplete',
  harsh: 'Harsh',
  generous: 'Generous',
  aligned: 'Aligned',
};

/**
 * Judge calibration, organizer side: add practice projects with the scores you
 * expect, then read who ran harsh or generous.
 *
 * The flag is a prompt to have a conversation, not a verdict -- with one or two
 * practice projects it is a noisy estimate, and nothing here changes anyone's
 * real scores. `app/calibration.py` says exactly what is measured.
 */
export default async function OrganizerCalibrationPage({
  params,
  searchParams,
}: {
  params: Promise<{ slug: string }>;
  searchParams: Promise<{ error?: string }>;
}) {
  const { slug } = await params;
  const { error } = await searchParams;

  const me = await getMe();
  if (!atLeast(me.role, 'organizer')) notFound();

  const [event, criteria, projects, report] = await Promise.all([
    api<Event>(`/api/events/${slug}`),
    api<Criterion[]>(`/api/events/${slug}/criteria`),
    api<CalibrationProject[]>(`/api/events/${slug}/calibration/projects`),
    api<CalibrationReport>(`/api/events/${slug}/calibration/report`),
  ]);
  const criterionName = new Map(criteria.map((c) => [c.id, c.name]));

  return (
    <main className="shell">
      <section style={{ padding: '24px 0 4px' }}>
        <Link href={`/organizer/${slug}/judging`} className="muted" style={{ fontSize: 13 }}>
          ← Judging
        </Link>
        <p className="eyebrow" style={{ marginTop: 10 }}>
          Judge calibration
        </p>
        <h1 style={{ fontSize: 30 }}>{event.name}</h1>
        <p className="muted">
          Judges score your practice projects before real judging. A judge whose scores
          average {Math.round(report.threshold * 100)}% of the scale or more below or above
          yours is flagged harsh or generous. It is a prompt to talk, not a correction.
        </p>
      </section>

      {error && <p className="notice bad">{error}</p>}

      <section className="panel">
        <h2 style={{ fontSize: 18, marginTop: 0 }}>Who is calibrated</h2>
        {report.rows.length === 0 ? (
          <Empty>No active judges on this event yet.</Empty>
        ) : (
          <table className="table">
            <thead>
              <tr>
                <th>Judge</th>
                <th>Practice scored</th>
                <th>Mean deviation</th>
                <th>Mean absolute deviation</th>
                <th>Verdict</th>
              </tr>
            </thead>
            <tbody>
              {report.rows.map((r) => (
                <tr key={r.judge_id}>
                  <td>{r.judge_name}</td>
                  <td>
                    {r.projects_scored} / {r.projects_total}
                  </td>
                  <td>
                    {r.mean_signed_deviation === null
                      ? '—'
                      : `${r.mean_signed_deviation > 0 ? '+' : ''}${(r.mean_signed_deviation * 100).toFixed(1)}%`}
                  </td>
                  <td>
                    {r.mean_abs_deviation === null
                      ? '—'
                      : `${(r.mean_abs_deviation * 100).toFixed(1)}%`}
                  </td>
                  <td>
                    <strong>{VERDICT_LABEL[r.verdict]}</strong>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>

      <section className="panel">
        <h2 style={{ fontSize: 18, marginTop: 0 }}>Practice projects</h2>
        {projects.length === 0 ? (
          <Empty>None yet. Add one below.</Empty>
        ) : (
          projects.map((p) => (
            <div key={p.id} className="criterion">
              <strong>{p.name}</strong>
              <span className="muted" style={{ marginLeft: 8, fontSize: 13 }}>
                expected:{' '}
                {p.expected
                  .map((e) => `${criterionName.get(e.criterion_id) ?? '?'} ${e.value}`)
                  .join(', ')}
              </span>
              <form action={removeCalibrationProjectAction} style={{ display: 'inline', marginLeft: 12 }}>
                <input type="hidden" name="slug" value={slug} />
                <input type="hidden" name="project_id" value={p.id} />
                <button type="submit" className="button small">
                  Remove
                </button>
              </form>
            </div>
          ))
        )}
      </section>

      <form action={addCalibrationProjectAction} className="panel">
        <input type="hidden" name="slug" value={slug} />
        <h2 style={{ fontSize: 18, marginTop: 0 }}>Add a practice project</h2>
        {criteria.length === 0 ? (
          <p className="notice bad">Add rubric criteria first.</p>
        ) : (
          <>
            <label htmlFor="cal-name">Name</label>
            <input id="cal-name" name="name" required maxLength={160} />
            <label htmlFor="cal-description">What the judges are shown</label>
            <textarea id="cal-description" name="description" rows={4} />
            <p className="muted" style={{ fontSize: 13 }}>
              The scores you expect, one per criterion. Judges never see these.
            </p>
            {criteria.map((c) => (
              <div key={c.id}>
                <label htmlFor={`expected:${c.id}`}>
                  {c.name} ({c.min_score}–{c.max_score})
                </label>
                <input
                  id={`expected:${c.id}`}
                  type="number"
                  name={`expected:${c.id}`}
                  min={c.min_score}
                  max={c.max_score}
                  step="0.1"
                  required
                  style={{ width: 90 }}
                />
              </div>
            ))}
            <div className="row">
              <button type="submit" className="button primary">
                Add practice project
              </button>
            </div>
          </>
        )}
      </form>
    </main>
  );
}
