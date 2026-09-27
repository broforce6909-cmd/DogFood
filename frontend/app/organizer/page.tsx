import Link from 'next/link';
import { notFound } from 'next/navigation';

import { Empty } from '../components';
import { api } from '@/lib/api';
import { formatDateTime, untilDeadline } from '@/lib/format';
import { getMe } from '@/lib/session';
import { atLeast, type Certificate, type EventSummary } from '@/lib/types';

export const dynamic = 'force-dynamic';

export const metadata = { title: 'Organize -- Dogfood' };

export default async function OrganizerPage() {
  const me = await getMe();
  // Hiding the nav link is a courtesy; this is the page saying no. The API
  // would say no too, on every request this page could make.
  if (!atLeast(me.role, 'organizer')) notFound();

  const [events, archivedEvents, certificates] = await Promise.all([
    api<EventSummary[]>('/api/events'),
    api<EventSummary[]>('/api/events?archived=only'),
    api<Certificate[]>('/api/me/certificates'),
  ]);
  const organizingCerts = certificates.filter((c) => c.kind === 'organizing' && c.valid);

  return (
    <main className="shell">
      <p className="eyebrow">Organizer</p>
      <div className="spread">
        <h1>Events</h1>
        <Link className="button primary" href="/organizer/new">
          New event
        </Link>
      </div>

      {events.length === 0 ? (
        <Empty>No events yet.</Empty>
      ) : (
        <div className="table-wrap panel" style={{ marginTop: 18 }}>
          <table>
            <thead>
              <tr>
                <th>Event</th>
                <th>Status</th>
                <th>Deadline</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {events.map((event) => (
                <tr key={event.id}>
                  <td>
                    <Link href={`/organizer/${event.slug}`}>{event.name}</Link>
                    <br />
                    <span className="muted small">{event.slug}</span>
                  </td>
                  <td>
                    {event.is_published ? (
                      <span className="pill ok">Published</span>
                    ) : (
                      <span className="pill warn">Draft</span>
                    )}
                  </td>
                  <td className="small">
                    {formatDateTime(event.submission_deadline)}
                    <br />
                    <span className="muted">{untilDeadline(event.submission_deadline)}</span>
                  </td>
                  <td style={{ textAlign: 'right' }}>
                    <Link className="small" href={`/events/${event.slug}/gallery`}>
                      Gallery
                    </Link>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {archivedEvents.length > 0 && (
        <section className="panel" style={{ marginTop: 18 }}>
          <h2 style={{ fontSize: 18, marginTop: 0 }}>Archived</h2>
          <p className="muted" style={{ fontSize: 13 }}>
            Frozen and read-only. Open one to unarchive it.
          </p>
          {archivedEvents.map((event) => (
            <p key={event.id} style={{ margin: '8px 0' }}>
              <Link href={`/organizer/${event.slug}`}>{event.name}</Link>{' '}
              <span className="muted small">
                {event.slug}
                {event.archived_at && ` · archived ${formatDateTime(event.archived_at)}`}
              </span>
            </p>
          ))}
        </section>
      )}

      {organizingCerts.length > 0 && (
        <section className="panel" style={{ marginTop: 18 }}>
          <h2 style={{ fontSize: 18, marginTop: 0 }}>Your certificates</h2>
          <p className="muted" style={{ fontSize: 13 }}>
            Issued to you directly, not requested through the participant flow.
          </p>
          {organizingCerts.map((c) => (
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
