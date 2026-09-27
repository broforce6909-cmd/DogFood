import Link from 'next/link';
import { notFound } from 'next/navigation';

import {
  createWebhookAction,
  deleteWebhookAction,
  importCsvAction,
  issueCertificateForUserAction,
  issueCertificatesAction,
  revokeCertificateAction,
  testWebhookAction,
  toggleWebhookAction,
} from '../../../actions';
import { Empty, Pagination } from '../../../components';
import { api, PUBLIC_BASE } from '@/lib/api';
import { formatDateTime } from '@/lib/format';
import { getMe } from '@/lib/session';
import {
  atLeast,
  type Certificate,
  type Event,
  type Judge,
  type Page,
  type User,
  type Webhook,
} from '@/lib/types';

export const dynamic = 'force-dynamic';

export const metadata = { title: 'Integrations -- Dogfood' };

const IMPORTABLE = ['tracks', 'teams', 'submissions'] as const;
const EXPORTABLE = [
  'tracks',
  'teams',
  'submissions',
  'directory',
  'judges',
  'assignments',
  'scores',
  'results',
  'votes',
  'audit',
] as const;

/**
 * T4 in one organizer page: webhooks, records, bulk import and the embed snippet.
 *
 * One page rather than four, because these are all the same job — getting data and
 * events out of this install and into whatever else the organizer runs.
 */
