import Link from 'next/link';
import { notFound, redirect } from 'next/navigation';

import { comparePairwiseAction } from '../../../actions';
import { FormError } from '../../../components';
import { ApiError, api, apiOrNull } from '@/lib/api';
import { getMe } from '@/lib/session';
import type { Event, Pair } from '@/lib/types';
import { PairwiseShortcuts } from './keyboard-shortcuts';

export const dynamic = 'force-dynamic';

export const metadata = { title: 'Pairwise judging -- Dogfood' };

/**
 * A judge's pairwise comparison screen: two projects, three buttons.
 *
 * Deliberately not a single-page app -- picking a winner posts a plain form to
 * `comparePairwiseAction`, which redirects back here, and a fresh `GET` fetches
 * whatever pair `app/pairwise.pick_next_pair` decides comes next. There is no
 * client-side state to keep in sync with the server's idea of what has already
 * been compared, which is exactly the bug class that model avoids.
 *
 * Nothing here decides eligibility. `GET .../pairwise/next` already applied
 * every rule (submitted only, this judge's track, never this judge's own
 * team) -- this page renders whatever it was handed.
 */
export default async function PairwisePage({
  params,
  searchParams,
}: {
  params: Promise<{ slug: string }>;
  searchParams: Promise<{ error?: string; saved?: string }>;
}) {
  const { slug } = await params;
  const { error, saved } = await searchParams;

  const me = await getMe();
  if (!me.authenticated) redirect(`/login?next=/events/${slug}/pairwise`);

  const event = await apiOrNull<Event>(`/api/events/${slug}`);
  if (!event) notFound();

  let pair: Pair | null = null;
  let notEnough = false;
  try {
    pair = await api<Pair>(`/api/events/${slug}/pairwise/next`);
  } catch (err) {
    // 404 here means "you are not a judge on this event" -- treated the same
    // as any other resource this viewer may not have.
    if (err instanceof ApiError && err.status === 404) notFound();
    // 409: a real, expected state (fewer than two eligible projects), not a
    // bug -- rendered as a message, not a crash.
    if (err instanceof ApiError && err.status === 409) notEnough = true;
    else throw err;
  }

  return (
    <main className="shell narrow">
      <section style={{ padding: '24px 0 4px' }}>
        <Link href="/judging" className="muted" style={{ fontSize: 13 }}>
          ← Judge console
        </Link>
        <p className="eyebrow" style={{ marginTop: 10 }}>
          Pairwise · {event.name}
        </p>
        <h1 style={{ fontSize: 28 }}>Which is better?</h1>
        <p className="muted">
          Pick the stronger project. If you genuinely can&rsquo;t tell, say so --
          that is real information, not a skipped question.
        </p>
      </section>

      <FormError message={error} />
      {saved && <p className="notice">Recorded. Here is the next pair.</p>}

      {notEnough ? (
        <p className="notice">
          Not enough submitted projects to compare yet -- check back once more
          teams have entered, or once judging opens.
        </p>
      ) : pair ? (
        <form action={comparePairwiseAction}>
          <input type="hidden" name="event_slug" value={slug} />
          <input type="hidden" name="submission_a_id" value={pair.submission_a.id} />
          <input type="hidden" name="submission_b_id" value={pair.submission_b.id} />

          <div className="grid" style={{ gridTemplateColumns: '1fr 1fr', gap: 16 }}>
            <ProjectCard project={pair.submission_a} />
            <ProjectCard project={pair.submission_b} />
          </div>

          <div className="row" style={{ marginTop: 16, justifyContent: 'center' }}>
            <button type="submit" name="winner" value="tie" className="button">
              Can&rsquo;t decide
            </button>
          </div>
        </form>
      ) : null}

      {pair && (
        <PairwiseShortcuts leftId={pair.submission_a.id} rightId={pair.submission_b.id} />
      )}
    </main>
  );
}

function ProjectCard({ project }: { project: Pair['submission_a'] }) {
  return (
    <div className="panel">
      {project.thumbnail_url && (
        // A thumbnail can be an inline data: URI, which next/image does not accept.
        // eslint-disable-next-line @next/next/no-img-element
        <img
          src={project.thumbnail_url}
          alt=""
          style={{ width: '100%', borderRadius: 8, marginBottom: 10 }}
        />
      )}
      <h2 style={{ fontSize: 19, marginTop: 0 }}>{project.name}</h2>
      <p className="muted" style={{ fontSize: 13 }}>
        {project.team_name}
        {project.track && <span className="tag" style={{ marginLeft: 6 }}>{project.track.name}</span>}
      </p>
      {project.tagline && <p>{project.tagline}</p>}
      {project.description && (
        <p className="muted" style={{ fontSize: 13, whiteSpace: 'pre-wrap' }}>
          {project.description}
        </p>
      )}
      {project.tech_tags.length > 0 && (
        <p>
          {project.tech_tags.map((t) => (
            <span key={t} className="tag">
              {t}
            </span>
          ))}
        </p>
      )}
      <p className="muted" style={{ fontSize: 13 }}>
        <Link href={`/projects/${project.id}`}>Open the full project page →</Link>
      </p>
      <button
        type="submit"
        name="winner"
        value={project.id}
        className="button primary"
        style={{ width: '100%' }}
      >
        This one is better
      </button>
    </div>
  );
}
