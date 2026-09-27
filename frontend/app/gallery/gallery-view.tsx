/**
 * The public gallery.
 *
 * Search and filter are a plain GET form writing query parameters, rendered on
 * the server. No client-side JavaScript, no fetch-on-type, and every filtered
 * view has a URL somebody can send to a colleague.
 */

import Link from 'next/link';

import { Empty, ProjectCard } from '../components';
import { api } from '@/lib/api';
import type { Event, GalleryPage } from '@/lib/types';

export type GalleryQuery = {
  q?: string;
  track?: string;
  tag?: string;
  sort?: string;
  page?: string;
};

const SORTS = [
  ['mixed', 'Events mixed'],
  ['recent', 'Most recent'],
  ['name', 'Project name'],
  ['team', 'Team name'],
] as const;

function queryString(base: GalleryQuery, overrides: Partial<GalleryQuery>): string {
  const params = new URLSearchParams();
  const merged = { ...base, ...overrides };
  for (const [key, value] of Object.entries(merged)) {
    if (value) params.set(key, String(value));
  }
  const encoded = params.toString();
  return encoded ? `?${encoded}` : '';
}

export default async function GalleryView({
  event,
  query,
  basePath,
}: {
  event: Event | null;
  query: GalleryQuery;
  basePath: string;
}) {
  const params = new URLSearchParams();
  if (event) params.set('event', event.slug);
  if (query.q) params.set('q', query.q);
  if (query.track) params.set('track', query.track);
  if (query.tag) params.set('tag', query.tag);
  // Across events, "most recent" lets the newest event fill the first page, so
  // that view takes turns between events by default; one event has nothing to mix.
  const sort = query.sort ?? (event ? 'recent' : 'mixed');
  params.set('sort', sort);
  params.set('page', query.page ?? '1');

  const [page, tags] = await Promise.all([
    api<GalleryPage>(`/api/gallery?${params.toString()}`),
    api<string[]>(`/api/gallery/tags${event ? `?event=${event.slug}` : ''}`),
  ]);

  return (
    <main className="shell">
      <p className="eyebrow">{event ? event.name : 'All events'}</p>
      <div className="spread">
        <h1>Gallery</h1>
        <span className="muted small">
          {page.total} project{page.total === 1 ? '' : 's'}
        </span>
      </div>

      <form method="get" className="panel" style={{ margin: '18px 0 24px' }}>
        <div className="field-row">
          <div>
            <label htmlFor="q">Search</label>
            <input
              id="q"
              name="q"
              type="search"
              defaultValue={query.q ?? ''}
              placeholder="Name, tagline, description, team"
            />
          </div>
          {event && event.tracks.length > 0 && (
            <div>
              <label htmlFor="track">Track</label>
              <select id="track" name="track" defaultValue={query.track ?? ''}>
                <option value="">Every track</option>
                {event.tracks.map((track) => (
                  <option key={track.id} value={track.key}>
                    {track.name}
                  </option>
                ))}
              </select>
            </div>
          )}
          <div>
            <label htmlFor="sort">Sort</label>
            <select id="sort" name="sort" defaultValue={sort}>
              {SORTS.filter(([value]) => !event || value !== 'mixed').map(([value, label]) => (
                <option key={value} value={value}>
                  {label}
                </option>
              ))}
            </select>
          </div>
        </div>
        {query.tag && <input type="hidden" name="tag" value={query.tag} />}
        <div className="row" style={{ marginTop: 14 }}>
          <button className="primary" type="submit">
            Apply
          </button>
          {(query.q || query.track || query.tag) && (
            <Link className="button" href={basePath}>
              Clear
            </Link>
          )}
        </div>
      </form>

      {tags.length > 0 && (
        <div className="row" style={{ gap: 6, marginBottom: 24 }} role="group" aria-label="Filter by tech tag">
          <span className="muted small" style={{ marginRight: 4 }}>
            Tech
          </span>
          {tags.map((tag) => (
            <Link
              key={tag}
              className={`tag ${query.tag === tag ? 'on' : ''}`}
              href={`${basePath}${queryString(query, { tag: query.tag === tag ? undefined : tag, page: undefined })}`}
              aria-current={query.tag === tag ? 'true' : undefined}
            >
              {tag}
              {query.tag === tag && <span className="sr-only"> (selected, click to clear)</span>}
            </Link>
          ))}
        </div>
      )}

      {page.items.length === 0 ? (
        <Empty>
          Nothing matches that. Projects appear here once a team submits them, and not before.
        </Empty>
      ) : (
        <div className="grid">
          {page.items.map((project) => (
            <ProjectCard key={project.id} project={project} eventSlug={event?.slug} />
          ))}
        </div>
      )}

      {page.pages > 1 && (
        <nav className="row" style={{ justifyContent: 'center', marginTop: 28 }} aria-label="Gallery pages">
          {Array.from({ length: page.pages }, (_, index) => index + 1).map((number) => (
            <Link
              key={number}
              className={`tag ${number === page.page ? 'on' : ''}`}
              href={`${basePath}${queryString(query, { page: String(number) })}`}
              aria-current={number === page.page ? 'page' : undefined}
              aria-label={`Page ${number}`}
            >
              {number}
            </Link>
          ))}
        </nav>
      )}
    </main>
  );
}
