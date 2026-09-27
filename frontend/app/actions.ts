'use server';

/**
 * Server actions: the only way this app writes anything.
 *
 * Each one reads a plain HTML form, calls the API with the caller's session,
 * and redirects. On failure it redirects back with `?error=`, so every form
 * works with JavaScript switched off and no state lives in the browser.
 *
 * None of these functions decides whether the action is allowed. They call the
 * API and report what it said -- a participant who forges a `team_id` gets the
 * same 403 the UI was trying to spare them.
 */

import { cookies } from 'next/headers';
import { redirect } from 'next/navigation';
import { revalidatePath } from 'next/cache';

import { ApiError, VOTER_COOKIE, api, apiRaw } from '@/lib/api';
import { clearSessionCookie, setSessionCookie } from '@/lib/session';
import type {
  AssignResult,
  Assignment,
  Ballot,
  Certificate,
  Event,
  PairwiseComparison,
  Submission,
  Team,
  User,
  Voter,
} from '@/lib/types';

type SessionResponse = { token: string; expires_at: string };

function text(form: FormData, key: string): string {
  return String(form.get(key) ?? '').trim();
}

function optional(form: FormData, key: string): string | null {
  const value = text(form, key);
  return value === '' ? null : value;
}

/** `<input type="datetime-local">` gives a naive local string; we mean UTC. */
function toIso(form: FormData, key: string): string {
  const value = text(form, key);
  if (!value) return value;
  return value.length === 16 ? `${value}:00Z` : `${value}Z`;
}

function optionalIso(form: FormData, key: string): string | null {
  const value = toIso(form, key);
  return value === '' ? null : value;
}

function failure(back: string, error: unknown): string {
  const message = error instanceof ApiError ? error.text : 'Something went wrong';
  const separator = back.includes('?') ? '&' : '?';
  return `${back}${separator}error=${encodeURIComponent(message)}`;
}

// --------------------------------------------------------------------------- //
// Auth
// --------------------------------------------------------------------------- //

export async function registerAction(form: FormData): Promise<void> {
  const next = text(form, 'next') || '/dashboard';
  let target: string;
  try {
    const session = await api<SessionResponse>('/api/auth/register', {
      method: 'POST',
      body: {
        email: text(form, 'email'),
        display_name: text(form, 'display_name'),
        password: text(form, 'password'),
      },
    });
    await setSessionCookie(session.token, session.expires_at);
    target = next;
  } catch (error) {
    target = failure('/register', error);
  }
  revalidatePath('/', 'layout');
  redirect(target);
}

export async function loginAction(form: FormData): Promise<void> {
  const next = text(form, 'next') || '/dashboard';
  // Present only when the person explicitly picked a tab. The API then refuses
  // (without signing in) an account whose real role is a different one.
  const as = optional(form, 'as');
  let target: string;
  try {
    const session = await api<SessionResponse>('/api/auth/login', {
      method: 'POST',
      body: {
        email: text(form, 'email'),
        password: text(form, 'password'),
        expected_role: as,
      },
    });
    await setSessionCookie(session.token, session.expires_at);
    target = next;
  } catch (error) {
    // Back to the same tab, and to the same `next`, so the message can be acted
    // on -- pick the right tab and sign in again -- without losing where they
    // were headed.
    const query = new URLSearchParams();
    if (as) query.set('as', as);
    if (next !== '/dashboard') query.set('next', next);
    const qs = query.toString();
    target = failure(qs ? `/login?${qs}` : '/login', error);
  }
  revalidatePath('/', 'layout');
  redirect(target);
}

export async function logoutAction(): Promise<void> {
  try {
    await api<void>('/api/auth/logout', { method: 'POST' });
  } catch {
    // A session the server has already forgotten is still a logout.
  }
  await clearSessionCookie();
  revalidatePath('/', 'layout');
  redirect('/');
}

// --------------------------------------------------------------------------- //
// Admin: accounts
// --------------------------------------------------------------------------- //

/** Admin-only, and the one path to a judge/organizer/admin account that does
 * not start with the public registration form -- see `routers/users.py`'s
 * `create_user`. A generated password is emailed to the new account; this
 * action never sees it. */
export async function createUserAction(form: FormData): Promise<void> {
  const back = '/admin';
  let target = `${back}?created=1`;
  try {
    await api<User>('/api/users', {
      method: 'POST',
      body: {
        email: text(form, 'email'),
        display_name: text(form, 'display_name'),
        role: text(form, 'role'),
        admin_level: optional(form, 'admin_level'),
      },
    });
  } catch (error) {
    target = failure(back, error);
  }
  revalidatePath(back);
  redirect(target);
}

export async function setUserRoleAction(form: FormData): Promise<void> {
  const userId = text(form, 'user_id');
  const back = '/admin';
  let target = back;
  try {
    await api<User>(`/api/users/${userId}/role`, {
      method: 'PATCH',
      body: { role: text(form, 'role'), admin_level: optional(form, 'admin_level') },
    });
  } catch (error) {
    target = failure(back, error);
  }
  revalidatePath(back);
  redirect(target);
}

export async function deactivateUserAction(form: FormData): Promise<void> {
  const userId = text(form, 'user_id');
  const back = '/admin';
  let target = back;
  try {
    await api<User>(`/api/users/${userId}/deactivate`, { method: 'POST' });
  } catch (error) {
    target = failure(back, error);
  }
  revalidatePath(back);
  redirect(target);
}