export default async function IntegrationsPage({
  params,
  searchParams,
}: {
  params: Promise<{ slug: string }>;
  searchParams: Promise<{
    error?: string;
    secret?: string;
    ping?: string;
    issued?: string;
    updated?: string;
    cert?: string;
    cert_name?: string;
    cert_kind?: string;
    cert_fresh?: string;
    cert_at?: string;
    imported?: string;
    import_errors?: string;
    cert_page?: string;
  }>;
}) {
  const { slug } = await params;
  const query = await searchParams;
  const certPage = Number(query.cert_page) || 1;

  const me = await getMe();
  if (!atLeast(me.role, 'organizer')) notFound();

  const [event, hooks, certResult, topics, judges, organizerUsers, adminUsers] = await Promise.all([
    api<Event>(`/api/events/${slug}`),
    api<Webhook[]>(`/api/events/${slug}/webhooks`),
    api<Page<Certificate>>(`/api/events/${slug}/certificates?page=${certPage}`),
    api<string[]>(`/api/events/${slug}/webhooks/topics`),
    api<Judge[]>(`/api/events/${slug}/judges`),
    api<Page<User>>('/api/users?role=organizer&per_page=200'),
    api<Page<User>>('/api/users?role=admin&per_page=200'),
  ]);
  const certificates = certResult.items;
  // `role=` on /api/users is an exact match, but issue-for-user accepts anyone at
  // organizer rank or above -- so an admin is a valid subject too, and needs the
  // second query to be selectable.
  const organizers = [...organizerUsers.items, ...adminUsers.items].sort((a, b) =>
    a.display_name.localeCompare(b.display_name),
  );

  const embedSrc = `${PUBLIC_BASE}/embed/gallery/${slug}`;
  const snippet = `<script src="${embedSrc}.js"></script>`;

  return (
    <main className="shell">
      <section style={{ padding: '24px 0 4px' }}>
        <Link href={`/organizer/${slug}`} className="muted" style={{ fontSize: 13 }}>
          ← {event.name}
        </Link>
        <p className="eyebrow" style={{ marginTop: 10 }}>
          Integrations
        </p>
        <h1 style={{ fontSize: 30 }}>Getting data in and out</h1>
        <p className="muted">
          Webhooks, signed records, bulk import and an embeddable gallery. Everything
          here is also a documented API call — see{' '}
          <a href={`${PUBLIC_BASE}/docs`}>/docs</a>.
        </p>
      </section>

      {query.error && <p className="notice bad">{query.error}</p>}
      {query.ping && <p className="notice">Test delivery: {query.ping}</p>}
      {query.cert && (
        <p className="notice good">
          {query.cert_fresh === '1'
            ? `Issued a new ${query.cert_kind} certificate for ${query.cert_name}: `
            : `${query.cert_name} already has this ${query.cert_kind} certificate (issued ${formatDateTime(query.cert_at ?? '')}) -- codes are kept, nothing changed: `}
          <Link href={`/verify?code=${query.cert}`}>
            <code>{query.cert}</code>
          </Link>
          .
        </p>
      )}
      {query.issued !== undefined && (
        <p className="notice">
          Records: {query.issued} issued, {query.updated} updated.
        </p>
      )}
      {query.imported && (
        <p className="notice">
          Import: {query.imported}
          {query.import_errors && (
            <>
              <br />
              <strong>Errors:</strong> {query.import_errors}
            </>
          )}
        </p>
      )}
      {query.secret && (
        <p className="notice bad">
          <strong>Save this signing secret now — it is not shown again:</strong>
          <br />
          <code>{query.secret}</code>
          <br />
          Your endpoint verifies a delivery by computing{' '}
          <code>
            HMAC-SHA256(secret, {'"'}
            {'{timestamp}'}.{'"'} + body)
          </code>{' '}
          and comparing it
          to the <code>X-Dogfood-Signature</code> header.
        </p>
      )}

      {/* ---------------------------------------------------------------- */}
      <section className="panel">
        <h2 style={{ fontSize: 20, marginTop: 0 }}>Webhooks</h2>
        <p className="muted" style={{ fontSize: 13 }}>
          Every delivery is signed, and the timestamp is inside the signed material so
          a captured delivery cannot be replayed forever. Private and internal
          addresses are refused — the server will not call <code>localhost</code>, a
          LAN address, or a bare hostname, because a webhook form pointed inward is a
          request-forgery tool. See <code>THREAT-MODEL.md</code>.
        </p>

        {hooks.length === 0 ? (
          <Empty>No webhooks registered.</Empty>
        ) : (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>URL</th>
                  <th>Topics</th>
                  <th>Active</th>
                  <th>Failures</th>
                  <th>Last delivery</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {hooks.map((hook) => (
                  <tr key={hook.id} className={hook.is_active ? undefined : 'flagged'}>
                    <td style={{ wordBreak: 'break-all' }}>
                      {hook.url}
                      {hook.description && (
                        <div className="muted" style={{ fontSize: 12 }}>
                          {hook.description}
                        </div>
                      )}
                    </td>
                    <td className="muted" style={{ fontSize: 12 }}>
                      {hook.topics.length === 0 ? 'everything' : hook.topics.join(', ')}
                    </td>
                    <td>{hook.is_active ? 'yes' : 'no'}</td>
                    <td className={hook.failure_count > 0 ? 'down' : 'muted'}>
                      {hook.failure_count}
                    </td>
                    <td className="muted" style={{ fontSize: 12 }}>
                      {hook.last_delivery_at ? formatDateTime(hook.last_delivery_at) : '—'}
                    </td>
                    <td>
                      <div className="row">
                        <form action={testWebhookAction}>
                          <input type="hidden" name="slug" value={slug} />
                          <input type="hidden" name="hook_id" value={hook.id} />
                          <button type="submit" className="button small">
                            Test
                          </button>
                        </form>
                        <form action={toggleWebhookAction}>
                          <input type="hidden" name="slug" value={slug} />
                          <input type="hidden" name="hook_id" value={hook.id} />
                          <input
                            type="hidden"
                            name="is_active"
                            value={hook.is_active ? 'no' : 'yes'}
                          />
                          <button type="submit" className="button small">
                            {hook.is_active ? 'Disable' : 'Enable'}
                          </button>
                        </form>
                        <form action={deleteWebhookAction}>
                          <input type="hidden" name="slug" value={slug} />
                          <input type="hidden" name="hook_id" value={hook.id} />
                          <button type="submit" className="button small">
                            Remove
                          </button>
                        </form>
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}

        <details>
          <summary>Register a webhook</summary>
          <form action={createWebhookAction} className="stack" style={{ marginTop: 12 }}>
            <input type="hidden" name="slug" value={slug} />
            <label htmlFor="url">Endpoint URL</label>
            <input id="url" name="url" type="url" required placeholder="https://hooks.example.com/dogfood" />
            <label htmlFor="description">Description</label>
            <input id="description" name="description" placeholder="Slack relay" />
            <fieldset className="criterion">
              <legend>Topics</legend>
              <p className="muted" style={{ fontSize: 13, marginTop: 0 }}>
                Select none to receive everything.
              </p>
              <div className="scale" style={{ gap: 6 }}>
                {topics.map((topic) => (
                  <label key={topic} className="scale-option">
                    <input type="checkbox" name="topics" value={topic} />
                    <span style={{ fontSize: 12 }}>{topic}</span>
                  </label>
                ))}
              </div>
            </fieldset>
            <button type="submit" className="button primary">
              Register
            </button>
          </form>
        </details>
      </section>

      {/* ---------------------------------------------------------------- */}
      <section className="panel">
        <h2 style={{ fontSize: 20, marginTop: 0 }}>Certificates and records</h2>
        <p className="muted" style={{ fontSize: 13 }}>
          Every record is signed, and anybody can check one at{' '}
          <Link href="/verify">/verify</Link> with no account. Issuing is idempotent and
          keeps existing codes, so re-issuing never breaks a link somebody already
          shared.
        </p>
        <form action={issueCertificatesAction}>
          <input type="hidden" name="slug" value={slug} />
          <button type="submit" className="button primary">
            Issue records for everyone
          </button>
        </form>

        <p className="muted" style={{ fontSize: 13, marginTop: 18, marginBottom: 4 }}>
          Judges and organizers don&rsquo;t self-serve their own record the way a
          participant does -- an organizer issues it for them, on demand, here.
        </p>
        <div className="row" style={{ gap: 24, flexWrap: 'wrap' }}>
          {judges.length > 0 && (
            <form action={issueCertificateForUserAction} className="row">
              <input type="hidden" name="slug" value={slug} />
              <input type="hidden" name="kind" value="judging" />
              <select name="user_id" required defaultValue="" aria-label="Judge">
                <option value="" disabled>
                  Choose a judge…
                </option>
                {judges.map((j) => (
                  <option key={j.user.id} value={j.user.id}>
                    {j.user.display_name}
                  </option>
                ))}
              </select>
              <button type="submit" className="button small">
                Issue judging certificate
              </button>
            </form>
          )}
          {organizers.length > 0 && (
            <form action={issueCertificateForUserAction} className="row">
              <input type="hidden" name="slug" value={slug} />
              <input type="hidden" name="kind" value="organizing" />
              <select name="user_id" required defaultValue="" aria-label="Organizer">
                <option value="" disabled>
                  Choose an organizer…
                </option>
                {organizers.map((u) => (
                  <option key={u.id} value={u.id}>
                    {u.display_name}
                  </option>
                ))}
              </select>
              <button type="submit" className="button small">
                Issue organizing certificate
              </button>
            </form>
          )}
        </div>

        {certificates.length > 0 && (
          <div className="table-wrap" style={{ marginTop: 14 }}>
            <table>
              <thead>
                <tr>
                  <th>Code</th>
                  <th>Kind</th>
                  <th>Subject</th>
                  <th>Valid</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {certificates.map((cert) => (
                  <tr key={cert.id} className={cert.valid ? undefined : 'flagged'}>
                    <td>
                      <Link href={`/verify?code=${cert.code}`}>
                        <code>{cert.code}</code>
                      </Link>
                    </td>
                    <td className="muted">{cert.kind}</td>
                    <td>{cert.subject_name}</td>
                    <td>{cert.valid ? 'yes' : `revoked: ${cert.revoked_reason ?? ''}`}</td>
                    <td>
                      {cert.valid && (
                        <form action={revokeCertificateAction} className="row">
                          <input type="hidden" name="slug" value={slug} />
                          <input type="hidden" name="certificate_id" value={cert.id} />
                          <input
                            name="reason"
                            required
                            placeholder="reason"
                            style={{ width: 200 }}
                          />
                          <button type="submit" className="button small">
                            Revoke
                          </button>
                        </form>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
            <Pagination
              page={certResult.page}
              pages={certResult.pages}
              total={certResult.total}
              basePath={`/organizer/${slug}/integrations`}
              pageParam="cert_page"
              noun="record"
            />
          </div>
        )}
      </section>

      {/* ---------------------------------------------------------------- */}
      <section className="panel">
        <h2 style={{ fontSize: 20, marginTop: 0 }}>Bulk export and import</h2>
        <p className="muted" style={{ fontSize: 13 }}>
          The same column set in both directions, so an export re-imports without
          editing. Judging data and votes export but deliberately do <strong>not</strong>{' '}
          import — a ballot you can paste in is not a ballot.
        </p>

        <h3 style={{ fontSize: 15 }}>Export</h3>
        <ul className="inline-list">
          {EXPORTABLE.map((entity) => (
            <li key={entity}>
              <a href={`${PUBLIC_BASE}/api/events/${slug}/export/${entity}.csv`}>
                {entity}.csv
              </a>
            </li>
          ))}
        </ul>

        <h3 style={{ fontSize: 15 }}>Import</h3>
        <p className="muted" style={{ fontSize: 13 }}>
          Preview parses and validates the file and reports what would happen --
          nothing is written until you import for real.
        </p>
        <form action={importCsvAction} className="grid-form">
          <input type="hidden" name="slug" value={slug} />
          <label htmlFor="entity">Entity</label>
          <select id="entity" name="entity" defaultValue="submissions">
            {IMPORTABLE.map((entity) => (
              <option key={entity} value={entity}>
                {entity}
              </option>
            ))}
          </select>
          <label htmlFor="file">CSV file</label>
          <input id="file" name="file" type="file" accept=".csv,text/csv" required />
          <div />
          <div style={{ display: 'flex', gap: 8 }}>
            <button type="submit" name="intent" value="preview" className="button">
              Preview
            </button>
            <button type="submit" name="intent" value="import" className="button primary">
              Import
            </button>
          </div>
        </form>
      </section>

      {/* ---------------------------------------------------------------- */}
      <section className="panel">
        <h2 style={{ fontSize: 20, marginTop: 0 }}>Embeddable gallery</h2>
        <p className="muted" style={{ fontSize: 13 }}>
          Paste this on your event site. It renders in an iframe rather than injecting
          markup into your page, so our code never runs in your origin.
        </p>
        <textarea readOnly rows={2} value={snippet} />
        <p className="muted" style={{ fontSize: 13 }}>
          Or use the frame directly: <a href={embedSrc}>{embedSrc}</a> — supports{' '}
          <code>?limit=</code>, <code>?theme=dark</code> and <code>?track=</code>.
        </p>
        <iframe
          src={`${embedSrc}?limit=3`}
          title="Gallery preview"
          loading="lazy"
          style={{ width: '100%', minHeight: 260, border: '1px solid var(--line)', borderRadius: 8 }}
        />
      </section>
    </main>
  );
}
