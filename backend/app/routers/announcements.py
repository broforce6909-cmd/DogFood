"""Organizer announcements.

One flag, `visible_to_visitors`: unset, an announcement is for people who are
actually part of the event (participant rank or above, or staff);
set, it is also shown to a signed-out visitor on the public event page. See
`models.Announcement`'s own docstring for the full reasoning, and
`access._check_announcement` for the rule this list applies per row.

Posting is staff-only and fires `announcement.posted`, the same
audit-and-webhook shape as every other organizer-initiated event in this
codebase (`results.published`, `certificate.issued`, ...).
"""

from __future__ import annotations

from fastapi import APIRouter, BackgroundTasks, Depends, Request
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from ..access import Action, Principal, check_access, require_authenticated
from ..audit import quote, record
from ..db import get_db
from ..deps import get_current_principal
from ..hooks import schedule
from ..models import Announcement, AuditAction, WebhookEvent
from ..schemas import AnnouncementIn, AnnouncementOut
from .events import manageable_event, readable_event

router = APIRouter(prefix="/api/events", tags=["announcements"])

_LOADERS = (selectinload(Announcement.event), selectinload(Announcement.posted_by))


def _out(announcement: Announcement) -> AnnouncementOut:
    return AnnouncementOut(
        id=announcement.id,
        event_id=announcement.event_id,
        title=announcement.title,
        body=announcement.body,
        visible_to_visitors=announcement.visible_to_visitors,
        posted_by_display_name=(
            announcement.posted_by.display_name if announcement.posted_by else None
        ),
        posted_at=announcement.posted_at,
    )


@router.get("/{slug}/announcements", response_model=list[AnnouncementOut])
def list_announcements(
    slug: str,
    principal: Principal = Depends(get_current_principal),
    db: Session = Depends(get_db),
) -> list[AnnouncementOut]:
    """Newest first. Small and per-event by construction -- an organizer
    posts a handful of notices, not a feed -- so this is a plain list rather
    than a paginated one, the same reasoning `event.tracks`/`event.prizes`
    already use."""
    event = readable_event(db, slug, principal)
    rows = (
        db.execute(
            select(Announcement)
            .options(*_LOADERS)
            .where(Announcement.event_id == event.id)
            .order_by(Announcement.posted_at.desc())
        )
        .scalars()
        .all()
    )
    return [_out(a) for a in rows if check_access(principal, a, Action.READ)]


@router.post("/{slug}/announcements", response_model=AnnouncementOut, status_code=201)
def post_announcement(
    slug: str,
    payload: AnnouncementIn,
    request: Request,
    background: BackgroundTasks,
    principal: Principal = Depends(get_current_principal),
    db: Session = Depends(get_db),
) -> AnnouncementOut:
    event = manageable_event(db, slug, principal)
    user = require_authenticated(principal)

    announcement = Announcement(
        event_id=event.id,
        title=payload.title,
        body=payload.body,
        visible_to_visitors=payload.visible_to_visitors,
        posted_by_id=user.id,
    )
    db.add(announcement)
    db.flush()

    audience = "everyone" if payload.visible_to_visitors else "participants"
    record(
        db,
        action=AuditAction.ANNOUNCEMENT_POSTED,
        summary=(
            f"{user.email} posted {quote(announcement.title)} to {event.slug} "
            f"({audience}): {quote(announcement.body)}"
        ),
        principal=user,
        event=event,
        resource_type="announcement",
        resource_id=announcement.id,
        request=request,
    )
    db.commit()
    db.refresh(announcement)

    schedule(
        background,
        db,
        event.id,
        WebhookEvent.ANNOUNCEMENT_POSTED,
        {
            "event": event.slug,
            "announcement_id": str(announcement.id),
            "title": announcement.title,
            "visible_to_visitors": announcement.visible_to_visitors,
        },
    )

    announcement.posted_by = user  # avoid a re-query; we already have it
    return _out(announcement)
