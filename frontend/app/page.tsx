import Link from 'next/link';

import { Empty } from './components';
import { NormalizationDemo } from './normalization-demo';
import { api } from '@/lib/api';
import { formatDateTime, formatRange, untilDeadline } from '@/lib/format';
import { getMe } from '@/lib/session';
import type { EventSummary } from '@/lib/types';

export const dynamic = 'force-dynamic';

export default async function Home() {
  // The list a visitor gets and the list an organizer gets differ, and the
  // difference is decided by the API, not here.
  const [events, me] = await Promise.all([api<EventSummary[]>('/api/events'), getMe()]);

  return (
    <main className="shell">
      <section style={{ padding: '28px 0 8px' }}>
        <p className="eyebrow">Submission and judging portal</p>
        <h1 style={{ fontSize: 36 }}>Run a hackathon end to end.</h1>
        <p className="muted" style={{ maxWidth: 620 }}>
          Registration, teams, submissions, weighted judging with cross-judge normalization,
          community voting and public results -- self-hosted, with one command and no external
          services.
        </p>
        {!me.authenticated && (
          <div className="row" style={{ marginTop: 18 }}>
            <Link className="button primary" href="/register">
              Create an account
            </Link>
            <Link className="button" href="/gallery">
              Browse the gallery
            </Link>
          </div>
        )}
      </section>

      <hr />

      <div className="spread">
        <h2>Events</h2>
        <span className="muted small">{events.length} visible to you</span>
      </div>

      {events.length === 0 ? (
        <Empty>No events yet.</Empty>
      ) : (
        <div className="grid">
          {events.map((event) => (
            <Link key={event.id} className="card" href={`/events/${event.slug}`}>
              <div className="spread" style={{ alignItems: 'center' }}>
                <h3 style={{ margin: 0 }}>{event.name}</h3>
                {!event.is_published && <span className="pill warn">Unpublished</span>}
              </div>
              <p className="muted small" style={{ margin: '6px 0 12px' }}>
                {event.tagline ?? 'No tagline.'}
              </p>
              <dl className="meta small">
                <dt>Runs</dt>
                <dd>{formatRange(event.starts_at, event.ends_at)}</dd>
                <dt>Deadline</dt>
                <dd>
                  {formatDateTime(event.submission_deadline)}
                  <br />
                  <span className="muted">{untilDeadline(event.submission_deadline)}</span>
                </dd>
              </dl>
            </Link>
          ))}
        </div>
      )}

      <hr />

      <section>
        <p className="eyebrow">Why the ranking isn&rsquo;t just an average</p>
        <h2>A judge who marks everything a 3 shouldn&rsquo;t decide the result.</h2>
        <p className="muted" style={{ maxWidth: 620 }}>
          Averaging raw scores treats a harsh judge and a generous one as if they used the same
          scale. Toggle the view below -- the ranking moves.
        </p>
        <NormalizationDemo />
      </section>
    </main>
  );
}
