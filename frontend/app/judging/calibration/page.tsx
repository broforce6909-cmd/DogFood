import Link from 'next/link';
import { redirect } from 'next/navigation';

import { saveCalibrationAction } from '../../actions';
import { Empty } from '../../components';
import { api } from '@/lib/api';
import { getMe } from '@/lib/session';
import type { CalibrationPractice } from '@/lib/types';

export const dynamic = 'force-dynamic';

export const metadata = { title: 'Practice projects -- Dogfood' };

/**
 * Practice projects: score a few known projects before real judging, so an
 * organizer can see whether you run harsh or generous.
 *
 * A plain form per project, the same shape as a real ballot. Nothing on this
 * page can show you the expected scores, and nothing you enter here counts
 * toward any real result -- it is calibration, not judging.
 */
export default async function CalibrationPage({
  searchParams,
}: {
  searchParams: Promise<{ error?: string; saved?: string }>;
}) {
  const { error, saved } = await searchParams;
  const me = await getMe();
  if (!me.authenticated) redirect('/login?next=/judging/calibration');

  const projects = await api<CalibrationPractice[]>('/api/judging/calibration');

  return (
    <main className="shell">
      <section style={{ padding: '24px 0 4px' }}>
        <Link href="/judging" className="muted" style={{ fontSize: 13 }}>
          ← Back to your ballots
        </Link>
        <p className="eyebrow" style={{ marginTop: 10 }}>
          Judge console
        </p>
        <h1 style={{ fontSize: 30 }}>Practice projects</h1>
        <p className="muted">
          Score these as you would a real project. They do not count toward any result;
          they help the organizers see how you use the scale before judging begins.
        </p>
      </section>

      {error && <p className="notice bad">{error}</p>}
      {saved && <p className="notice">Practice scores saved.</p>}

      {projects.length === 0 && (
        <Empty>No practice projects have been set up for your events.</Empty>
      )}

      {projects.map((p) => {
        const mine = new Map(p.my_scores.map((s) => [s.criterion_id, s.value]));
        return (
          <form key={p.id} action={saveCalibrationAction} className="panel">
            <input type="hidden" name="project_id" value={p.id} />
            <p className="eyebrow">
              {p.event_slug} · {p.complete ? 'Scored' : 'Not yet scored'}
            </p>
            <h2 style={{ fontSize: 20, marginTop: 0 }}>{p.name}</h2>
            {p.description && <p style={{ whiteSpace: 'pre-wrap' }}>{p.description}</p>}

            {p.criteria.map((c) => (
              <fieldset key={c.id} className="criterion">
                <legend>{c.name}</legend>
                <label htmlFor={`${p.id}:${c.id}`} className="muted" style={{ fontSize: 13 }}>
                  Score ({c.min_score}–{c.max_score}, 0.1 steps)
                </label>
                <input
                  id={`${p.id}:${c.id}`}
                  type="number"
                  name={`score:${c.id}`}
                  min={c.min_score}
                  max={c.max_score}
                  step="0.1"
                  inputMode="decimal"
                  defaultValue={mine.get(c.id) ?? ''}
                  style={{ width: 90 }}
                />
              </fieldset>
            ))}

            <div className="row">
              <button type="submit" className="button primary">
                Save practice scores
              </button>
            </div>
          </form>
        );
      })}
    </main>
  );
}