export async function reactivateUserAction(form: FormData): Promise<void> {
  const userId = text(form, 'user_id');
  const back = '/admin';
  let target = back;
  try {
    await api<User>(`/api/users/${userId}/reactivate`, { method: 'POST' });
  } catch (error) {
    target = failure(back, error);
  }
  revalidatePath(back);
  redirect(target);
}

// --------------------------------------------------------------------------- //
// Teams
// --------------------------------------------------------------------------- //

export async function registerForEventAction(form: FormData): Promise<void> {
  const slug = text(form, 'event_slug');
  const back = `/events/${slug}`;
  let target = back;
  try {
    const isTeamLeader = form.get('is_team_leader') === 'on';
    await api(`/api/events/${slug}/register`, {
      method: 'POST',
      body: {
        email: optional(form, 'email'),
        discord_username: text(form, 'discord_username'),
        is_team_leader: isTeamLeader,
        // The API rejects a leader_name sent without is_team_leader, the same
        // rule this omits from a plain checkbox -- unchecked means no field
        // at all, not an empty string.
        ...(isTeamLeader ? { leader_name: text(form, 'leader_name') } : {}),
      },
    });
  } catch (error) {
    target = failure(back, error);
  }
  revalidatePath(back);
  redirect(target);
}

/** Staff only, for cause. A hard delete -- unlike a judge or a submission, no
 * ballots or scores hang off a registration row. See `remove_registration` in
 * `routers/registrations.py`. */
export async function removeRegistrationAction(form: FormData): Promise<void> {
  const slug = text(form, 'event_slug');
  const back = `/organizer/${slug}`;
  try {
    await api(`/api/events/${slug}/registrations/${text(form, 'registration_id')}/remove`, {
      method: 'POST',
      body: { reason: text(form, 'reason') },
    });
  } catch (error) {
    redirect(failure(back, error));
  }
  revalidatePath(back);
  redirect(back);
}

export async function postAnnouncementAction(form: FormData): Promise<void> {
  const slug = text(form, 'slug');
  const back = `/organizer/${slug}`;
  try {
    await api(`/api/events/${slug}/announcements`, {
      method: 'POST',
      body: {
        title: text(form, 'title'),
        body: text(form, 'body'),
        visible_to_visitors: form.get('visible_to_visitors') === 'on',
      },
    });
  } catch (error) {
    redirect(failure(back, error));
  }
  revalidatePath(back);
  revalidatePath(`/events/${slug}`);
  redirect(back);
}

/** Same write as `postAnnouncementAction`, for the admin surface specifically
 * -- the only difference is where it sends the admin back to, since `/admin`
 * has no event in its own URL the way `/organizer/{slug}` does. */
export async function postAnnouncementFromAdminAction(form: FormData): Promise<void> {
  const slug = text(form, 'slug');
  const back = `/admin?event=${slug}`;
  try {
    await api(`/api/events/${slug}/announcements`, {
      method: 'POST',
      body: {
        title: text(form, 'title'),
        body: text(form, 'body'),
        visible_to_visitors: form.get('visible_to_visitors') === 'on',
      },
    });
  } catch (error) {
    redirect(failure(back, error));
  }
  revalidatePath(back);
  revalidatePath(`/events/${slug}`);
  redirect(back);
}

export async function createTeamAction(form: FormData): Promise<void> {
  const slug = text(form, 'event_slug');
  let target: string;
  try {
    const team = await api<Team>(`/api/events/${slug}/teams`, {
      method: 'POST',
      body: { name: text(form, 'name') },
    });
    target = `/teams/${team.id}`;
  } catch (error) {
    target = failure(`/events/${slug}`, error);
  }
  revalidatePath('/dashboard');
  redirect(target);
}

export async function joinTeamAction(form: FormData): Promise<void> {
  const token = text(form, 'token');
  let target: string;
  try {
    const team = await api<Team>('/api/teams/join', { method: 'POST', body: { token } });
    target = `/teams/${team.id}`;
  } catch (error) {
    target = failure(`/join/${token}`, error);
  }
  revalidatePath('/dashboard');
  redirect(target);
}

export async function rotateInviteAction(form: FormData): Promise<void> {
  const teamId = text(form, 'team_id');
  let target = `/teams/${teamId}`;
  try {
    await api(`/api/teams/${teamId}/invite/rotate`, { method: 'POST' });
  } catch (error) {
    target = failure(target, error);
  }
  revalidatePath(`/teams/${teamId}`);
  redirect(target);
}

export async function leaveTeamAction(form: FormData): Promise<void> {
  const teamId = text(form, 'team_id');
  const userId = text(form, 'user_id');
  let target = '/dashboard';
  try {
    await api(`/api/teams/${teamId}/members/${userId}`, { method: 'DELETE' });
  } catch (error) {
    target = failure(`/teams/${teamId}`, error);
  }
  revalidatePath('/dashboard');
  redirect(target);
}

/** Staff only, for cause. Distinct from `leaveTeamAction`'s plain DELETE --
 * see `remove_member_for_cause` in `routers/teams.py` for why. */
export async function removeMemberForCauseAction(form: FormData): Promise<void> {
  const teamId = text(form, 'team_id');
  const back = `/teams/${teamId}`;
  try {
    await api(`/api/teams/${teamId}/members/${text(form, 'user_id')}/remove`, {
      method: 'POST',
      body: { reason: text(form, 'reason') },
    });
  } catch (error) {
    redirect(failure(back, error));
  }
  revalidatePath(back);
  redirect(back);
}

// --------------------------------------------------------------------------- //
// Submissions
// --------------------------------------------------------------------------- //

