import Link from 'next/link';
import { notFound } from 'next/navigation';

import {
  saveSubmissionAction,
  submitSubmissionAction,
  unsubmitSubmissionAction,
} from '../../../actions';
import { FormError, Notice } from '../../../components';
import { apiOrNull } from '@/lib/api';
import { formatDateTime, untilDeadline } from '@/lib/format';
import type { Event, Submission } from '@/lib/types';

export const dynamic = 'force-dynamic';

export const metadata = { title: 'Edit submission -- Dogfood' };

/**
 * Draft and edit, until the deadline.
 *
 * The form is disabled once `can_edit` is false, and `can_edit` is computed by
 * the API with the same predicate that would refuse the PATCH. Re-enabling the
 * inputs in devtools gets a 409 with the reason.
 */
export default async function EditSubmissionPage({
  params,
  searchParams,
}: {
  params: Promise<{ id: string }>;
  searchParams: Promise<{ error?: string; saved?: string; missing?: string }>;
}) {
  const { id } = await params;
  const { error, saved, missing } = await searchParams;

  const submission = await apiOrNull<Submission>(`/api/submissions/${id}`);
  if (!submission) notFound();

  const event = await apiOrNull<Event>(`/api/events/${submission.event_slug}`);
  const locked = !submission.can_edit;
  const blockers = missing ? missing.split('|') : [];

  return (
    <main className="shell narrow">
      <p className="eyebrow">
        <Link href={`/projects/${submission.id}`}>{submission.name}</Link> · {submission.team_name}
      </p>
      <div className="spread">
        <h1>Edit project</h1>
        <span className={`pill ${submission.status === 'submitted' ? 'ok' : 'warn'}`}>
          {submission.status}
        </span>
      </div>

      <FormError message={error} />
      {saved && <Notice kind="good">Saved.</Notice>}
      {blockers.length > 0 && (
        <Notice kind="bad">
          <strong>Not ready to submit.</strong>
          <ul style={{ margin: '8px 0 0', paddingLeft: 18 }}>
            {blockers.map((blocker) => (
              <li key={blocker}>{blocker}</li>
            ))}
          </ul>
        </Notice>
      )}

      {event && (
        <Notice kind={locked ? 'bad' : 'info'}>
          <span className="small">
            Deadline <strong>{formatDateTime(event.submission_deadline)}</strong> --{' '}
            {untilDeadline(event.submission_deadline)}.
            {locked && ' Editing is closed. Your draft is kept, and stays out of the gallery.'}
          </span>
        </Notice>
      )}

      <form action={saveSubmissionAction} className="panel stack" style={{ marginTop: 18 }}>
        <input type="hidden" name="submission_id" value={submission.id} />
        <fieldset
          disabled={locked}
          style={{ border: 0, padding: 0, margin: 0, minInlineSize: 'auto' }}
        >
          <div className="field">
            <label htmlFor="name">Name</label>
            <input id="name" name="name" defaultValue={submission.name} required maxLength={160} />
          </div>
          <div className="field">
            <label htmlFor="tagline">Tagline</label>
            <input
              id="tagline"
              name="tagline"
              defaultValue={submission.tagline ?? ''}
              maxLength={240}
              placeholder="One line. It is what the gallery shows."
            />
          </div>
          <div className="field">
            <label htmlFor="description">Description</label>
            <textarea
              id="description"
              name="description"
              defaultValue={submission.description ?? ''}
              placeholder="What it does, what you built, what you would do next."
            />
          </div>

          {event && event.tracks.length > 0 && (
            <div className="field">
              <label htmlFor="track_id">Track</label>
              <select id="track_id" name="track_id" defaultValue={submission.track?.id ?? ''}>
                <option value="">Choose a track</option>
                {event.tracks.map((track) => (
                  <option key={track.id} value={track.id}>
                    {track.name}
                  </option>
                ))}
              </select>
            </div>
          )}

          <div className="field">
            <label htmlFor="discord_usernames">Discord username(s)</label>
            <input
              id="discord_usernames"
              name="discord_usernames"
              defaultValue={submission.discord_usernames.join(', ')}
              placeholder="Solo: yourname -- team: yourname, teammate, teammate2"
            />
            <p className="muted small" style={{ margin: '2px 0 0' }}>
              One per person submitting. Comma separated if you are a team; just
              your own if you are solo. Required to submit.
            </p>
          </div>

          <div className="field field-row">
            <div>
              <label htmlFor="repo_url">GitHub repository URL</label>
              <input
                id="repo_url"
                name="repo_url"
                type="url"
                defaultValue={submission.repo_url ?? ''}
                placeholder="https://github.com/owner/repo"
              />
            </div>
            <div>
              <label htmlFor="live_url">Live demo URL (optional)</label>
              <input id="live_url" name="live_url" type="url" defaultValue={submission.live_url ?? ''} />
            </div>
          </div>

          <div className="field field-row">
            <div>
              <label htmlFor="demo_video_url">Demo video URL (optional)</label>
              <input
                id="demo_video_url"
                name="demo_video_url"
                type="url"
                defaultValue={submission.demo_video_url ?? ''}
              />
            </div>
            <div>
              <label htmlFor="linkedin_url">LinkedIn URL (optional)</label>
              <input
                id="linkedin_url"
                name="linkedin_url"
                type="url"
                defaultValue={submission.linkedin_url ?? ''}
              />
            </div>
          </div>

          <div className="field">
            <label htmlFor="thumbnail_url">Thumbnail URL</label>
            <input
              id="thumbnail_url"
              name="thumbnail_url"
              defaultValue={submission.thumbnail_url ?? ''}
            />
          </div>

          <div className="field">
            <label htmlFor="gallery_image_urls">Image gallery</label>
            <textarea
              id="gallery_image_urls"
              name="gallery_image_urls"
              style={{ minHeight: 80 }}
              defaultValue={submission.gallery_image_urls.join('\n')}
              placeholder="One URL per line, up to 8."
            />
          </div>

          <div className="field">
            <label htmlFor="tech_tags">Tech tags</label>
            <input
              id="tech_tags"
              name="tech_tags"
              defaultValue={submission.tech_tags.join(', ')}
              placeholder="Comma separated: python, postgres, svelte"
            />
          </div>

          {submission.answers.length > 0 && (
            <>
              <hr />
              <h2>Organizer questions</h2>
              {submission.answers.map((answer) => (
                <div className="field" key={answer.question_id}>
                  <label htmlFor={`answer-${answer.question_id}`}>{answer.prompt}</label>
                  <textarea
                    id={`answer-${answer.question_id}`}
                    name={`answer:${answer.question_id}`}
                    style={{ minHeight: 90 }}
                    defaultValue={answer.value ?? ''}
                  />
                </div>
              ))}
            </>
          )}

          <div className="row" style={{ marginTop: 18 }}>
            <button className="primary" type="submit">
              Save draft
            </button>
            <Link className="button" href={`/projects/${submission.id}`}>
              View
            </Link>
          </div>
        </fieldset>
      </form>

      <section className="panel" style={{ marginTop: 16 }}>
        <h2>Entry</h2>
        {submission.status === 'draft' ? (
          <>
            <p className="muted small">
              Submitting puts this project in the public gallery. You can still edit it, and you
              can take it back out, until the deadline.
            </p>
            <form action={submitSubmissionAction}>
              <input type="hidden" name="submission_id" value={submission.id} />
              <button className="primary" type="submit" disabled={!submission.can_submit}>
                Submit project
              </button>
            </form>
          </>
        ) : (
          <>
            <p className="muted small">
              Entered{submission.submitted_at ? ` ${formatDateTime(submission.submitted_at)}` : ''}.
              It is in the gallery now.
            </p>
            <form action={unsubmitSubmissionAction}>
              <input type="hidden" name="submission_id" value={submission.id} />
              <button type="submit" disabled={locked}>
                Withdraw to draft
              </button>
            </form>
          </>
        )}
      </section>
    </main>
  );
}
