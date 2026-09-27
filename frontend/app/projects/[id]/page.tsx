import { Fragment } from 'react';
import Link from 'next/link';
import { notFound } from 'next/navigation';

import {
  hideCommentAction,
  postCommentAction,
  removeCommentAction,
} from '../../actions';
import { Empty, Pagination, Tags, Thumbnail, TrackBadge } from '../../components';
import { apiOrNull } from '@/lib/api';
import { formatDateTime, safeHref } from '@/lib/format';
import { getMe } from '@/lib/session';
import { atLeast, type Comment, type Page, type Submission } from '@/lib/types';

export const dynamic = 'force-dynamic';

export async function generateMetadata({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  const project = await apiOrNull<Submission>(`/api/submissions/${id}`);
  return { title: project ? `${project.name} -- Dogfood` : 'Project -- Dogfood' };
}

/**
 * A project page. Public for anything in the gallery; 404 for a draft unless
 * you are on its team or you run the event. That decision is the API's -- this
 * page just renders whatever came back, or nothing.
 */
export default async function ProjectPage({
  params,
  searchParams,
}: {
  params: Promise<{ id: string }>;
  searchParams: Promise<{ error?: string; page?: string }>;
}) {
  const { id } = await params;
  const { error, page } = await searchParams;
  const project = await apiOrNull<Submission>(`/api/submissions/${id}`);
  if (!project) notFound();

  const links: [string, string | null][] = [
    ['Repository', project.repo_url],
    ['Live', project.live_url],
    ['Demo video', project.demo_video_url],
    ['LinkedIn', project.linkedin_url],
  ];

  return (
    <main className="shell">
      <p className="eyebrow">
        <Link href={`/events/${project.event_slug}/gallery`}>Gallery</Link>
      </p>
      <div className="spread">
        <h1>{project.name}</h1>
        <div className="row">
          <TrackBadge track={project.track} />
          {project.status === 'draft' && <span className="pill warn">Draft</span>}
          {project.tier === 'winner' && <span className="pill ok">Winner</span>}
          {project.tier === 'community_tier' && (
            <span className="pill warn">Community vote round</span>
          )}
          {project.can_edit && (
            <Link className="button" href={`/submissions/${project.id}/edit`}>
              Edit
            </Link>
          )}
          {project.status === 'submitted' && (
            <Link className="button" href={`/submissions/${project.id}/report`}>
              Judge feedback
            </Link>
          )}
        </div>
      </div>
      <p className="muted" style={{ maxWidth: 640 }}>
        {project.tagline}
      </p>

      <div
        className="grid"
        style={{ gridTemplateColumns: 'minmax(0, 2fr) minmax(260px, 1fr)', marginTop: 20 }}
      >
        <div className="stack">
          <Thumbnail
            src={project.thumbnail_url}
            id={project.id}
            name={project.name}
            track={project.track}
            techTags={project.tech_tags}
            submittedAt={project.submitted_at}
          />

          {project.description && (
            <section className="panel">
              <h2>About</h2>
              <p className="prose" style={{ margin: 0 }}>
                {project.description}
              </p>
            </section>
          )}

          {project.answers.filter((a) => a.value).length > 0 && (
            <section className="panel stack">
              <h2>Questions</h2>
              {project.answers
                .filter((answer) => answer.value)
                .map((answer) => (
                  <div key={answer.question_id}>
                    <p className="muted small" style={{ margin: '0 0 4px' }}>
                      {answer.prompt}
                    </p>
                    <p className="prose" style={{ margin: 0 }}>
                      {answer.value}
                    </p>
                  </div>
                ))}
            </section>
          )}

          {project.gallery_image_urls.length > 0 && (
            <section className="panel">
              <h2>Gallery</h2>
              <div className="grid">
                {project.gallery_image_urls.map((url) => (
                  // eslint-disable-next-line @next/next/no-img-element
                  <img key={url} className="thumb" src={url} alt="" />
                ))}
              </div>
            </section>
          )}
        </div>

        <aside className="stack">
          <section className="panel">
            <h3>Team</h3>
            <p style={{ margin: '0 0 6px' }}>
              <Link href={`/teams/${project.team_id}`}>{project.team_name}</Link>
            </p>
            <ul className="muted small" style={{ margin: 0, paddingLeft: 18 }}>
              {project.members.map((member) => (
                <li key={member.id}>{member.display_name}</li>
              ))}
            </ul>
            {project.discord_usernames.length > 0 && (
              <p className="muted small" style={{ margin: '8px 0 0' }}>
                Discord: {project.discord_usernames.join(', ')}
              </p>
            )}
          </section>

          <section className="panel">
            <h3>Links</h3>
            <dl className="meta small">
              {links.map(([label, raw]) => {
                // Second layer against a javascript: URI stored in a link field --
                // see lib/format.safeHref. The API already rejects anything but
                // http(s) on write; this is what stops a bad value from ever
                // becoming a clickable href even if that ever stopped being true.
                const href = safeHref(raw);
                return (
                  <Fragment key={label}>
                    <dt>{label}</dt>
                    <dd>
                      {href ? (
                        <a href={href} rel="noreferrer noopener nofollow" target="_blank">
                          {href.replace(/^https?:\/\//, '')}
                        </a>
                      ) : (
                        <span className="muted">--</span>
                      )}
                    </dd>
                  </Fragment>
                );
              })}
              <dt>Submitted</dt>
              <dd>{project.submitted_at ? formatDateTime(project.submitted_at) : 'not yet'}</dd>
            </dl>
          </section>

          <section className="panel">
            <h3>Built with</h3>
            {project.tech_tags.length > 0 ? (
              <Tags tags={project.tech_tags} eventSlug={project.event_slug} />
            ) : (
              <p className="muted small" style={{ margin: 0 }}>
                Not listed.
              </p>
            )}
          </section>
        </aside>
      </div>

      <CommentThread projectId={id} error={error} page={page} />
    </main>
  );
}

/**
 * The public comment thread.
 *
 * Hidden comments never reach a non-staff reader: the API filters them through the
 * same predicate that enforces the rule, so this component renders whatever it is
 * given and does no filtering of its own. Staff see hidden entries marked, because
 * moderation that cannot be reviewed is not moderation.
 */
async function CommentThread({
  projectId,
  error,
  page: pageParam,
}: {
  projectId: string;
  error?: string;
  page?: string;
}) {
  const page = Number(pageParam) || 1;
  const [result, me] = await Promise.all([
    apiOrNull<Page<Comment>>(`/api/gallery/${projectId}/comments?page=${page}`),
    getMe(),
  ]);

  // 404 when the project is not in the gallery -- a draft has no public thread.
  if (result === null) return null;
  const comments = result.items;

  const isStaff = atLeast(me.role, 'organizer');

  return (
    <section id="comments" className="panel" style={{ marginTop: 24 }}>
      <h2 style={{ fontSize: 20, marginTop: 0 }}>
        Comments{' '}
        <span className="muted">
          ({comments.filter((c) => !c.is_hidden).length}
          {result.pages > 1 ? ` on this page` : ''})
        </span>
      </h2>

      {error && <p className="notice bad">{error}</p>}

      {comments.length === 0 ? (
        <Empty>No comments yet.</Empty>
      ) : (
        <div className="stack">
          {comments.map((comment) => (
            <article
              key={comment.id}
              className={comment.is_hidden ? 'card flagged' : 'card'}
            >
              <div className="spread">
                <div>
                  <strong>{comment.author.display_name}</strong>
                  <span className="muted" style={{ fontSize: 13, marginLeft: 8 }}>
                    {formatDateTime(comment.created_at)}
                  </span>
                  {comment.is_hidden && (
                    <span className="pill warn" style={{ marginLeft: 8 }}>
                      Hidden
                    </span>
                  )}
                </div>
              </div>

              <p style={{ marginBottom: 0, whiteSpace: 'pre-wrap' }}>{comment.body}</p>

              {comment.is_hidden && comment.hidden_reason && (
                <p className="muted" style={{ fontSize: 13, marginBottom: 0 }}>
                  Hidden: {comment.hidden_reason}
                </p>
              )}

              <div className="row" style={{ marginTop: 10 }}>
                {comment.can_remove && (
                  <form action={removeCommentAction}>
                    <input type="hidden" name="submission_id" value={projectId} />
                    <input type="hidden" name="comment_id" value={comment.id} />
                    <button type="submit" className="button small">
                      Remove
                    </button>
                  </form>
                )}
                {isStaff && !comment.is_hidden && (
                  <form action={hideCommentAction} className="row">
                    <input type="hidden" name="submission_id" value={projectId} />
                    <input type="hidden" name="comment_id" value={comment.id} />
                    <input
                      name="reason"
                      required
                      placeholder="reason (recorded in the audit log)"
                      style={{ width: 280 }}
                    />
                    <button type="submit" className="button small">
                      Hide
                    </button>
                  </form>
                )}
              </div>
            </article>
          ))}
        </div>
      )}

      <Pagination
        page={result.page}
        pages={result.pages}
        total={result.total}
        basePath={`/projects/${projectId}`}
        noun="comment"
      />

      {me.authenticated ? (
        <form action={postCommentAction} style={{ marginTop: 18 }}>
          <input type="hidden" name="submission_id" value={projectId} />
          <label htmlFor="body">Add a comment</label>
          <textarea id="body" name="body" rows={3} required maxLength={4000} />
          <button type="submit" className="button primary" style={{ marginTop: 8 }}>
            Post
          </button>
        </form>
      ) : (
        <p className="muted" style={{ fontSize: 13, marginTop: 18, marginBottom: 0 }}>
          <a href="/login">Sign in</a> to comment. An account is required on purpose:
          an anonymous comment box on a public gallery is a spam endpoint.
        </p>
      )}
    </section>
  );
}
