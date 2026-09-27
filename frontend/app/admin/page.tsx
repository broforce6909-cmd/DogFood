import { notFound } from 'next/navigation';

import {
  createUserAction,
  deactivateUserAction,
  postAnnouncementFromAdminAction,
  reactivateUserAction,
  setUserRoleAction,
} from '../actions';
import { Empty, FormError, Pagination } from '../components';
import { api, apiOrNull } from '@/lib/api';
import { formatDateTime } from '@/lib/format';
import { getMe } from '@/lib/session';
import {
  atLeast,
  type AdminLevel,
  type Announcement,
  type EventSummary,
  type Page,
  type Role,
  type User,
} from '@/lib/types';

export const dynamic = 'force-dynamic';

export const metadata = { title: 'Admin -- Dogfood' };

const ROLES: Role[] = ['participant', 'judge', 'organizer', 'admin'];
// Self-registration already covers participant; provisioning a new account
// directly is for the roles that never go through that public form.
const PROVISIONABLE_ROLES: Role[] = ['judge', 'organizer', 'admin'];
const ADMIN_LEVELS: AdminLevel[] = ['owner', 'manager', 'auditor'];
const LEVEL_HELP: Record<AdminLevel, string> = {
  owner: 'everything, including account administration',
  manager: 'everything except account administration',
  auditor: 'read-only',
};

/**
 * Account administration: the one UI surface for `backend/app/routers/users.py`.
 *
 * Admin-only, and deliberately not folded into any organizer page -- promoting
 * someone to organizer or admin is a platform-wide grant, not one event's
 * business, the same distinction `users.py`'s own docstring draws for why this
 * router exists apart from `judges.py`.
 *
 * Self-registration only ever produces a `participant` (see `routers/auth.py`);
 * this page, backed by `PATCH /api/users/{id}/role`, is the only path to
 * `judge`, `organizer` or `admin` for anyone else.
 */
