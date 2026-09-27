import Link from 'next/link';
import { redirect } from 'next/navigation';

import { Empty } from '../components';
import { api } from '@/lib/api';
import { formatDateTime } from '@/lib/format';
import { getMe } from '@/lib/session';
import type { Assignment, Certificate, Judge } from '@/lib/types';

export const dynamic = 'force-dynamic';

export const metadata = { title: 'Your ballots -- Dogfood' };

/**
 * The judge's queue.
 *
 * Thirty projects in five hours is a UX problem before it is anything else, so
 * this page is a worklist rather than a dashboard: what is left, what is done,
 * and one click into the next ballot. Nothing here decides what the judge may
 * see -- the queue the API returns already contains only their own ballots.
 */
export default async function JudgingQueuePage() {
  const me = await getMe();
  if (!me.authenticated) redirect('/login?next=/judging');

  const [assignments, records, certificates] = await Promise.all([
    api<Assignment[]>('/api/judging/queue'),
    api<Judge[]>('/api/judging/me'),
    api<Certificate[]>('/api/me/certificates'),
  ]);
  const judgingCerts = certificates.filter((c) => c.kind === 'judging' && c.valid);

  const outstanding = assignments.filter((a) => a.status !== 'complete');
  const done = assignments.filter((a) => a.status === 'complete');

  return (
    <main className="shell">
      <section style={{ padding: '28px 0 8px' }}>
        <p className="eyebrow">Judge console</p>
        <h1 style={{ fontSize: 32 }}>Your ballots</h1>
        {records.length === 0 ? (
          <p className="muted">
            You are not currently a judge on any event. An organizer has to invite you.
          </p>
        ) : (
          <p className="muted">
            {records.map((r) => (
              <span key={r.id} style={{ marginRight: 12 }}>
                {r.track ? (
                  <>
                    Judging the <strong>{r.track.name}</strong> track
                  </>
                ) : (
                  <>Judging all tracks</>
                )}
                {!r.is_active && ' (inactive)'}
              </span>
            ))}
          </p>
        )}
        {records.some((r) => r.is_active) && (
          <div className="row" style={{ marginTop: 4 }}>
            <Link className="button" href="/judging/calibration">
              Practice projects
            </Link>
          </div>
        )}
        {records.filter((r) => r.pairwise_enabled && r.is_active).length > 0 && (
          <div className="row" style={{ marginTop: 4 }}>
            {records
              .filter((r) => r.pairwise_enabled && r.is_active)
              .map((r) => (
                <Link
                  key={r.id}
                  className="button"
                  href={`/events/${r.event_slug}/pairwise`}
                >
                  Compare projects · {r.event_slug}
                </Link>
              ))}
          </div>
        )}
      </section>

      {assignments.length === 0 ? (
        <Empty>
          Nothing assigned yet. Ballots appear here once an organizer has run assignment.
        </Empty>
      ) : (
        <>
          <section>
            <h2 style={{ fontSize: 20 }}>
              To do <span className="muted">({outstanding.length})</span>
            </h2>
            {outstanding.length === 0 ? (
              <Empty>All of your ballots are in. Thank you.</Empty>
            ) : (
              <BallotList rows={outstanding} />
            )}
          </section>

          {done.length > 0 && (
            <section style={{ marginTop: 28 }}>
              <h2 style={{ fontSize: 20 }}>
                Done <span className="muted">({done.length})</span>
              </h2>
              <BallotList rows={done} />
            </section>
          )}
        </>
      )}

      {judgingCerts.length > 0 && (
        <section className="panel" style={{ marginTop: 28 }}>
          <h2 style={{ fontSize: 18, marginTop: 0 }}>Your certificates</h2>
          <p className="muted" style={{ fontSize: 13 }}>
            Issued by an organizer once your reviews are in -- not something you
            request yourself.
          </p>
          {judgingCerts.map((c) => (
            <p key={c.id} style={{ margin: '10px 0' }}>
              <Link href={`/verify?code=${c.code}`} className="button">
                {c.title}
              </Link>
            </p>
          ))}
        </section>
      )}
    </main>
  );
}

function BallotList({ rows }: { rows: Assignment[] }) {
  return (
    <div className="stack">
      {rows.map((a) => (
        <Link key={a.id} href={`/judging/${a.id}`} className="card spread">
          <div>
            <strong>{a.submission.name}</strong>
            {a.submission.track && <span className="tag">{a.submission.track.name}</span>}
            <div className="muted" style={{ fontSize: 13 }}>
              {a.submission.tagline ?? 'No tagline'}
            </div>
          </div>
          <div style={{ textAlign: 'right', whiteSpace: 'nowrap' }}>
            <StatusPill status={a.status} />
            {a.raw_score !== null && (
              <div className="muted" style={{ fontSize: 13 }}>
                your score {a.raw_score.toFixed(2)}
              </div>
            )}
            {a.completed_at && (
              <div className="muted" style={{ fontSize: 12 }}>
                {formatDateTime(a.completed_at)}
              </div>
            )}
          </div>
        </Link>
      ))}
    </div>
  );
}

function StatusPill({ status }: { status: Assignment['status'] }) {
  const label =
    status === 'complete' ? 'Complete' : status === 'in_progress' ? 'In progress' : 'Not started';
  // Reuses the existing pill modifiers rather than adding three more.
  const tone = status === 'complete' ? 'ok' : status === 'in_progress' ? 'warn' : '';
  return <span className={`pill ${tone}`.trim()}>{label}</span>;
}