export async function startSubmissionAction(form: FormData): Promise<void> {
  const teamId = text(form, 'team_id');
  let target: string;
  try {
    const submission = await api<Submission>('/api/submissions', {
      method: 'POST',
      body: { team_id: teamId, name: text(form, 'name') },
    });
    target = `/submissions/${submission.id}/edit`;
  } catch (error) {
    target = failure(`/teams/${teamId}`, error);
  }
  revalidatePath('/dashboard');
  redirect(target);
}

function lines(form: FormData, key: string): string[] {
  return text(form, key)
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter(Boolean);
}

function commas(form: FormData, key: string): string[] {
  return text(form, key)
    .split(',')
    .map((item) => item.trim())
    .filter(Boolean);
}

export async function saveSubmissionAction(form: FormData): Promise<void> {
  const id = text(form, 'submission_id');
  const back = `/submissions/${id}/edit`;

  const answers: Record<string, string> = {};
  for (const [key, value] of form.entries()) {
    if (key.startsWith('answer:')) answers[key.slice('answer:'.length)] = String(value);
  }

  let target = back;
  try {
    await api<Submission>(`/api/submissions/${id}`, {
      method: 'PATCH',
      body: {
        name: text(form, 'name'),
        tagline: optional(form, 'tagline'),
        description: optional(form, 'description'),
        thumbnail_url: optional(form, 'thumbnail_url'),
        gallery_image_urls: lines(form, 'gallery_image_urls'),
        demo_video_url: optional(form, 'demo_video_url'),
        repo_url: optional(form, 'repo_url'),
        live_url: optional(form, 'live_url'),
        linkedin_url: optional(form, 'linkedin_url'),
        tech_tags: commas(form, 'tech_tags'),
        discord_usernames: commas(form, 'discord_usernames'),
        track_id: optional(form, 'track_id'),
        answers,
      },
    });
    target = `${back}?saved=1`;
  } catch (error) {
    target = failure(back, error);
  }
  revalidatePath(back);
  redirect(target);
}

export async function submitSubmissionAction(form: FormData): Promise<void> {
  const id = text(form, 'submission_id');
  const back = `/submissions/${id}/edit`;
  let target: string;
  try {
    await api<Submission>(`/api/submissions/${id}/submit`, { method: 'POST' });
    target = `/projects/${id}`;
  } catch (error) {
    if (error instanceof ApiError && error.missing.length > 0) {
      // The API returns every blocker at once; show them all rather than one.
      target = `${back}?missing=${encodeURIComponent(error.missing.join('|'))}`;
    } else {
      target = failure(back, error);
    }
  }
  revalidatePath(back);
  redirect(target);
}

export async function unsubmitSubmissionAction(form: FormData): Promise<void> {
  const id = text(form, 'submission_id');
  const back = `/submissions/${id}/edit`;
  let target = back;
  try {
    await api<Submission>(`/api/submissions/${id}/unsubmit`, { method: 'POST' });
  } catch (error) {
    target = failure(back, error);
  }
  revalidatePath(back);
  redirect(target);
}

/** Staff only, for cause. The row, its ballots and its votes are kept --
 * only the status changes. See `disqualify_submission` in
 * `routers/submissions.py`. */
export async function disqualifySubmissionAction(form: FormData): Promise<void> {
  const slug = text(form, 'slug');
  const back = `/organizer/${slug}`;
  try {
    await api(`/api/submissions/${text(form, 'submission_id')}/disqualify`, {
      method: 'POST',
      body: { reason: text(form, 'reason') },
    });
  } catch (error) {
    redirect(failure(back, error));
  }
  revalidatePath(back);
  redirect(back);
}

/** Undoes a disqualification -- also staff only, also reasoned, so there is
 * an audit entry either direction. */
export async function reinstateSubmissionAction(form: FormData): Promise<void> {
  const slug = text(form, 'slug');
  const back = `/organizer/${slug}`;
  try {
    await api(`/api/submissions/${text(form, 'submission_id')}/reinstate`, {
      method: 'POST',
      body: { reason: text(form, 'reason') },
    });
  } catch (error) {
    redirect(failure(back, error));
  }
  revalidatePath(back);
  redirect(back);
}

// --------------------------------------------------------------------------- //
// Organizer
// --------------------------------------------------------------------------- //

export async function createEventAction(form: FormData): Promise<void> {
  let target: string;
  try {
    const event = await api<Event>('/api/events', {
      method: 'POST',
      body: {
        slug: text(form, 'slug'),
        name: text(form, 'name'),
        tagline: optional(form, 'tagline'),
        description: optional(form, 'description'),
        starts_at: toIso(form, 'starts_at'),
        ends_at: toIso(form, 'ends_at'),
        registration_opens_at: toIso(form, 'registration_opens_at'),
        submission_opens_at: toIso(form, 'submission_opens_at'),
        submission_deadline: toIso(form, 'submission_deadline'),
        judging_opens_at: optionalIso(form, 'judging_opens_at'),
        judging_closes_at: optionalIso(form, 'judging_closes_at'),
        max_team_size: Number(text(form, 'max_team_size') || 4),
        is_published: form.get('is_published') === 'on',
      },
    });
    target = `/organizer/${event.slug}`;
  } catch (error) {
    target = failure('/organizer/new', error);
  }
  revalidatePath('/organizer');
  redirect(target);
}

