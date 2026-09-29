import Link from 'next/link';
import { notFound } from 'next/navigation';

import { Empty } from '../../../components';
import { api, PUBLIC_BASE } from '@/lib/api';
import { formatDateTime } from '@/lib/format';
import { getMe } from '@/lib/session';
import { atLeast, type AuditPage, type Event } from '@/lib/types';

export const dynamic = 'force-dynamic';

export const metadata = { title: 'Audit log -- Dogfood' };

const PER_PAGE = 50;

/**
 * The audit trail.
 *
 * The brief's requirement is about the reader: _an audit trail an organizer can read
 * without a database client_. So the `summary` sentence is the widest column and the
 * structured fields are secondary — the opposite of how a log viewer is usually
 * built, and the whole reason this page exists rather than a link to a CSV.
 */
export default async function AuditPageView({
  params,
  searchParams,
}: {
  params: Promise<{ slug: string }>;
  searchParams: Promise<{ action?: string; actor?: string; page?: string }>;
}) {
  const { slug } = await params;
  const { action, actor, page } = await searchParams;

  const me = await getMe();
  if (!atLeast(me.role, 'organizer')) notFound();

  const query = new URLSearchParams({ per_page: String(PER_PAGE) });
  if (action) query.set('action', action);
  if (actor) query.set('actor', actor);
  if (page) query.set('page', page);

  const [event, log, actions] = await Promise.all([
    api<Event>(`/api/events/${slug}`),
    api<AuditPage>(`/api/events/${slug}/audit?${query.toString()}`),
    api<string[]>(`/api/events/${slug}/audit/actions`),
  ]);

  return (
    <main className="shell">
      <section style={{ padding: '24px 0 4px' }}>
        <Link href={`/organizer/${slug}`} className="muted" style={{ fontSize: 13 }}>
          ← {event.name}
        </Link>
        <p className="eyebrow" style={{ marginTop: 10 }}>
          Audit trail · organizers only
        </p>
        <h1 style={{ fontSize: 30 }}>What happened</h1>
        <p className="muted">
          {log.total} recorded action{log.total === 1 ? '' : 's'}, newest first. This log
          is append-only: nothing in the product writes it by hand, and nothing edits or
          deletes an entry.
        </p>
      </section>

      <section className="panel">
        <form method="GET" className="grid-form">
          <label htmlFor="action">Action</label>
          <select id="action" name="action" defaultValue={action ?? ''}>
            <option value="">Everything</option>
            {actions.map((value) => (
              <option key={value} value={value}>
                {value.replace(/_/g, ' ')}
              </option>
            ))}
          </select>
          <label htmlFor="actor">Actor</label>
          <input
            id="actor"
            name="actor"
            defaultValue={actor ?? ''}
            placeholder="email, or part of one"
          />
          <div />
          <div className="row">
            <button type="submit" className="button primary">
              Filter
            </button>
            {(action || actor) && (
              <Link className="button" href={`/organizer/${slug}/audit`}>
                Clear
              </Link>
            )}
            <a className="button" href={`${PUBLIC_BASE}/api/events/${slug}/export/audit.csv`}>
              audit.csv
            </a>
          </div>
        </form>
      </section>

      <section className="panel">
        {log.rows.length === 0 ? (
          <Empty>
            Nothing matches. Sensitive actions — votes, comments, moderation, scoring,
            deadline overrides — appear here as they happen.
          </Empty>
        ) : (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th style={{ whiteSpace: 'nowrap' }}>When</th>
                  <th>What happened</th>
                  <th>Action</th>
                  <th>IP</th>
                </tr>
              </thead>
              <tbody>
                {log.rows.map((row) => (
                  <tr key={row.id}>
                    <td className="muted" style={{ whiteSpace: 'nowrap', fontSize: 13 }}>
                      {formatDateTime(row.created_at)}
                    </td>
                    {/* The sentence, and the widest column on the page. */}
                    <td>{row.summary}</td>
                    <td className="muted" style={{ fontSize: 12, whiteSpace: 'nowrap' }}>
                      {row.action.replace(/_/g, ' ')}
                    </td>
                    <td className="muted" style={{ fontSize: 12 }}>
                      {row.ip_address ?? '—'}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}

        {log.pages > 1 && (
          <div className="row" style={{ marginTop: 14 }}>
            {log.page > 1 && (
              <Link
                className="button"
                href={`/organizer/${slug}/audit?${new URLSearchParams({
                  ...(action ? { action } : {}),
                  ...(actor ? { actor } : {}),
                  page: String(log.page - 1),
                }).toString()}`}
              >
                ← Newer
              </Link>
            )}
            <span className="muted" style={{ fontSize: 13 }}>
              Page {log.page} of {log.pages}
            </span>
            {log.page < log.pages && (
              <Link
                className="button"
                href={`/organizer/${slug}/audit?${new URLSearchParams({
                  ...(action ? { action } : {}),
                  ...(actor ? { actor } : {}),
                  page: String(log.page + 1),
                }).toString()}`}
              >
                Older →
              </Link>
            )}
          </div>
        )}
      </section>

      <p className="muted" style={{ fontSize: 13 }}>
        Known limit, stated here rather than only in the docs: the log is append-only
        through the API and every entry is hash-chained to the one before it, so a
        silently edited or deleted row breaks the chain (an admin can check it at{' '}
        <code>GET /api/audit/verify</code>). Someone with database access who rewrites
        the whole chain forward would leave nothing inside the database to catch; the
        newest hash is also written to the API&apos;s own log for that reason. See{' '}
        <code>THREAT-MODEL.md</code> §6.
      </p>
    </main>
  );
}
