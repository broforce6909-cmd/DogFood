"""The embeddable gallery widget.

An organizer wants their projects on their own event site. Three ways to do that,
and the choice matters more than the feature:

1. **A JSON endpoint and "write your own HTML".** Not a widget.
2. **A script that injects markup into the host page.** This is how most embed
   widgets work, and it means our code runs in *their* origin with access to their
   DOM and cookies. If this project is ever compromised, every site that embedded it
   is compromised too.
3. **An iframe.** The widget renders in its own origin, sandboxed by the browser.
   Our CSS cannot leak out, their CSS cannot leak in, and a compromise here cannot
   read their page.

We do (3), and the `/embed/gallery/{slug}.js` script exists only to write the
`<iframe>` tag for people who would rather paste one line. It is a convenience, not
the mechanism.

What the widget shows is exactly what `GET /api/gallery` already serves to an
anonymous visitor — no new data, no new access path. The page is deliberately
self-contained: inline CSS, no external fonts, no JavaScript at all in the frame.
"""

from __future__ import annotations

import html
import json

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse, PlainTextResponse
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from ..access import PUBLIC_SUBMISSION_CRITERIA
from ..config import settings
from ..db import get_db
from ..models import Event, Submission, Track

router = APIRouter(prefix="/embed", tags=["embed"])

MAX_ITEMS = 60


def _cards(db: Session, slug: str, limit: int, track: str | None):
    """The same predicate the public gallery uses. No second definition of public."""
    stmt = (
        select(Submission)
        .options(selectinload(Submission.team), selectinload(Submission.track))
        .join(Event, Event.id == Submission.event_id)
        .where(Event.slug == slug, *PUBLIC_SUBMISSION_CRITERIA)
    )
    if track:
        stmt = stmt.join(Track, Track.id == Submission.track_id).where(Track.key == track)
    stmt = stmt.order_by(Submission.submitted_at.desc()).limit(limit)
    return list(db.execute(stmt).scalars().all())


# NOTE ON ROUTE ORDER: the `.js` snippet is declared *before* the frame, because
# FastAPI matches in declaration order and `{slug}` matches `my-event.js` perfectly
# happily. With the frame first, the snippet route is unreachable and the frame
# renders for a slug of "my-event.js" -- a 200 that looks like it works. The test
# `test_the_snippet_writes_an_iframe_not_markup` exists to catch a reordering.


@router.get("/gallery/{slug}.js", response_class=PlainTextResponse)
def gallery_snippet(
    slug: str,
    request: Request,
    limit: int = Query(default=12, ge=1, le=MAX_ITEMS),
    track: str | None = None,
    theme: str = Query(default="light", pattern="^(light|dark)$"),
) -> PlainTextResponse:
    """One line to paste. Writes the iframe, and nothing else.

        <script src="http://localhost:8000/embed/gallery/my-event.js"></script>

    It deliberately does **not** inject gallery markup into the host page. Rendering
    our HTML in their origin would put our code inside their security boundary; the
    iframe keeps it in ours. This script's entire job is to emit the tag.
    """
    base = str(request.base_url).rstrip("/")
    query = f"?limit={limit}&theme={theme}" + (f"&track={track}" if track else "")
    src = f"{base}/embed/gallery/{slug}{query}"

    script = f"""(function () {{
  // Written as document.write so the iframe lands where the <script> tag is,
  // with no DOM scanning and no assumptions about the host page's structure.
  var src = {json.dumps(src)};
  var frame =
    '<iframe src="' + src + '" title="Project gallery" loading="lazy" ' +
    'style="width:100%;min-height:420px;border:0" ' +
    // The frame needs nothing: no scripts, no forms, no same-origin access.
    'sandbox="allow-popups allow-popups-to-escape-sandbox"></iframe>';
  document.write(frame);
}})();
"""
    return PlainTextResponse(
        script,
        media_type="application/javascript",
        headers={"cache-control": "public, max-age=300"},
    )


