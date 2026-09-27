import Link from 'next/link';
import { notFound } from 'next/navigation';

import { apiOrNull } from '@/lib/api';
import { safeHref } from '@/lib/format';
import type { Event, PublicResults } from '@/lib/types';

export const dynamic = 'force-dynamic';

export const metadata = { title: 'Leaderboard -- Dogfood' };

/**
 * Part 5's public results dashboard: every submitted project, not just
 * winners, ranked and scored by the judges -- distinct from the community
 * vote tally (`/events/[slug]/results`, a vote count) and from the gallery
 * (`/events/[slug]/gallery`, no rank or score at all).
 *
 * Gated by the API, not by this page: `apiOrNull` returns null on the 403
 * `Action.READ_PUBLIC_RESULTS` raises while results are unpublished, same
 * pattern the community results page already uses.
 */
export default async function LeaderboardPage({
  params,
}: {
  params: Promise<{ slug: string }>;
}) {
  const { slug } = await params;
  const event = await apiOrNull<Event>(`/api/events/${slug}`);
  if (!event) notFound();

  const results = await apiOrNull<PublicResults>(`/api/events/${slug}/results/public`);

  return (
    <main className="shell">
      <p className="eyebrow">
        <Link href={`/events/${slug}`}>{event.name}</Link>
      </p>
      <h1>Leaderboard</h1>
      <p className="muted" style={{ maxWidth: 640 }}>
        Every submitted project, ranked by the judges. Not a vote count -- see the{' '}
        <Link href={`/events/${slug}/results`}>community vote results</Link> for that.
      </p>

      {!results ? (
        <div className="notice" style={{ marginTop: 18 }}>
          Results for this event are not public yet.
        </div>
      ) : results.rows.length === 0 ? (
        <div className="notice" style={{ marginTop: 18 }}>
          No projects were submitted to this event.
        </div>
      ) : (
        <div className="table-wrap" style={{ marginTop: 18 }}>
          <table>
            <thead>
              <tr>
                <th>#</th>
                <th>Project</th>
                <th>Team</th>
                <th>Track</th>
                <th>Score</th>
                <th>Tier</th>
                <th>Links</th>
              </tr>
            </thead>
            <tbody>
              {results.rows.map((row) => (
                <tr key={row.submission_id}>
                  <td>
                    <strong>{row.normalized_rank ?? '—'}</strong>
                  </td>
                  <td>
                    <Link href={`/projects/${row.submission_id}`}>{row.submission_name}</Link>
                  </td>
                  <td className="muted">{row.team_name}</td>
                  <td className="muted">{row.track ?? '—'}</td>
                  <td>{row.normalized_score?.toFixed(3) ?? '—'}</td>
                  <td>
                    {row.tier === 'winner' ? (
                      <span className="pill ok">Winner</span>
                    ) : row.tier === 'community_tier' ? (
                      <span className="pill warn">Community vote</span>
                    ) : (
                      <span className="muted">—</span>
                    )}
                  </td>
                  <td className="row">
                    {safeHref(row.repo_url) && (
                      <a href={safeHref(row.repo_url)!} target="_blank" rel="noreferrer noopener">
                        Repo
                      </a>
                    )}
                    {safeHref(row.live_url) && (
                      <a href={safeHref(row.live_url)!} target="_blank" rel="noreferrer noopener">
                        Demo
                      </a>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </main>
  );
}