export async function updateEventAction(form: FormData): Promise<void> {
  const slug = text(form, 'slug');
  const back = `/organizer/${slug}`;
  let target = back;
  try {
    await api<Event>(`/api/events/${slug}`, {
      method: 'PATCH',
      body: {
        name: text(form, 'name'),
        tagline: optional(form, 'tagline'),
        description: optional(form, 'description'),
        starts_at: toIso(form, 'starts_at'),
        ends_at: toIso(form, 'ends_at'),
        registration_opens_at: toIso(form, 'registration_opens_at'),
        submission_opens_at: toIso(form, 'submission_opens_at'),
        submission_deadline: toIso(form, 'submission_deadline'),
        max_team_size: Number(text(form, 'max_team_size') || 4),
        is_published: form.get('is_published') === 'on',
        // Every field below has always been in `EventOut` -- every voter-facing
        // route reads them -- but until this pass nothing ever wrote them, so a
        // real organizer had no way to turn on quadratic voting, open-link or
        // email-gated access, or pairwise judging at all. Found by checking the
        // read side against the write side rather than assuming one implied
        // the other.
        voting_opens_at: optionalIso(form, 'voting_opens_at'),
        voting_closes_at: optionalIso(form, 'voting_closes_at'),
        voting_access: text(form, 'voting_access') || undefined,
        voting_method: text(form, 'voting_method') || undefined,
        vote_credits: Number(text(form, 'vote_credits') || 100),
        votes_per_voter: Number(text(form, 'votes_per_voter') || 3),
        comments_enabled: form.get('comments_enabled') === 'on',
        pairwise_enabled: form.get('pairwise_enabled') === 'on',
        winner_slots: Number(text(form, 'winner_slots') || 0),
        community_vote_slots: Number(text(form, 'community_vote_slots') || 0),
      },
    });
  } catch (error) {
    target = failure(back, error);
  }
  revalidatePath(back);
  redirect(target);
}

export async function createTrackAction(form: FormData): Promise<void> {
  const slug = text(form, 'event_slug');
  const back = `/organizer/${slug}`;
  let target = back;
  try {
    await api(`/api/events/${slug}/tracks`, {
      method: 'POST',
      body: {
        key: text(form, 'key'),
        name: text(form, 'name'),
        description: optional(form, 'description'),
        position: Number(text(form, 'position') || 0),
      },
    });
  } catch (error) {
    target = failure(back, error);
  }
  revalidatePath(back);
  redirect(target);
}

export async function deleteTrackAction(form: FormData): Promise<void> {
  const slug = text(form, 'event_slug');
  const back = `/organizer/${slug}`;
  let target = back;
  try {
    await api(`/api/events/${slug}/tracks/${text(form, 'track_id')}`, { method: 'DELETE' });
  } catch (error) {
    target = failure(back, error);
  }
  revalidatePath(back);
  redirect(target);
}

export async function createPrizeAction(form: FormData): Promise<void> {
  const slug = text(form, 'event_slug');
  const back = `/organizer/${slug}`;
  let target = back;
  try {
    await api(`/api/events/${slug}/prizes`, {
      method: 'POST',
      body: {
        title: text(form, 'title'),
        description: optional(form, 'description'),
        value: optional(form, 'value'),
        track_id: optional(form, 'track_id'),
        position: Number(text(form, 'position') || 0),
      },
    });
  } catch (error) {
    target = failure(back, error);
  }
  revalidatePath(back);
  redirect(target);
}

export async function updatePrizeAction(form: FormData): Promise<void> {
  const slug = text(form, 'event_slug');
  const back = `/organizer/${slug}`;
  let target = back;
  try {
    // `PrizeIn` is the same shape for create and update -- the PATCH route
    // takes a full replacement, not a partial patch, so every field the form
    // shows has to be resent even if only one of them actually changed.
    await api(`/api/events/${slug}/prizes/${text(form, 'prize_id')}`, {
      method: 'PATCH',
      body: {
        title: text(form, 'title'),
        description: optional(form, 'description'),
        value: optional(form, 'value'),
        track_id: optional(form, 'track_id'),
        position: Number(text(form, 'position') || 0),
      },
    });
  } catch (error) {
    target = failure(back, error);
  }
  revalidatePath(back);
  redirect(target);
}

export async function deletePrizeAction(form: FormData): Promise<void> {
  const slug = text(form, 'event_slug');
  const back = `/organizer/${slug}`;
  let target = back;
  try {
    await api(`/api/events/${slug}/prizes/${text(form, 'prize_id')}`, { method: 'DELETE' });
  } catch (error) {
    target = failure(back, error);
  }
  revalidatePath(back);
  redirect(target);
}

export async function createQuestionAction(form: FormData): Promise<void> {
  const slug = text(form, 'event_slug');
  const back = `/organizer/${slug}`;
  let target = back;
  try {
    await api(`/api/events/${slug}/questions`, {
      method: 'POST',
      body: {
        prompt: text(form, 'prompt'),
        help_text: optional(form, 'help_text'),
        kind: text(form, 'kind') || 'textarea',
        required: form.get('required') === 'on',
        position: Number(text(form, 'position') || 0),
      },
    });
  } catch (error) {
    target = failure(back, error);
  }
  revalidatePath(back);
  redirect(target);
}

export async function deleteQuestionAction(form: FormData): Promise<void> {
  const slug = text(form, 'event_slug');
  const back = `/organizer/${slug}`;
  let target = back;
  try {
    await api(`/api/events/${slug}/questions/${text(form, 'question_id')}`, { method: 'DELETE' });
  } catch (error) {
    target = failure(back, error);
  }
  revalidatePath(back);
  redirect(target);
}


// --------------------------------------------------------------------------- //
// Judging (Phase 2)
// --------------------------------------------------------------------------- //

