import Link from 'next/link';
import { notFound } from 'next/navigation';

import {
  assignThirdReviewAction,
  clearResultOverrideAction,
  setResultOverrideAction,
} from '../../../actions';
import { Empty } from '../../../components';
import { api, apiOrNull, PUBLIC_BASE } from '@/lib/api';
import { getMe } from '@/lib/session';
import {
  atLeast,
  type Consistency,
  type Disagreement,
  type Event,
  type PairwiseResults,
  type Results,
} from '@/lib/types';

export const dynamic = 'force-dynamic';

export const metadata = { title: 'Judging results -- Dogfood' };

/**
 * Results: raw against normalized, with the rank movement between them.
 *
 * Staff-only for the whole of T2 — the brief requires results hidden from
 * everyone but organizers during the voting window, and starting closed is the
 * honest way to land that.
 *
 * The page shows both rankings side by side rather than just the final one,
 * because "normalization moved this project up three places" is a claim somebody
 * will want to check, and hiding the old number would not make it less true.
 */
export default async function ResultsPage({
  params,
  searchParams,
}: {
  params: Promise<{ slug: string }>;
  searchParams: Promise<{ provisional?: string; error?: string; third_review?: string }>;
}) {
  const { slug } = await params;
  const { provisional, error, third_review } = await searchParams;

  const me = await getMe();
  if (!atLeast(me.role, 'organizer')) notFound();

  const includeUnfinished = provisional === '1';
  const [event, results] = await Promise.all([
    api<Event>(`/api/events/${slug}`),
    api<Results>(`/api/events/${slug}/results${includeUnfinished ? '?provisional=true' : ''}`),
  ]);
  const [pairwise, disagreement, consistency] = await Promise.all([
    event.pairwise_enabled
      ? apiOrNull<PairwiseResults>(`/api/events/${slug}/pairwise/results`)
      : Promise.resolve(null),
    apiOrNull<Disagreement>(`/api/events/${slug}/results/disagreement`),
    apiOrNull<Consistency>(`/api/events/${slug}/results/consistency`),
  ]);

  const moved = results.rows.filter((r) => r.rank_delta !== 0);

  return (
    <main className="shell">
      <section style={{ padding: '24px 0 4px' }}>
        <Link href={`/organizer/${slug}/judging`} className="muted" style={{ fontSize: 13 }}>
          ← Judging
        </Link>
        <p className="eyebrow" style={{ marginTop: 10 }}>
          Results · organizers only
        </p>
        <h1 style={{ fontSize: 30 }}>{event.name}</h1>
        <p className="muted">{results.method}</p>
      </section>

      {error && <p className="notice bad">{error}</p>}
      {third_review && <p className="notice">Third review: {third_review}</p>}

      <section className="panel">
        <div className="stat-row">
          <Stat label="Global mean" value={results.global_mean.toFixed(3)} />
          <Stat label="Global sd" value={results.global_sd.toFixed(3)} />
          <Stat label="Shrinkage k" value={String(results.shrinkage_k)} />
          <Stat label="Projects moved" value={`${moved.length}/${results.rows.length}`} />
        </div>
        <p className="muted" style={{ fontSize: 13, marginBottom: 0 }}>
          {includeUnfinished ? (
            <>
              Showing <strong>provisional</strong> results, including ballots that are
              not finished. Never publish these.{' '}
              <Link href={`/organizer/${slug}/results`}>Show finished ballots only →</Link>
            </>
          ) : (
            <>
              Only completed ballots are counted — a half-filled ballot is not a
              judgement.{' '}
              <Link href={`/organizer/${slug}/results?provisional=1`}>
                Show the provisional picture →
              </Link>
            </>
          )}
        </p>
      </section>

      <section className="panel">
        <h2 style={{ fontSize: 20, marginTop: 0 }}>Ranking</h2>
        {results.rows.length === 0 ? (
          <Empty>
            No completed ballots yet. Results appear once judges start finishing their
            reviews.
          </Empty>
        ) : (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>#</th>
                  <th>Project</th>
                  <th>Team</th>
                  <th>Track</th>
                  <th>Reviews</th>
                  <th>Raw</th>
                  <th>Normalized</th>
                  <th>Was</th>
                  <th>Move</th>
                  <th>Tier</th>
                  <th>Override</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {results.rows.map((r) => (
                  <tr key={r.submission_id}>
                    <td>
                      <strong>{r.normalized_rank}</strong>
                    </td>
                    <td>
                      <Link href={`/projects/${r.submission_id}`}>{r.submission_name}</Link>
                    </td>
                    <td className="muted">{r.team_name}</td>
                    <td className="muted">{r.track ?? '—'}</td>
                    <td>{r.n_reviews}</td>
                    <td className="muted">{r.raw_mean.toFixed(3)}</td>
                    <td>
                      <strong>{r.normalized_mean.toFixed(3)}</strong>
                    </td>
                    <td className="muted">{r.raw_rank}</td>
                    <td className={r.rank_delta > 0 ? 'up' : r.rank_delta < 0 ? 'down' : 'muted'}>
                      {r.rank_delta > 0 ? `▲ ${r.rank_delta}` : r.rank_delta < 0 ? `▼ ${-r.rank_delta}` : '—'}
                    </td>
                    <td>
                      {r.tier === 'winner' ? (
                        <span className="pill ok">Winner</span>
                      ) : r.tier === 'community_tier' ? (
                        <span className="pill warn">Community vote</span>
                      ) : (
                        <span className="muted">—</span>
                      )}
                      {r.tier !== r.computed_tier && (
                        <div className="muted small" style={{ marginTop: 2 }}>
                          computed: {r.computed_tier ?? 'neither'}
                        </div>
                      )}
                    </td>
                    <td style={{ minWidth: 220 }}>
                      {r.overridden_by && (
                        <p className="muted small" style={{ margin: '0 0 4px' }}>
                          {r.overridden_by}: {r.override_reason}
                        </p>
                      )}
                      <form action={setResultOverrideAction} className="row" style={{ flexWrap: 'wrap', gap: 4 }}>
                        <input type="hidden" name="slug" value={event.slug} />
                        <input type="hidden" name="submission_id" value={r.submission_id} />
                        <select name="tier" defaultValue={r.tier ?? 'neither'} style={{ width: 130 }}>
                          <option value="winner">Winner</option>
                          <option value="community_tier">Community vote</option>
                          <option value="neither">Neither</option>
                        </select>
                        <input
                          name="reason"
                          required
                          placeholder="reason"
                          style={{ width: 130 }}
                        />
                        <button type="submit" className="button small">
                          Set
                        </button>
                      </form>
                      {r.overridden_by && (
                        <form action={clearResultOverrideAction} className="row" style={{ marginTop: 4 }}>
                          <input type="hidden" name="slug" value={event.slug} />
                          <input type="hidden" name="submission_id" value={r.submission_id} />
                          <input name="reason" required placeholder="reason to clear" style={{ width: 130 }} />
                          <button type="submit" className="quiet small">
                            Clear
                          </button>
                        </form>
                      )}
                    </td>
                    <td>
                      <Link
                        className="button small"
                        href={`/organizer/${event.slug}/results/${r.submission_id}`}
                      >
                        Scoring
                      </Link>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>

      <section className="panel">
        <h2 style={{ fontSize: 20, marginTop: 0 }}>How each judge used the scale</h2>
        <p className="muted" style={{ fontSize: 13 }}>
          Published because &ldquo;your normalization moved my project down four places&rdquo;
          deserves an answer more specific than &ldquo;statistics&rdquo;. See{' '}
          <a href="https://github.com/">JUDGING.md</a> for the method and its limits.
        </p>
        {results.calibrations.length === 0 ? (
          <Empty>No ballots to calibrate from yet.</Empty>
        ) : (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Judge</th>
                  <th>Track</th>
                  <th>Ballots</th>
                  <th>Mean</th>
                  <th>SD</th>
                  <th>Shrunk mean</th>
                  <th>Shrunk SD</th>
                  <th>Note</th>
                </tr>
              </thead>
              <tbody>
                {/* A judge with no track restriction reviews across tracks and
                    is normalized separately for each -- see
                    ARCHITECTURE.md's "Judging, and the one place staff
                    privilege stops" and JUDGING.md §3. They appear once per
                    track here, `judge_id` alone is not a unique key. */}
                {results.calibrations.map((c) => (
                  <tr key={`${c.judge_id}:${c.track ?? ''}`} className={c.flat ? 'flagged' : undefined}>
                    <td>{c.judge_name}</td>
                    <td className="muted">{c.track ?? 'No track'}</td>
                    <td>{c.n}</td>
                    <td>{c.mean.toFixed(3)}</td>
                    <td>{c.sd.toFixed(3)}</td>
                    <td className="muted">{c.shrunk_mean.toFixed(3)}</td>
                    <td className="muted">{c.shrunk_sd.toFixed(3)}</td>
                    <td className="muted" style={{ fontSize: 13 }}>
                      {c.note}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>

      {disagreement && (
        <section className="panel">
          <h2 style={{ fontSize: 20, marginTop: 0 }}>Disagreement between judges</h2>
          <p className="muted" style={{ fontSize: 13 }}>
            Projects flagged here have judges whose <em>normalized</em> scores still
            differ by more than {disagreement.threshold.toFixed(1)} points on average --
            disagreement about the project, not about how each judge uses the scale.{' '}
            {disagreement.needs_review_count} of {disagreement.submissions.length} project(s)
            flagged.
          </p>
          {disagreement.submissions.filter((r) => r.needs_review).length === 0 ? (
            <Empty>No projects need a third opinion right now.</Empty>
          ) : (
            <div className="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th>Project</th>
                    <th>Team</th>
                    <th>Track</th>
                    <th>Reviews</th>
                    <th>Avg. pairwise diff</th>
                    <th></th>
                  </tr>
                </thead>
                <tbody>
                  {disagreement.submissions
                    .filter((r) => r.needs_review)
                    .map((r) => (
                      <tr key={r.submission_id} className="flagged">
                        <td>
                          <Link href={`/projects/${r.submission_id}`}>{r.submission_name}</Link>
                        </td>
                        <td className="muted">{r.team_name}</td>
                        <td className="muted">{r.track ?? '—'}</td>
                        <td>{r.n_reviews}</td>
                        <td>
                          <strong>{r.mean_abs_pairwise_diff.toFixed(2)}</strong>
                        </td>
                        <td>
                          <form action={assignThirdReviewAction}>
                            <input type="hidden" name="slug" value={slug} />
                            <input type="hidden" name="submission_id" value={r.submission_id} />
                            <button type="submit" className="button" style={{ fontSize: 13, padding: '4px 10px' }}>
                              Request third review
                            </button>
                          </form>
                        </td>
                      </tr>
                    ))}
                </tbody>
              </table>
            </div>
          )}
          {disagreement.judges.length > 0 && (
            <p className="muted" style={{ fontSize: 13, marginTop: 12, marginBottom: 0 }}>
              Furthest from consensus:{' '}
              {disagreement.judges
                .slice(0, 5)
                .map((j) => `${j.judge_name} (${j.mean_abs_deviation.toFixed(2)})`)
                .join(', ')}
            </p>
          )}
        </section>
      )}

      {consistency && consistency.flags.length > 0 && (
        <section className="panel">
          <h2 style={{ fontSize: 20, marginTop: 0 }}>Patterns worth a look</h2>
          <p className="muted" style={{ fontSize: 13 }}>
            Signals for organizer review only, never a verdict -- each flag describes a
            pattern in the data, not a conclusion about a judge.
          </p>
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Judge</th>
                  <th>Pattern</th>
                  <th>Detail</th>
                </tr>
              </thead>
              <tbody>
                {consistency.flags.map((f, i) => (
                  <tr key={`${f.judge_id}:${f.reason}:${i}`} className="flagged">
                    <td>{f.judge_name}</td>
                    <td className="muted">
                      {f.reason === 'near_identical_scores' ? 'Near-identical scores' : 'Fast completion'}
                    </td>
                    <td className="muted" style={{ fontSize: 13 }}>
                      {f.detail}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>
      )}

      {pairwise && (
        <section className="panel">
          <h2 style={{ fontSize: 20, marginTop: 0 }}>Pairwise ranking</h2>
          <p className="muted" style={{ fontSize: 13 }}>
            {pairwise.method}. {pairwise.total_comparisons} comparison
            {pairwise.total_comparisons === 1 ? '' : 's'} recorded
            {!pairwise.converged && ' -- the fit had not converged when this was computed'}.
          </p>
          {pairwise.coverage.total_submissions > 0 && (
            <p className="muted" style={{ fontSize: 13 }}>
              Coverage: {pairwise.coverage.total_submissions} projects,{' '}
              {pairwise.coverage.total_comparisons} comparisons,{' '}
              {pairwise.coverage.coverage_pct.toFixed(1)}% of projects compared at least once
              (min {pairwise.coverage.min_comparisons}, max {pairwise.coverage.max_comparisons},
              mean {pairwise.coverage.mean_comparisons.toFixed(1)} per project).
              {pairwise.coverage.coverage_pct < 100 &&
                ' Some projects have zero comparisons yet -- see the Comparisons column below.'}
            </p>
          )}
          {pairwise.rows.length === 0 ? (
            <Empty>No submitted projects to rank yet.</Empty>
          ) : (
            <div className="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th>#</th>
                    <th>Project</th>
                    <th>Team</th>
                    <th>Track</th>
                    <th>Comparisons</th>
                    <th>Record</th>
                    <th>Rating</th>
                  </tr>
                </thead>
                <tbody>
                  {pairwise.rows.map((r) => (
                    <tr key={r.submission_id}>
                      <td>
                        <strong>{r.rank}</strong>
                      </td>
                      <td>
                        <Link href={`/projects/${r.submission_id}`}>{r.submission_name}</Link>
                      </td>
                      <td className="muted">{r.team_name}</td>
                      <td className="muted">{r.track ?? '—'}</td>
                      <td>{r.n_comparisons}</td>
                      <td className="muted">
                        {r.n_comparisons === 0 ? '—' : `${r.wins}-${r.losses}`}
                      </td>
                      <td>
                        <strong>{r.rating.toFixed(3)}</strong>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
          {pairwise.judges.length > 0 && (
            <p className="muted" style={{ fontSize: 13, marginTop: 12, marginBottom: 0 }}>
              {pairwise.judges.map((j) => `${j.judge_name} (${j.n_comparisons})`).join(', ')}
            </p>
          )}
        </section>
      )}

      <section className="panel">
        <h2 style={{ fontSize: 20, marginTop: 0 }}>Export</h2>
        <ul className="inline-list">
          <li>
            <a href={`${PUBLIC_BASE}/api/events/${slug}/export/results.csv`}>results.csv</a>
          </li>
          <li>
            <a href={`${PUBLIC_BASE}/api/events/${slug}/export/scores.csv`}>scores.csv</a>
          </li>
        </ul>
        <p className="muted" style={{ fontSize: 13, marginBottom: 0 }}>
          <code>scores.csv</code> is every ballot, one row per judge, project and
          criterion — the file the worked example in JUDGING.md is read from.
        </p>
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
