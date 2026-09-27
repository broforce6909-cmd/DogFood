"""The public gallery: search, filter, paginate.

This is the one endpoint that returns a *collection* whose members the caller
was never individually authorized for, so it is the one place where a filtered
query could quietly become the access control. It does not:
`PUBLIC_SUBMISSION_CRITERIA` lives next to `check_access` in `app.access`, and a
test asserts that the rows this query returns are exactly the rows the predicate
would let a visitor read.

Search is Postgres `ILIKE` over name, tagline, description and team name, plus
an array-containment match on tech tags. No search service: at hackathon scale
this is not a query-planning problem, it is a `LIKE`.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import Select, func, nulls_last, or_, select
from sqlalchemy.orm import Session, selectinload

from ..access import PUBLIC_SUBMISSION_CRITERIA
from ..db import get_db
from ..models import Event, Submission, Team, Track
from ..schemas import GalleryPage, GallerySort, SubmissionCard
from ..serializers import submission_card

router = APIRouter(prefix="/api/gallery", tags=["gallery"])

DEFAULT_PER_PAGE = 24
MAX_PER_PAGE = 60


def _visible(stmt: Select) -> Select:
    """Submitted projects in published events. Nothing else is in the gallery."""
    return (
        stmt.join(Event, Event.id == Submission.event_id)
        .join(Team, Team.id == Submission.team_id)
        .where(*PUBLIC_SUBMISSION_CRITERIA)
    )


def _filtered(
    stmt: Select,
    *,
    event: str | None = None,
    q: str | None = None,
    track: str | None = None,
    tag: str | None = None,
) -> Select:
    if event:
        stmt = stmt.where(Event.slug == event)
    if track:
        stmt = stmt.join(Track, Track.id == Submission.track_id).where(Track.key == track)
    if tag:
        # Array containment, which is what the GIN index on tech_tags serves.
        stmt = stmt.where(Submission.tech_tags.contains([tag.strip().lower()]))
    if q:
        pattern = f"%{q.strip()}%"
        stmt = stmt.where(
            or_(
                Submission.name.ilike(pattern),
                Submission.tagline.ilike(pattern),
                Submission.description.ilike(pattern),
                Team.name.ilike(pattern),
            )
        )
    return stmt


@router.get("", response_model=GalleryPage)
def browse(
    event: str | None = Query(default=None, description="Event slug"),
    q: str | None = Query(default=None, description="Free text: name, tagline, description, team"),
    track: str | None = Query(default=None, description="Track key"),
    tag: str | None = Query(default=None, description="A single tech tag"),
    sort: GallerySort | None = Query(
        default=None,
        description=(
            "`mixed` (the default across events) takes turns between events, newest first "
            "within each; `recent` is strictly newest first (the default for one event)"
        ),
    ),
    page: int = Query(default=1, ge=1),
    per_page: int = Query(default=DEFAULT_PER_PAGE, ge=1, le=MAX_PER_PAGE),
    db: Session = Depends(get_db),
) -> GalleryPage:
    """Open to visitors by design -- the gallery is the public face of an event."""
    stmt = _filtered(_visible(select(Submission)), event=event, q=q, track=track, tag=tag)

    total = db.execute(select(func.count()).select_from(stmt.subquery())).scalar_one()

    newest_first = nulls_last(Submission.submitted_at.desc())
    order = {
        "recent": (newest_first,),
        # Each event's newest project, then each event's second newest, and so
        # on. Across events, "newest first" lets whichever event submitted last
        # fill the whole first page; this keeps every event on it. The rank is
        # taken after the filters, so a search only spreads over its own matches.
        "mixed": (
            func.row_number().over(
                partition_by=Submission.event_id, order_by=(newest_first, Submission.id)
            ),
            newest_first,
        ),
        "name": (Submission.name.asc(),),
        "team": (Team.name.asc(),),
    }[sort or ("recent" if event else "mixed")]

    rows = (
        db.execute(
            stmt.options(selectinload(Submission.team), selectinload(Submission.track))
            .order_by(*order, Submission.id)
            .offset((page - 1) * per_page)
            .limit(per_page)
        )
        .scalars()
        .all()
    )

    return GalleryPage(
        items=[submission_card(s) for s in rows],
        total=total,
        page=page,
        per_page=per_page,
        pages=max(1, -(-total // per_page)),
    )


@router.get("/tags", response_model=list[str])
def tags(
    event: str | None = Query(default=None, description="Event slug"),
    db: Session = Depends(get_db),
) -> list[str]:
    """The tag vocabulary actually in use, for building a filter control.

    Unnested in the database rather than flattened in Python, so the size of the
    response does not depend on how many projects there are.
    """
    tag_column = func.unnest(Submission.tech_tags).label("tag")
    inner = _filtered(_visible(select(tag_column)), event=event).subquery()
    rows = db.execute(select(inner.c.tag).distinct().order_by(inner.c.tag)).scalars().all()
    return [row for row in rows if row]


@router.get("/{submission_id}", response_model=SubmissionCard)
def card(submission_id: uuid.UUID, db: Session = Depends(get_db)) -> SubmissionCard:
    """One gallery card. The full project page is `/api/submissions/{id}`, which
    applies the same read predicate as everything else in the API."""
    stmt = (
        _visible(select(Submission))
        .where(Submission.id == submission_id)
        .options(selectinload(Submission.team), selectinload(Submission.track))
    )
    row = db.execute(stmt).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not in the gallery")
    return submission_card(row)