/**
 * Submit a ballot.
 *
 * The form posts one field per criterion, named `score:<criterion_id>`, which is
 * how a plain HTML form carries a variable-length rubric without JavaScript.
 * Note what is *not* in the form: a judge id. There is no way to express
 * "score on behalf of" here, because there is no way to express it in the API.
 */
export async function saveBallotAction(form: FormData): Promise<void> {
  const id = text(form, 'assignment_id');
  const back = `/judging/${id}`;

  const scores: { criterion_id: string; value: number }[] = [];
  for (const [key, raw] of form.entries()) {
    if (!key.startsWith('score:')) continue;
    const value = String(raw).trim();
    if (value === '') continue;
    scores.push({ criterion_id: key.slice('score:'.length), value: Number(value) });
  }

  if (scores.length === 0) {
    redirect(failure(back, new Error('Score at least one criterion')));
  }

  try {
    await api<Assignment>(`/api/judging/assignments/${id}/scores`, {
      method: 'PUT',
      body: {
        scores,
        comment: optional(form, 'comment'),
        complete: text(form, 'complete') === 'yes',
      },
    });
  } catch (error) {
    redirect(failure(back, error));
  }
  revalidatePath('/judging');
  revalidatePath(back);
  redirect(`${back}?saved=1`);
}

/**
 * A judge's answer to "which is better". `winner` is one of the two submission
 * ids, or the literal string `"tie"` for "I can't decide" -- a real button on
 * the page, not an edge case nobody can reach, since `app/pairwise.py` treats
 * a tie as real information (half a win each), not a discard.
 *
 * Redirects back to the same compare page rather than to a fixed "next"
 * destination, so a fresh `GET` naturally fetches whatever pair comes next --
 * there is nothing to compute here, the route already did it.
 */
export async function comparePairwiseAction(form: FormData): Promise<void> {
  const slug = text(form, 'event_slug');
  const back = `/events/${slug}/pairwise`;
  const a = text(form, 'submission_a_id');
  const b = text(form, 'submission_b_id');
  const winner = text(form, 'winner');

  try {
    await api<PairwiseComparison>(`/api/events/${slug}/pairwise/compare`, {
      method: 'POST',
      body: {
        submission_a_id: a,
        submission_b_id: b,
        winner_id: winner === 'tie' ? null : winner,
      },
    });
  } catch (error) {
    redirect(failure(back, error));
  }
  revalidatePath(back);
  redirect(`${back}?saved=1`);
}

export async function createCriterionAction(form: FormData): Promise<void> {
  const slug = text(form, 'slug');
  const back = `/organizer/${slug}/judging`;
  try {
    await api(`/api/events/${slug}/criteria`, {
      method: 'POST',
      body: {
        key: text(form, 'key'),
        name: text(form, 'name'),
        description: optional(form, 'description'),
        weight: Number(text(form, 'weight') || '1'),
        min_score: Number(text(form, 'min_score') || '1'),
        max_score: Number(text(form, 'max_score') || '5'),
        position: Number(text(form, 'position') || '0'),
      },
    });
  } catch (error) {
    redirect(failure(back, error));
  }
  revalidatePath(back);
  redirect(back);
}

export async function deleteCriterionAction(form: FormData): Promise<void> {
  const slug = text(form, 'slug');
  const back = `/organizer/${slug}/judging`;
  try {
    await api(`/api/events/${slug}/criteria/${text(form, 'criterion_id')}`, { method: 'DELETE' });
  } catch (error) {
    redirect(failure(back, error));
  }
  revalidatePath(back);
  redirect(back);
}

export async function inviteJudgeAction(form: FormData): Promise<void> {
  const slug = text(form, 'slug');
  const back = `/organizer/${slug}/judging`;
  const trackId = optional(form, 'track_id');
  try {
    await api(`/api/events/${slug}/judges`, {
      method: 'POST',
      body: { email: text(form, 'email'), track_id: trackId },
    });
  } catch (error) {
    redirect(failure(back, error));
  }
  revalidatePath(back);
  redirect(back);
}

export async function setJudgeActiveAction(form: FormData): Promise<void> {
  const slug = text(form, 'slug');
  const back = `/organizer/${slug}/judging`;
  try {
    await api(`/api/events/${slug}/judges/${text(form, 'judge_id')}`, {
      method: 'PATCH',
      body: { is_active: text(form, 'is_active') === 'yes' },
    });
  } catch (error) {
    redirect(failure(back, error));
  }
  revalidatePath(back);
  redirect(back);
}

/**
 * Run batch assignment.
 *
 * `dry_run` is offered in the UI because an organizer should be able to see what
 * a seed does before committing it, and because the shortfall report is the most
 * useful thing on the page when an event is under-judged.
 */
export async function runAssignmentAction(form: FormData): Promise<void> {
  const slug = text(form, 'slug');
  const back = `/organizer/${slug}/judging`;
  const seed = text(form, 'seed');
  const dryRun = text(form, 'dry_run') === 'yes';

  let result: AssignResult;
  try {
    result = await api<AssignResult>(`/api/events/${slug}/assignments`, {
      method: 'POST',
      body: {
        reviews_per_submission: Number(text(form, 'reviews_per_submission') || '3'),
        seed: seed === '' ? null : Number(seed),
        dry_run: dryRun,
      },
    });
  } catch (error) {
    redirect(failure(back, error));
  }

  // Outside the try: `redirect` signals by throwing, and catching it here would
  // swallow the navigation.
  revalidatePath(back);
  const summary = new URLSearchParams({
    assigned: String(result.created),
    spread: String(result.spread),
    short: String(result.shortfalls.length),
    ...(dryRun ? { dry: '1' } : {}),
  });
  redirect(`${back}?${summary.toString()}`);
}


