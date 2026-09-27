import Link from 'next/link';
import { notFound } from 'next/navigation';

import { Empty } from '../../../components';
import { apiOrNull } from '@/lib/api';
import type { JudgeReport, Submission } from '@/lib/types';

export const dynamic = 'force-dynamic';

export const metadata = { title: 'Judge feedback -- Dogfood' };

/**
 * Part 5's per-project judge report: a team's own project, judge feedback
 * included, and nothing else. Every judge is anonymized to "Judge 1",
 * "Judge 2", ... by the API itself -- this page never sees a real name to
 * accidentally render.
 *
 * Gated entirely by the API: `apiOrNull` returns null on the 403 a caller
 * who is not on this project's team (or whom results are not yet public
 * for) gets back, and the page renders the closed state either way rather
 * than trying to tell those two cases apart.
 */
export default async function JudgeReportPage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id } = await params;
  const project = await apiOrNull<Submission>(`/api/submissions/${id}`);
  if (!project) notFound();

  const report = await apiOrNull<JudgeReport>(
    `/api/events/${project.event_slug}/results/${id}/report`,
  );

  return (
    <main className="shell">
      <p className="eyebrow">
        <Link href={`/projects/${id}`}>{project.name}</Link>
      </p>
      <h1>Judge feedback</h1>

      {!report ? (
        <div className="notice" style={{ marginTop: 18 }}>
          Judge feedback for this project isn&apos;t available yet -- either results
          haven&apos;t been published, or this project has no completed reviews.
        </div>
      ) : report.judges.length === 0 ? (
        <Empty>No completed reviews yet.</Empty>
      ) : (
        <>
          <div className="grid" style={{ margin: '18px 0' }}>
            <div className="card">
              <p className="eyebrow">Reviews</p>
              <h2 style={{ margin: 0 }}>{report.n_reviews}</h2>
            </div>
            <div className="card">
              <p className="eyebrow">Final score</p>
              <h2 style={{ margin: 0 }}>{report.final_normalized_mean?.toFixed(3) ?? '—'}</h2>
            </div>
            {report.tier && (
              <div className="card">
                <p className="eyebrow">Result</p>
                <h2 style={{ margin: 0 }}>
                  {report.tier === 'winner' ? 'Winner' : 'Community vote round'}
                </h2>
              </div>
            )}
          </div>

          <div className="stack" style={{ gap: 16 }}>
            {report.judges.map((judge) => {
              const byCriterion = new Map(judge.scores.map((s) => [s.criterion_id, s.value]));
              return (
                <section className="panel" key={judge.label}>
                  <h2 style={{ fontSize: 18, marginTop: 0 }}>{judge.label}</h2>
                  <div className="table-wrap">
                    <table>
                      <thead>
                        <tr>
                          {report.criteria.map((c) => (
                            <th key={c.id}>{c.name}</th>
                          ))}
                        </tr>
                      </thead>
                      <tbody>
                        <tr>
                          {report.criteria.map((c) => (
                            <td key={c.id}>{byCriterion.get(c.id) ?? '—'}</td>
                          ))}
                        </tr>
                      </tbody>
                    </table>
                  </div>
                  {judge.comment && (
                    <p style={{ marginBottom: 0 }} className="prose">
                      {judge.comment}
                    </p>
                  )}
                </section>
              );
            })}
          </div>
        </>
      )}
    </main>
  );
}
