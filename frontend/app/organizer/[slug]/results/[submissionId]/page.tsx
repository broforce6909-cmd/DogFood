import Link from 'next/link';
import { notFound } from 'next/navigation';

import { api } from '@/lib/api';
import { getMe } from '@/lib/session';
import { atLeast, type Event, type ScoringGrid } from '@/lib/types';

export const dynamic = 'force-dynamic';

export const metadata = { title: 'Scoring review -- Dogfood' };

/**
 * Part 4's scoring review grid: every judge's ballot for one project, side
 * by side. Read-only end to end -- this page has no form on it, because the
 * route behind it has no way to write a score either.
 */
export default async function ScoringGridPage({
  params,
}: {
  params: Promise<{ slug: string; submissionId: string }>;
}) {
  const { slug, submissionId } = await params;

  const me = await getMe();
  if (!atLeast(me.role, 'organizer')) notFound();

  const [event, grid] = await Promise.all([
    api<Event>(`/api/events/${slug}`),
    api<ScoringGrid>(`/api/events/${slug}/results/${submissionId}/scoring`),
  ]);

  return (
    <main className="shell">
      <p className="eyebrow">
        <Link href={`/organizer/${slug}/results`}>Results</Link> · {event.name}
      </p>
      <div className="spread">
        <h1>{grid.submission_name}</h1>
        <Link className="button" href={`/projects/${grid.submission_id}`}>
          View project
        </Link>
      </div>
      <p className="muted small">
        Every judge&apos;s ballot for this project, read-only. Nothing on this page can change a
        score.
      </p>

      <section className="panel" style={{ marginTop: 18 }}>
        <div className="grid" style={{ margin: '0 0 18px' }}>
          <div className="card">
            <p className="eyebrow">Reviews</p>
            <h2 style={{ margin: 0 }}>{grid.n_reviews}</h2>
          </div>
          <div className="card">
            <p className="eyebrow">Final raw</p>
            <h2 style={{ margin: 0 }}>{grid.final_raw_mean?.toFixed(3) ?? '—'}</h2>
          </div>
          <div className="card">
            <p className="eyebrow">Final normalized</p>
            <h2 style={{ margin: 0 }}>{grid.final_normalized_mean?.toFixed(3) ?? '—'}</h2>
          </div>
        </div>

        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Judge</th>
                <th>Status</th>
                {grid.criteria.map((c) => (
                  <th key={c.id}>{c.name}</th>
                ))}
                <th>Raw</th>
                <th>Normalized</th>
                <th>Comment</th>
              </tr>
            </thead>
            <tbody>
              {grid.judges.map((j) => {
                const byCriterion = new Map(j.scores.map((s) => [s.criterion_id, s.value]));
                return (
                  <tr key={j.judge_id}>
                    <td>
                      {j.judge_name}
                      {j.is_adjudication && (
                        <span className="pill" style={{ marginLeft: 6 }}>
                          adjudication
                        </span>
                      )}
                    </td>
                    <td className="muted">{j.status}</td>
                    {grid.criteria.map((c) => (
                      <td key={c.id}>{byCriterion.get(c.id) ?? '—'}</td>
                    ))}
                    <td className="muted">{j.raw_mean?.toFixed(3) ?? '—'}</td>
                    <td>
                      <strong>{j.normalized_mean?.toFixed(3) ?? '—'}</strong>
                    </td>
                    <td className="muted">{j.comment ?? '—'}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </section>
    </main>
  );
}