// --------------------------------------------------------------------------- //
// Public voting, comments and moderation (Phase 3)
// --------------------------------------------------------------------------- //

/**
 * Claim a ballot.
 *
 * For open-link and email-gated events the API returns a token and sets its own
 * HttpOnly cookie on the response. That cookie is set on the *API* origin, which
 * the browser never talks to directly here -- so the token is copied into a cookie
 * on this origin instead, and `lib/api` forwards it on later calls. Same reasoning
 * as the session cookie: the browser holds an opaque value it cannot read.
 */
export async function claimBallotAction(form: FormData): Promise<void> {
  const slug = text(form, 'slug');
  const back = `/events/${slug}/vote`;
  const email = optional(form, 'email');

  let voter: Voter;
  try {
    voter = await api<Voter>(`/api/events/${slug}/voting/claim`, {
      method: 'POST',
      body: email === null ? {} : { email },
    });
  } catch (error) {
    redirect(failure(back, error));
  }

  if (voter.token) {
    (await cookies()).set({
      name: VOTER_COOKIE,
      value: voter.token,
      httpOnly: true,
      sameSite: 'lax',
      path: '/',
      maxAge: 60 * 60 * 24 * 30,
    });
  }
  revalidatePath(back);
  redirect(back);
}

/**
 * Cast or revise a ballot.
 *
 * The form posts one field per project, named `vote:<submission_id>`, so a ballot
 * of any length survives without JavaScript. Blank and zero mean "not voting for
 * this", which is how a voter removes a project: there is no separate delete.
 */
export async function castVotesAction(form: FormData): Promise<void> {
  const slug = text(form, 'slug');
  const back = `/events/${slug}/vote`;

  const votes: { submission_id: string; credits: number }[] = [];
  for (const [key, raw] of form.entries()) {
    if (!key.startsWith('vote:')) continue;
    const credits = Number(String(raw).trim());
    if (!Number.isFinite(credits) || credits < 1) continue;
    votes.push({ submission_id: key.slice('vote:'.length), credits });
  }

  try {
    await api<Ballot>(`/api/events/${slug}/votes`, { method: 'PUT', body: { votes } });
  } catch (error) {
    redirect(failure(back, error));
  }
  revalidatePath(back);
  redirect(`${back}?saved=1`);
}

export async function withdrawVotesAction(form: FormData): Promise<void> {
  const slug = text(form, 'slug');
  const back = `/events/${slug}/vote`;
  try {
    await api<Ballot>(`/api/events/${slug}/votes`, { method: 'DELETE' });
  } catch (error) {
    redirect(failure(back, error));
  }
  revalidatePath(back);
  redirect(`${back}?withdrawn=1`);
}

/** Organizers only: open or re-close the community tally. Both ways are audited. */
export async function publishTallyAction(form: FormData): Promise<void> {
  const slug = text(form, 'slug');
  const back = `/organizer/${slug}/judging`;
  const making = text(form, 'public') === 'yes';
  try {
    await api(`/api/events/${slug}/voting/publish?public=${making}`, { method: 'POST' });
  } catch (error) {
    redirect(failure(back, error));
  }
  revalidatePath(back);
  revalidatePath(`/events/${slug}/results`);
  redirect(back);
}

export async function postCommentAction(form: FormData): Promise<void> {
  const submissionId = text(form, 'submission_id');
  const back = `/projects/${submissionId}`;
  try {
    await api(`/api/gallery/${submissionId}/comments`, {
      method: 'POST',
      body: { body: text(form, 'body') },
    });
  } catch (error) {
    redirect(failure(back, error));
  }
  revalidatePath(back);
  redirect(`${back}#comments`);
}

export async function removeCommentAction(form: FormData): Promise<void> {
  const submissionId = text(form, 'submission_id');
  const back = `/projects/${submissionId}`;
  try {
    await api(`/api/comments/${text(form, 'comment_id')}`, { method: 'DELETE' });
  } catch (error) {
    redirect(failure(back, error));
  }
  revalidatePath(back);
  redirect(`${back}#comments`);
}

/** Staff only. A reason is required, and it goes in the audit log verbatim. */
export async function hideCommentAction(form: FormData): Promise<void> {
  const submissionId = text(form, 'submission_id');
  const back = `/projects/${submissionId}`;
  try {
    await api(`/api/comments/${text(form, 'comment_id')}/hide`, {
      method: 'POST',
      body: { reason: text(form, 'reason') },
    });
  } catch (error) {
    redirect(failure(back, error));
  }
  revalidatePath(back);
  redirect(`${back}#comments`);
}


// --------------------------------------------------------------------------- //
// Webhooks, certificates and import (Phase 4)
// --------------------------------------------------------------------------- //

/**
 * Register a webhook.
 *
 * The secret comes back exactly once, so it is carried to the page in the redirect
 * and shown there. Putting it in a query string is a real trade-off — it lands in
 * the browser's history — and it is the lesser of the two evils against never
 * showing it, because without it a receiver cannot verify anything. An organizer can
 * delete and re-register to get a fresh one.
 */