export default async function AdminUsersPage({
  searchParams,
}: {
  searchParams: Promise<{
    error?: string;
    created?: string;
    q?: string;
    role?: string;
    page?: string;
    event?: string;
  }>;
}) {
  const me = await getMe();
  if (!atLeast(me.role, 'admin')) notFound();

  const { error, created, q, role, page: pageParam, event: eventSlug } = await searchParams;
  const page = Number(pageParam) || 1;
  const params = new URLSearchParams();
  if (q) params.set('q', q);
  if (role) params.set('role', role);
  params.set('page', String(page));
  const [result, events, announcements] = await Promise.all([
    api<Page<User>>(`/api/users?${params}`),
    api<EventSummary[]>('/api/events'),
    eventSlug ? apiOrNull<Announcement[]>(`/api/events/${eventSlug}/announcements`) : null,
  ]);
  const users = result.items;
  // Hiding these controls is a courtesy; the API refuses the same requests for
  // any admin who is not an owner (see `app/access.py`).
  const level = me.user?.admin_level ?? null;
  const isOwner = level === 'owner';

  return (
    <main className="shell">
      <section style={{ padding: '28px 0 8px' }}>
        <p className="eyebrow">Admin</p>
        <h1 style={{ fontSize: 32 }}>Accounts</h1>
        <p className="muted">
          Role assignment and deactivation, platform-wide. Self-registration only
          ever produces a participant; this is the only path to judge, organizer
          or admin.
        </p>
      </section>

      <FormError message={error} />
      {!isOwner && (
        <div className="notice" style={{ marginBottom: 16 }}>
          You are signed in as {level === 'auditor' ? 'an auditor (read-only)' : 'a manager'}.
          Creating accounts, changing roles or levels, and deactivating are owner-only, so
          those controls are hidden here.
        </div>
      )}
      {created === '1' && (
        <div className="notice good" style={{ marginBottom: 16 }}>
          Account created. A temporary password was emailed to them -- this page never sees it.
        </div>
      )}

      {isOwner && (
      <section className="panel" style={{ marginBottom: 20 }}>
        <h2 style={{ fontSize: 18, marginTop: 0 }}>Provision an account</h2>
        <p className="muted small">
          For a judge or organizer who should never see the public registration form. A generated
          password is emailed to them directly -- nobody here types or sees it.
        </p>
        <form action={createUserAction} className="field-row">
          <div>
            <label htmlFor="new-email">Email</label>
            <input id="new-email" name="email" type="email" required />
          </div>
          <div>
            <label htmlFor="new-name">Name</label>
            <input id="new-name" name="display_name" required />
          </div>
          <div>
            <label htmlFor="new-role">Role</label>
            <select id="new-role" name="role" defaultValue="judge">
              {PROVISIONABLE_ROLES.map((r) => (
                <option key={r} value={r}>
                  {r}
                </option>
              ))}
            </select>
          </div>
          <div>
            <label htmlFor="new-level">Admin level (admins only)</label>
            <select id="new-level" name="admin_level" defaultValue="">
              <option value="">manager (default)</option>
              {ADMIN_LEVELS.map((l) => (
                <option key={l} value={l}>
                  {l} — {LEVEL_HELP[l]}
                </option>
              ))}
            </select>
          </div>
          <div style={{ alignSelf: 'end' }}>
            <button type="submit" className="primary">
              Create and email credentials
            </button>
          </div>
        </form>
      </section>
      )}

      <section className="panel" style={{ marginBottom: 20 }}>
        <h2 style={{ fontSize: 18, marginTop: 0 }}>Announcements</h2>
        <p className="muted small">
          Announcements are per-event, the same panel an organizer has on their own
          event page -- pick an event to read or post one from here instead.
        </p>
        <form method="GET" className="field-row" style={{ marginBottom: eventSlug ? 16 : 0 }}>
          <div>
            <label htmlFor="event">Event</label>
            <select id="event" name="event" defaultValue={eventSlug ?? ''}>
              <option value="" disabled>
                Choose an event…
              </option>
              {events.map((e) => (
                <option key={e.id} value={e.slug}>
                  {e.name}
                </option>
              ))}
            </select>
          </div>
          <div style={{ alignSelf: 'end' }}>
            <button type="submit">View</button>
          </div>
        </form>

        {eventSlug && (
          <>
            <form
              action={postAnnouncementFromAdminAction}
              className="stack"
              style={{ marginBottom: 16 }}
            >
              <input type="hidden" name="slug" value={eventSlug} />
              <div className="field">
                <label htmlFor="announcement-title">Title</label>
                <input id="announcement-title" name="title" required maxLength={200} />
              </div>
              <div className="field">
                <label htmlFor="announcement-body">Message</label>
                <textarea id="announcement-body" name="body" required rows={3} />
              </div>
              <label className="row" style={{ alignItems: 'center', gap: 6 }}>
                <input type="checkbox" name="visible_to_visitors" />
                Also show to signed-out visitors
              </label>
              <button type="submit" className="button primary">
                Post
              </button>
            </form>
            {!announcements || announcements.length === 0 ? (
              <Empty>No announcements posted yet for {eventSlug}.</Empty>
            ) : (
              <div className="stack" style={{ gap: 12 }}>
                {announcements.map((announcement) => (
                  <div key={announcement.id} className="table-wrap" style={{ padding: '10px 0' }}>
                    <div className="spread">
                      <p style={{ margin: 0, fontWeight: 600 }}>{announcement.title}</p>
                      <span className={`pill ${announcement.visible_to_visitors ? 'ok' : ''}`}>
                        {announcement.visible_to_visitors ? 'everyone' : 'participants'}
                      </span>
                    </div>
                    <p className="muted small" style={{ margin: '2px 0 6px' }}>
                      {formatDateTime(announcement.posted_at)}
                      {announcement.posted_by_display_name &&
                        ` · ${announcement.posted_by_display_name}`}
                    </p>
                    <p style={{ margin: 0 }}>{announcement.body}</p>
                  </div>
                ))}
              </div>
            )}
          </>
        )}
      </section>

      <form method="GET" className="field-row" style={{ marginBottom: 16 }}>
        <div>
          <label htmlFor="q">Search</label>
          <input id="q" name="q" defaultValue={q ?? ''} placeholder="email or name" />
        </div>
        <div>
          <label htmlFor="role">Role</label>
          <select id="role" name="role" defaultValue={role ?? ''}>
            <option value="">Any</option>
            {ROLES.map((r) => (
              <option key={r} value={r}>
                {r}
              </option>
            ))}
          </select>
        </div>
        <div style={{ alignSelf: 'end' }}>
          <button type="submit">Filter</button>
        </div>
      </form>

      {users.length === 0 ? (
        <Empty>No accounts match.</Empty>
      ) : (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Name</th>
                <th>Email</th>
                <th>Role</th>
                <th>Status</th>
                <th>Joined</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {users.map((u) => (
                <tr key={u.id} className={u.is_active ? undefined : 'muted'}>
                  <td>{u.display_name}</td>
                  <td className="muted">{u.email}</td>
                  <td>
                    <form action={setUserRoleAction} className="row" style={{ gap: 6 }}>
                      <input type="hidden" name="user_id" value={u.id} />
                      {/* A disabled <select> is not submitted, and the API needs the
                          role to accompany a level change on your own row. */}
                      {u.id === me.user?.id && <input type="hidden" name="role" value={u.role} />}
                      <select
                        name="role"
                        defaultValue={u.role}
                        disabled={!isOwner || u.id === me.user?.id}
                      >
                        {ROLES.map((r) => (
                          <option key={r} value={r}>
                            {r}
                          </option>
                        ))}
                      </select>
                      {u.role === 'admin' && (
                        <select
                          name="admin_level"
                          defaultValue={u.admin_level ?? 'manager'}
                          disabled={!isOwner}
                          aria-label={`Admin level for ${u.display_name}`}
                        >
                          {ADMIN_LEVELS.map((l) => (
                            <option key={l} value={l}>
                              {l}
                            </option>
                          ))}
                        </select>
                      )}
                      {isOwner && (u.id !== me.user?.id || u.role === 'admin') && (
                        <button type="submit" className="quiet small">
                          Save
                        </button>
                      )}
                    </form>
                  </td>
                  <td>
                    <span className={`pill ${u.is_active ? 'ok' : 'warn'}`}>
                      {u.is_active ? 'Active' : 'Deactivated'}
                    </span>
                  </td>
                  <td className="muted small">{formatDateTime(u.created_at)}</td>
                  <td style={{ textAlign: 'right' }}>
                    {u.id === me.user?.id ? (
                      <span className="muted small">You</span>
                    ) : !isOwner ? null : u.is_active ? (
                      <form action={deactivateUserAction}>
                        <input type="hidden" name="user_id" value={u.id} />
                        <button type="submit" className="quiet small">
                          Deactivate
                        </button>
                      </form>
                    ) : (
                      <form action={reactivateUserAction}>
                        <input type="hidden" name="user_id" value={u.id} />
                        <button type="submit" className="quiet small">
                          Reactivate
                        </button>
                      </form>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          <Pagination
            page={result.page}
            pages={result.pages}
            total={result.total}
            basePath="/admin"
            extraParams={{ q, role }}
            noun="account"
          />
        </div>
      )}
    </main>
  );
}