@router.get("/gallery/{slug}", response_class=HTMLResponse)
def gallery_frame(
    slug: str,
    limit: int = Query(default=12, ge=1, le=MAX_ITEMS),
    track: str | None = None,
    theme: str = Query(default="light", pattern="^(light|dark)$"),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """A self-contained gallery page, meant to be framed.

    `X-Frame-Options` is deliberately **not** set and the CSP allows any framing
    ancestor: the whole point is for somebody else's site to embed this. That is a
    decision rather than an omission, and it is safe here because the page is public,
    read-only, carries no session and accepts no input.

    Note what is *not* sent: no cookie is read, so framing this cannot be used to
    ride a visitor's session.
    """
    event = db.execute(select(Event).where(Event.slug == slug)).scalar_one_or_none()
    rows = _cards(db, slug, limit, track) if event and event.is_published else []

    dark = theme == "dark"
    bg, fg, muted, line = (
        ("#14161a", "#f3f4f6", "#9aa1ab", "#2a2e35")
        if dark
        else ("#ffffff", "#14161a", "#5f6672", "#e5e7eb")
    )

    if event is None or not event.is_published:
        body = '<p class="empty">That event is not available.</p>'
    elif not rows:
        body = '<p class="empty">No projects have been published yet.</p>'
    else:
        cards = []
        for row in rows:
            name = html.escape(row.name or "")
            tagline = html.escape(row.tagline or "")
            team = html.escape(row.team.name if row.team else "")
            track_name = html.escape(row.track.name) if row.track else ""
            url = f"{settings.web_base_url.rstrip('/')}/projects/{row.id}"
            tags = "".join(
                f'<span class="tag">{html.escape(t)}</span>' for t in (row.tech_tags or [])[:4]
            )
            cards.append(
                f'<a class="card" href="{html.escape(url)}" target="_blank" '
                f'rel="noopener noreferrer">'
                f'<strong>{name}</strong>'
                + (f'<span class="track">{track_name}</span>' if track_name else "")
                + f'<span class="tagline">{tagline}</span>'
                f'<span class="team">{team}</span>'
                f'<span class="tags">{tags}</span>'
                "</a>"
            )
        body = f'<div class="grid">{"".join(cards)}</div>'

    title = html.escape(event.name if event else "Gallery")
    credit_url = f"{settings.web_base_url.rstrip('/')}/events/{html.escape(slug)}/gallery"

    # Inline everything. An embed that pulls a font or a stylesheet from elsewhere is
    # an embed that breaks, or leaks a referrer, on somebody else's page.
    page = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
  :root {{ color-scheme: {"dark" if dark else "light"}; }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0; padding: 12px; background: {bg}; color: {fg};
    font: 14px/1.45 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
  }}
  .grid {{
    display: grid; gap: 10px;
    grid-template-columns: repeat(auto-fill, minmax(200px, 1fr));
  }}
  .card {{
    display: flex; flex-direction: column; gap: 4px; padding: 12px;
    border: 1px solid {line}; border-radius: 10px; text-decoration: none; color: inherit;
    background: {bg};
  }}
  .card:hover {{ border-color: #8a6bff; }}
  .card strong {{ font-size: 15px; }}
  .tagline {{ color: {muted}; font-size: 13px; }}
  .team, .track {{ color: {muted}; font-size: 12px; }}
  .track {{ text-transform: uppercase; letter-spacing: .06em; }}
  .tags {{ display: flex; flex-wrap: wrap; gap: 4px; margin-top: 4px; }}
  .tag {{
    font-size: 11px; padding: 2px 6px; border: 1px solid {line};
    border-radius: 4px; color: {muted};
  }}
  .empty {{ color: {muted}; }}
  footer {{ margin-top: 12px; font-size: 11px; color: {muted}; }}
  footer a {{ color: inherit; }}
</style></head>
<body>
{body}
<footer>
<a href="{credit_url}" target="_blank" rel="noopener noreferrer">Powered by Dogfood</a>
</footer>
</body></html>"""

    return HTMLResponse(
        page,
        headers={
            # Framing is the feature. Allowed from anywhere, and scoped by the page
            # being public and read-only.
            "content-security-policy": (
                "frame-ancestors *; default-src 'none'; style-src 'unsafe-inline'"
            ),
            "cache-control": "public, max-age=60",
            "referrer-policy": "no-referrer",
        },
    )