export async function createWebhookAction(form: FormData): Promise<void> {
  const slug = text(form, 'slug');
  const back = `/organizer/${slug}/integrations`;
  const topics = form
    .getAll('topics')
    .map((t) => String(t))
    .filter(Boolean);

  let created: { secret: string };
  try {
    created = await api<{ secret: string }>(`/api/events/${slug}/webhooks`, {
      method: 'POST',
      body: {
        url: text(form, 'url'),
        topics,
        description: optional(form, 'description'),
      },
    });
  } catch (error) {
    redirect(failure(back, error));
  }
  revalidatePath(back);
  redirect(`${back}?secret=${encodeURIComponent(created.secret)}`);
}

export async function deleteWebhookAction(form: FormData): Promise<void> {
  const slug = text(form, 'slug');
  const back = `/organizer/${slug}/integrations`;
  try {
    await api(`/api/events/${slug}/webhooks/${text(form, 'hook_id')}`, { method: 'DELETE' });
  } catch (error) {
    redirect(failure(back, error));
  }
  revalidatePath(back);
  redirect(back);
}

export async function toggleWebhookAction(form: FormData): Promise<void> {
  const slug = text(form, 'slug');
  const back = `/organizer/${slug}/integrations`;
  try {
    await api(`/api/events/${slug}/webhooks/${text(form, 'hook_id')}`, {
      method: 'PATCH',
      body: { is_active: text(form, 'is_active') === 'yes' },
    });
  } catch (error) {
    redirect(failure(back, error));
  }
  revalidatePath(back);
  redirect(back);
}

/** Send a ping and report what the endpoint said. */
export async function testWebhookAction(form: FormData): Promise<void> {
  const slug = text(form, 'slug');
  const back = `/organizer/${slug}/integrations`;

  let result: { status: string; response_code: number | null; error: string | null };
  try {
    result = await api(`/api/events/${slug}/webhooks/${text(form, 'hook_id')}/test`, {
      method: 'POST',
    });
  } catch (error) {
    redirect(failure(back, error));
  }
  revalidatePath(back);
  const summary = result.error
    ? `${result.status}: ${result.error}`
    : `${result.status}${result.response_code ? ` (HTTP ${result.response_code})` : ''}`;
  redirect(`${back}?ping=${encodeURIComponent(summary)}`);
}

export async function assignThirdReviewAction(form: FormData): Promise<void> {
  const slug = text(form, 'slug');
  const back = `/organizer/${slug}/results`;

  let result: { judge_name: string };
  try {
    result = await api(
      `/api/events/${slug}/results/disagreement/${text(form, 'submission_id')}/third-review`,
      { method: 'POST' },
    );
  } catch (error) {
    redirect(failure(back, error));
  }
  revalidatePath(back);
  redirect(`${back}?third_review=${encodeURIComponent(`Routed to ${result.judge_name}`)}`);
}

/** Staff only, for cause. Never touches a ballot or the judge-computed
 * ranking -- see `results.set_override`'s own docstring. */
export async function setResultOverrideAction(form: FormData): Promise<void> {
  const slug = text(form, 'slug');
  const back = `/organizer/${slug}/results`;
  try {
    await api(`/api/events/${slug}/results/overrides/${text(form, 'submission_id')}`, {
      method: 'PUT',
      body: { tier: text(form, 'tier'), reason: text(form, 'reason') },
    });
  } catch (error) {
    redirect(failure(back, error));
  }
  revalidatePath(back);
  redirect(back);
}

/** Undoes an override -- also staff only, also reasoned. */
export async function clearResultOverrideAction(form: FormData): Promise<void> {
  const slug = text(form, 'slug');
  const back = `/organizer/${slug}/results`;
  try {
    await api(
      `/api/events/${slug}/results/overrides/${text(form, 'submission_id')}/clear`,
      { method: 'POST', body: { reason: text(form, 'reason') } },
    );
  } catch (error) {
    redirect(failure(back, error));
  }
  revalidatePath(back);
  redirect(back);
}

export async function issueCertificatesAction(form: FormData): Promise<void> {
  const slug = text(form, 'slug');
  const back = `/organizer/${slug}/integrations`;

  let result: { issued: number; updated: number };
  try {
    result = await api(`/api/events/${slug}/certificates/issue`, { method: 'POST' });
  } catch (error) {
    redirect(failure(back, error));
  }
  revalidatePath(back);
  redirect(`${back}?issued=${result.issued}&updated=${result.updated}`);
}

/** Judging and organizing records: one specific account, on demand -- the
 * admin-issued counterpart to `issueCertificatesAction`'s bulk pass, since
 * neither role gets a self-service certificate of its own. */
export async function issueCertificateForUserAction(form: FormData): Promise<void> {
  const slug = text(form, 'slug');
  const back = `/organizer/${slug}/integrations`;
  let cert: Certificate;
  try {
    cert = await api<Certificate>(`/api/events/${slug}/certificates/issue-for-user`, {
      method: 'POST',
      body: { user_id: text(form, 'user_id'), kind: text(form, 'kind') },
    });
  } catch (error) {
    redirect(failure(back, error));
  }
  revalidatePath(back);
  // Issuing is idempotent -- an existing record keeps its code -- so the page
  // needs to say which happened, not claim a new record either way. The API does
  // not say, but the record does: every issue re-signs the payload with a fresh
  // `issued_at`, while the record's own `issued_at` stays at first creation. The
  // two agree (to within one request: ~50 ms measured) only for a record this
  // call just made; on a re-issue they differ by however long ago it was first
  // issued. The tolerance is seconds, not minutes, so a repeat click after the page
  // reloads reads correctly -- but it is still an inference from two clocks (the
  // API's and the database's), so a repeat within the tolerance of the first issue
  // reads "new" (the code shown is the same either way).
  let fresh = false;
  try {
    const signedAt = Date.parse(JSON.parse(cert.payload).issued_at);
    fresh = Math.abs(signedAt - Date.parse(cert.issued_at)) < 3_000;
  } catch {
    // Unparseable payload: say "already has" (the conservative claim) rather than
    // announce a new record we cannot show was made.
  }
  const q = new URLSearchParams({
    cert: cert.code,
    cert_name: cert.subject_name,
    cert_kind: cert.kind,
    cert_fresh: fresh ? '1' : '0',
    cert_at: cert.issued_at,
  });
  redirect(`${back}?${q}`);
}

