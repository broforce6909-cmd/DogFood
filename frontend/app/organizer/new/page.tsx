import { notFound } from 'next/navigation';

import { createEventAction } from '../../actions';
import { FormError } from '../../components';
import { getMe } from '@/lib/session';
import { atLeast } from '@/lib/types';

export const dynamic = 'force-dynamic';

export const metadata = { title: 'New event -- Dogfood' };

/** Four separate windows, because they are four separate decisions. */
const WINDOWS: [string, string, string][] = [
  ['registration_opens_at', 'Registration opens', 'When people can start forming teams.'],
  ['submission_opens_at', 'Submissions open', 'When drafts can be started.'],
  ['submission_deadline', 'Submission deadline', 'Enforced by the API, not by the form.'],
  ['judging_opens_at', 'Judging opens', 'Optional. Configured now, enforced in T2.'],
];

export default async function NewEventPage({
  searchParams,
}: {
  searchParams: Promise<{ error?: string }>;
}) {
  const me = await getMe();
  if (!atLeast(me.role, 'organizer')) notFound();
  const { error } = await searchParams;

  return (
    <main className="shell narrow">
      <p className="eyebrow">Organizer</p>
      <h1>New event</h1>
      <FormError message={error} />

      <form action={createEventAction} className="panel stack">
        <div className="field field-row">
          <div>
            <label htmlFor="name">Name</label>
            <input id="name" name="name" required maxLength={200} />
          </div>
          <div>
            <label htmlFor="slug">Slug</label>
            <input
              id="slug"
              name="slug"
              required
              pattern="[a-z0-9]+(-[a-z0-9]+)*"
              placeholder="dogfood"
            />
            <p className="muted small" style={{ margin: '6px 0 0' }}>
              Permanent. It is the public URL.
            </p>
          </div>
        </div>

        <div className="field">
          <label htmlFor="tagline">Tagline</label>
          <input id="tagline" name="tagline" maxLength={300} />
        </div>

        <div className="field">
          <label htmlFor="description">Description</label>
          <textarea id="description" name="description" />
        </div>

        <hr />
        <h2>Dates</h2>
        <p className="muted small">All times are UTC.</p>

        <div className="field field-row">
          <div>
            <label htmlFor="starts_at">Event starts</label>
            <input id="starts_at" name="starts_at" type="datetime-local" required />
          </div>
          <div>
            <label htmlFor="ends_at">Event ends</label>
            <input id="ends_at" name="ends_at" type="datetime-local" required />
          </div>
        </div>

        {WINDOWS.map(([name, label, help]) => (
          <div className="field" key={name}>
            <label htmlFor={name}>{label}</label>
            <input
              id={name}
              name={name}
              type="datetime-local"
              required={name !== 'judging_opens_at'}
            />
            <p className="muted small" style={{ margin: '6px 0 0' }}>
              {help}
            </p>
          </div>
        ))}

        <div className="field field-row">
          <div>
            <label htmlFor="max_team_size">Maximum team size</label>
            <input id="max_team_size" name="max_team_size" type="number" min={1} defaultValue={4} />
          </div>
          <div>
            <label htmlFor="is_published">Visibility</label>
            <label className="row small" style={{ color: 'inherit' }}>
              <input id="is_published" name="is_published" type="checkbox" style={{ width: 'auto' }} />
              Publish immediately
            </label>
            <p className="muted small" style={{ margin: '6px 0 0' }}>
              An unpublished event 404s for everybody but staff.
            </p>
          </div>
        </div>

        <button className="primary" type="submit">
          Create event
        </button>
      </form>
    </main>
  );
}
