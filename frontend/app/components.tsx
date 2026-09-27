/**
 * The handful of pieces that appear on more than one page.
 *
 * Server components without exception. Nothing here decides what a person may
 * do -- `can_edit` and `can_submit` arrive from the API, computed by the same
 * predicate that would refuse the request, and are used only to avoid showing
 * somebody a button that would fail.
 */

import Link from 'next/link';

import { ProjectCover } from '@/lib/cover';
import type { SubmissionCard, Track } from '@/lib/types';

/**
 * `src` is a real upload; everything else is what a generated cover is drawn
 * from when there isn't one -- see `lib/cover.tsx` for what each field
 * actually controls. Optional and defaulted for the few callers that only
 * ever show an uploaded image and would otherwise have to pass placeholders.
 */
export function Thumbnail({
  src,
  id,
  name,
  track = null,
  techTags = [],
  submittedAt = null,
}: {
  src: string | null;
  id: string;
  name: string;
  track?: Track | null;
  techTags?: string[];
  submittedAt?: string | null;
}) {
  if (src) {
    // Arbitrary remote and data: URLs, and no image optimizer to route them
    // through offline.
    // eslint-disable-next-line @next/next/no-img-element
    return <img className="thumb" src={src} alt="" />;
  }
  return (
    <ProjectCover
      className="thumb cover"
      id={id}
      name={name}
      track={track}
      techTags={techTags}
      submittedAt={submittedAt}
    />
  );
}

export function Tags({ tags, eventSlug }: { tags: string[]; eventSlug?: string }) {
  if (tags.length === 0) return null;
  return (
    <div className="row" style={{ gap: 6, marginTop: 10 }}>
      {tags.map((tag) =>
        eventSlug ? (
          <Link key={tag} className="tag" href={`/events/${eventSlug}/gallery?tag=${encodeURIComponent(tag)}`}>
            {tag}
          </Link>
        ) : (
          <span key={tag} className="tag">
            {tag}
          </span>
        ),
      )}
    </div>
  );
}

export function TrackBadge({ track }: { track: Track | null }) {
  if (!track) return null;
  return <span className="pill">{track.name}</span>;
}

export function ProjectCard({ project, eventSlug }: { project: SubmissionCard; eventSlug?: string }) {
  return (
    <Link className="card" href={`/projects/${project.id}`}>
      <Thumbnail
        src={project.thumbnail_url}
        id={project.id}
        name={project.name}
        track={project.track}
        techTags={project.tech_tags}
        submittedAt={project.submitted_at}
      />
      <div className="spread" style={{ alignItems: 'center' }}>
        <h3 style={{ margin: 0 }}>{project.name}</h3>
        <TrackBadge track={project.track} />
      </div>
      <p className="muted small" style={{ margin: '6px 0 0' }}>
        {project.tagline ?? 'No tagline yet.'}
      </p>
      <p className="muted small" style={{ margin: '8px 0 0' }}>
        by {project.team_name}
      </p>
      <Tags tags={project.tech_tags} eventSlug={eventSlug} />
    </Link>
  );
}

export function Empty({ children }: { children: React.ReactNode }) {
  return (
    <div className="panel muted" style={{ textAlign: 'center', padding: '40px 18px' }}>
      {children}
    </div>
  );
}

export function Notice({
  kind = 'info',
  children,
}: {
  kind?: 'info' | 'good' | 'bad';
  children: React.ReactNode;
}) {
  return <div className={`notice ${kind === 'info' ? '' : kind}`}>{children}</div>;
}

/**
 * Renders an error thrown by a server action and passed back through the URL.
 * Server actions in this app redirect on success and re-render with `?error=`
 * on failure, which keeps every form a plain HTML form.
 */
export function FormError({ message }: { message?: string }) {
  if (!message) return null;
  return (
    <div className="notice bad small" style={{ marginBottom: 16 }}>
      {message}
    </div>
  );
}

/**
 * Page-number links for any `Page<T>` response (`lib/types.ts`) -- one
 * component for every paged list in the app, the same way `Empty` and
 * `Notice` are, so a page boundary looks and behaves the same everywhere it
 * appears. `basePath` + `extraParams` build the link; the current page is
 * never itself a link, matching the gallery's own pagination this mirrors.
 */
export function Pagination({
  page,
  pages,
  total,
  basePath,
  extraParams,
  noun = 'result',
  pageParam = 'page',
}: {
  page: number;
  pages: number;
  total: number;
  basePath: string;
  extraParams?: Record<string, string | undefined>;
  noun?: string;
  /** The query param name, when a page hosts more than one paginated list
   * (e.g. integrations' certificate table uses `cert_page`) and `page` alone
   * would be ambiguous. */
  pageParam?: string;
}) {
  if (pages <= 1) return null;

  const href = (n: number) => {
    const params = new URLSearchParams();
    for (const [k, v] of Object.entries(extraParams ?? {})) {
      if (v) params.set(k, v);
    }
    params.set(pageParam, String(n));
    return `${basePath}?${params.toString()}`;
  };

  return (
    <nav
      className="row"
      style={{ justifyContent: 'space-between', alignItems: 'center', marginTop: 16 }}
      aria-label="Pagination"
    >
      <span className="muted small">
        {total} {noun}
        {total === 1 ? '' : 's'} · page {page} of {pages}
      </span>
      <span className="row" style={{ gap: 6 }}>
        {page > 1 && (
          <Link className="button" href={href(page - 1)}>
            ← Previous
          </Link>
        )}
        {page < pages && (
          <Link className="button" href={href(page + 1)}>
            Next →
          </Link>
        )}
      </span>
    </nav>
  );
}