/**
 * Archive or unarchive an event. Archiving freezes it (every write to it or
 * anything it owns is refused by the API until it is unarchived) and takes it out
 * of the default lists; nothing is deleted.
 */
export async function archiveEventAction(form: FormData): Promise<void> {
  const slug = text(form, 'slug');
  const back = `/organizer/${slug}`;
  const verb = text(form, 'verb') === 'unarchive' ? 'unarchive' : 'archive';
  try {
    await api(`/api/events/${slug}/${verb}`, { method: 'POST' });
  } catch (error) {
    redirect(failure(back, error));
  }
  revalidatePath('/', 'layout');
  redirect(back);
}

export async function revokeCertificateAction(form: FormData): Promise<void> {
  const slug = text(form, 'slug');
  const back = `/organizer/${slug}/integrations`;
  try {
    await api(`/api/events/${slug}/certificates/${text(form, 'certificate_id')}/revoke`, {
      method: 'POST',
      body: { reason: text(form, 'reason') },
    });
  } catch (error) {
    redirect(failure(back, error));
  }
  revalidatePath(back);
  redirect(back);
}

/**
 * Bulk import a CSV.
 *
 * The file is read here and posted as `text/csv`, so the API takes a raw body rather
 * than a multipart wrapper — which is also what makes `curl --data-binary @file.csv`
 * work against the same endpoint.
 */
export async function importCsvAction(form: FormData): Promise<void> {
  const slug = text(form, 'slug');
  const entity = text(form, 'entity');
  // Two submit buttons share this one form (`name="intent"`); "preview" asks
  // the API for a dry run -- parsed and validated, nothing written -- so an
  // organizer can read the outcome before committing to it.
  const dryRun = text(form, 'intent') === 'preview';
  const back = `/organizer/${slug}/integrations`;

  const upload = form.get('file');
  if (!(upload instanceof File) || upload.size === 0) {
    redirect(failure(back, new Error('Choose a CSV file to import')));
  }
  const body = await (upload as File).text();

  let result: { created: number; updated: number; skipped: number; errors: string[]; dry_run: boolean };
  try {
    const path = `/api/events/${slug}/import/${entity}${dryRun ? '?dry_run=true' : ''}`;
    result = await apiRaw<typeof result>(path, body);
  } catch (error) {
    redirect(failure(back, error));
  }
  revalidatePath(back);
  const summary = new URLSearchParams({
    imported:
      `${dryRun ? 'Preview (nothing written) -- ' : ''}` +
      `${result.created} created, ${result.updated} updated`,
    ...(result.errors.length ? { import_errors: result.errors.slice(0, 3).join(' | ') } : {}),
  });
  redirect(`${back}?${summary.toString()}`);
}

/**
 * Judge calibration. A judge scores a practice project against the event's
 * rubric; the organizer's expected scores are never in this form or in the
 * response to it, so there is nothing here that could leak one.
 */
export async function saveCalibrationAction(form: FormData): Promise<void> {
  const id = text(form, 'project_id');
  const back = '/judging/calibration';

  const scores: { criterion_id: string; value: number }[] = [];
  for (const [key, raw] of form.entries()) {
    if (!key.startsWith('score:')) continue;
    const value = String(raw).trim();
    if (value === '') continue;
    scores.push({ criterion_id: key.slice('score:'.length), value: Number(value) });
  }
  if (scores.length === 0) {
    redirect(failure(back, new Error('Score at least one criterion')));
  }

  try {
    await api(`/api/judging/calibration/${id}/scores`, { method: 'PUT', body: { scores } });
  } catch (error) {
    redirect(failure(back, error));
  }
  revalidatePath(back);
  redirect(`${back}?saved=1`);
}

export async function addCalibrationProjectAction(form: FormData): Promise<void> {
  const slug = text(form, 'slug');
  const back = `/organizer/${slug}/judging/calibration`;

  const expected: { criterion_id: string; value: number }[] = [];
  for (const [key, raw] of form.entries()) {
    if (!key.startsWith('expected:')) continue;
    const value = String(raw).trim();
    if (value === '') continue;
    expected.push({ criterion_id: key.slice('expected:'.length), value: Number(value) });
  }

  try {
    await api(`/api/events/${slug}/calibration/projects`, {
      method: 'POST',
      body: { name: text(form, 'name'), description: optional(form, 'description'), expected },
    });
  } catch (error) {
    redirect(failure(back, error));
  }
  revalidatePath(back);
  redirect(back);
}

export async function removeCalibrationProjectAction(form: FormData): Promise<void> {
  const slug = text(form, 'slug');
  const back = `/organizer/${slug}/judging/calibration`;
  try {
    await api(`/api/events/${slug}/calibration/projects/${text(form, 'project_id')}`, {
      method: 'DELETE',
    });
  } catch (error) {
    redirect(failure(back, error));
  }
  revalidatePath(back);
  redirect(back);
}
