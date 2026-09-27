import Link from 'next/link';
import { notFound } from 'next/navigation';

import {
  leaveTeamAction,
  removeMemberForCauseAction,
  rotateInviteAction,
  startSubmissionAction,
} from '../../actions';
import { FormError } from '../../components';
import { apiOrNull } from '@/lib/api';
import { formatDate } from '@/lib/format';
import { getMe } from '@/lib/session';
import { atLeast, type Event, type Invite, type Team } from '@/lib/types';

export const dynamic = 'force-dynamic';

export const metadata = { title: 'Your team -- Dogfood' };

export default async function TeamPage({
  params,
  searchParams,
}: {
  params: Promise<{ id: string }>;
  searchParams: Promise<{ error?: string }>;
}) {
  const { id } = await params;
  const { error } = await searchParams;

  const team = await apiOrNull<Team>(`/api/teams/${id}`);
  if (!team) notFound();

  const me = await getMe();
  // The invite endpoint is separately authorized, so asking for it is also how
  // we find out whether this viewer is entitled to it. `null` means no.
  const [invite, event] = await Promise.all([
    apiOrNull<Invite>(`/api/teams/${id}/invite`),
    apiOrNull<Event>(`/api/events/${team.event_slug}`),
  ]);

  const membership = team.members.find((member) => member.user.id === me.user?.id);

  return (
    <main className="shell narrow">
      <p className="eyebrow">Team</p>
      <h1>{team.name}</h1>
      <p className="muted small">Formed {formatDate(team.created_at)}</p>
      <FormError message={error} />

      <section className="panel" style={{ marginTop: 18 }}>
        <h2>Members</h2>
        <div className="table-wrap">
          <table>
            <tbody>
              {team.members.map((member) => (
                <tr key={member.user.id}>
                  <td>{member.user.display_name}</td>
                  <td className="muted">{member.team_role}</td>
                  <td style={{ textAlign: 'right' }}>
                    {member.user.id === me.user?.id && (
                      <form action={leaveTeamAction}>
                        <input type="hidden" name="team_id" value={team.id} />
                        <input type="hidden" name="user_id" value={member.user.id} />
                        <button className="quiet small" type="submit">
                          Leave
                        </button>
                      </form>
                    )}
                    {member.user.id !== me.user?.id && atLeast(me.role, 'organizer') && (
                      <form action={removeMemberForCauseAction} className="row">
                        <input type="hidden" name="team_id" value={team.id} />
                        <input type="hidden" name="user_id" value={member.user.id} />
                        <input
                          name="reason"
                          required
                          placeholder="reason (recorded in the audit log)"
                          style={{ width: 220 }}
                        />
                        <button className="quiet small" type="submit">
                          Remove
                        </button>
                      </form>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </section>

      {invite && (
        <section className="panel" style={{ marginTop: 16 }}>
          <h2>Invite link</h2>
          <p className="muted small">
            Anyone with this link can join the team. Rotate it if it ends up somewhere public --
            the old link stops working immediately.
          </p>
          <pre className="code">{invite.url}</pre>
          {membership?.team_role === 'owner' && (
            <form action={rotateInviteAction} style={{ marginTop: 10 }}>
              <input type="hidden" name="team_id" value={team.id} />
              <button type="submit">Rotate link</button>
            </form>
          )}
        </section>
      )}

      <section className="panel" style={{ marginTop: 16 }}>
        <h2>Project</h2>
        {team.submission_id ? (
          <p style={{ margin: 0 }}>
            <Link href={`/projects/${team.submission_id}`}>View</Link>
            {' · '}
            <Link href={`/submissions/${team.submission_id}/edit`}>Edit</Link>
          </p>
        ) : membership ? (
          event && !event.submissions_open ? (
            <p className="muted small" style={{ margin: 0 }}>
              Submissions for this event are closed, so no new draft can be started.
            </p>
          ) : (
            <form action={startSubmissionAction} className="stack">
              <input type="hidden" name="team_id" value={team.id} />
              <div className="field">
                <label htmlFor="project-name">Project name</label>
                <input id="project-name" name="name" type="text" required maxLength={160} />
              </div>
              <button className="primary" type="submit">
                Start a draft
              </button>
            </form>
          )
        ) : (
          <p className="muted small" style={{ margin: 0 }}>
            This team has not entered a project yet.
          </p>
        )}
      </section>
    </main>
  );
}
