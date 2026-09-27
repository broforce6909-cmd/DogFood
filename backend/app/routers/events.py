"""Events, and the tracks, prizes and custom questions that configure them.

An event is the unit everything else hangs off: teams belong to one, submissions
belong to one, and every deadline in the system is a column on this table.

Tracks, prizes and questions are child rows rather than JSON on the event.
That costs three small tables and buys real foreign keys -- a submission cannot
reference a track from another event, and a track rename does not orphan
anything, because identity is a primary key rather than a string.
"""

from __future__ import annotations

import uuid
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from ..access import PLATFORM, Action, Principal, check_access, require_access
from ..audit import quote, record
from ..db import get_db
from ..deps import get_current_principal
from ..models import AuditAction, Event, EventQuestion, Prize, Track, utcnow
from ..schemas import (
    EventCreate,
    EventOut,
    EventSummary,
    EventUpdate,
    PrizeIn,
    PrizeOut,
    QuestionIn,
    QuestionOut,
    TrackIn,
    TrackOut,
)
from ..serializers import event_out, event_summary

router = APIRouter(prefix="/api/events", tags=["events"])

_EVENT_LOADERS = (
    selectinload(Event.tracks),
    selectinload(Event.prizes),
    selectinload(Event.questions),
)


def load_event(db: Session, slug: str) -> Event:
    event = db.execute(
        select(Event).options(*_EVENT_LOADERS).where(Event.slug == slug)
    ).scalar_one_or_none()
    if event is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Event not found")
    return event


def readable_event(db: Session, slug: str, user: Principal) -> Event:
    """Load, then authorize. An unpublished event 404s rather than 403s: a 403
    would confirm that the slug exists."""
    event = load_event(db, slug)
    require_access(user, event, Action.READ, status_code=status.HTTP_404_NOT_FOUND)
    return event


def manageable_event(db: Session, slug: str, user: Principal) -> Event:
    event = readable_event(db, slug, user)
    require_access(user, event, Action.MANAGE)
    return event


def _validate_windows(event: Event) -> None:
    """Mirror the table's CHECK constraints so a bad PATCH is a 422, not a 500."""
    if event.ends_at < event.starts_at:
        raise HTTPException(status_code=422, detail="ends_at must not precede starts_at")
    if event.submission_deadline < event.submission_opens_at:
        raise HTTPException(
            status_code=422, detail="submission_deadline must not precede submission_opens_at"
        )
    if (
        event.judging_opens_at
        and event.judging_closes_at
        and event.judging_closes_at < event.judging_opens_at
    ):
        raise HTTPException(
            status_code=422, detail="judging_closes_at must not precede judging_opens_at"
        )
    if (
        event.voting_opens_at
        and event.voting_closes_at
        and event.voting_closes_at < event.voting_opens_at
    ):
        raise HTTPException(
            status_code=422, detail="voting_closes_at must not precede voting_opens_at"
        )


# --------------------------------------------------------------------------- #
# Events
# --------------------------------------------------------------------------- #


@router.get("", response_model=list[EventSummary])
def list_events(
    archived: Literal["exclude", "include", "only"] = Query(
        default="exclude",
        description=(
            "Archived events are left out by default. `only` lists just the archived "
            "ones, `include` lists both."
        ),
    ),
    principal: Principal = Depends(get_current_principal),
    db: Session = Depends(get_db),
) -> list[EventSummary]:
    """Visitors see published events; staff see everything.

    The SQL below is a coarse prefilter and the `check_access` call is the
    decision -- the list an actor gets is exactly the list of events they would
    be allowed to open individually, by construction rather than by hope.
    Archival only ever narrows *which of those* are listed: an archived event is
    still reachable by its link, and `archived=only|include` brings it back.
    """
    rows = db.execute(select(Event).order_by(Event.starts_at.desc())).scalars().all()
    visible = [e for e in rows if check_access(principal, e, Action.READ)]
    if archived == "exclude":
        visible = [e for e in visible if e.archived_at is None]
    elif archived == "only":
        visible = [e for e in visible if e.archived_at is not None]
    return [event_summary(e) for e in visible]


@router.post("", response_model=EventOut, status_code=status.HTTP_201_CREATED)
def create_event(
    payload: EventCreate,
    request: Request,
    principal: Principal = Depends(get_current_principal),
    db: Session = Depends(get_db),
) -> EventOut:
    require_access(principal, PLATFORM, Action.CREATE_EVENT)
    event = Event(
        **payload.model_dump(),
        created_by_id=getattr(principal, "id", None),
    )
    db.add(event)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="That slug is already taken"
        ) from None
    record(
        db,
        action=AuditAction.EVENT_CREATED,
        summary=f"{principal.email} created {event.slug}",
        principal=principal,
        event=event,
        resource_type="event",
        resource_id=event.id,
        request=request,
    )
    db.commit()
    db.refresh(event)
    return event_out(event)


@router.get("/{slug}", response_model=EventOut)
def get_event(
    slug: str,
    principal: Principal = Depends(get_current_principal),
    db: Session = Depends(get_db),
) -> EventOut:
    return event_out(readable_event(db, slug, principal))


@router.patch("/{slug}", response_model=EventOut)
def update_event(
    slug: str,
    payload: EventUpdate,
    request: Request,
    principal: Principal = Depends(get_current_principal),
    db: Session = Depends(get_db),
) -> EventOut:
    """The slug is deliberately not editable. A gallery URL that a participant
    put in a CV two years ago should still resolve."""
    event = manageable_event(db, slug, principal)
    fields = payload.model_dump(exclude_unset=True)
    for field, value in fields.items():
        setattr(event, field, value)
    _validate_windows(event)
    if fields:
        record(
            db,
            action=AuditAction.EVENT_CHANGED,
            summary=(
                f"{principal.email} changed {event.slug} "
                f"({', '.join(sorted(fields))})"
            ),
            principal=principal,
            event=event,
            resource_type="event",
            resource_id=event.id,
            request=request,
        )
    db.commit()
    db.refresh(event)
    return event_out(event)


@router.post("/{slug}/archive", response_model=EventOut)
def archive_event(
    slug: str,
    request: Request,
    principal: Principal = Depends(get_current_principal),
    db: Session = Depends(get_db),
) -> EventOut:
    """Freeze an event and take it out of the default lists.

    Nothing is deleted or hidden from anyone who has the link: the gallery, the
    results and every certificate keep working. What stops is *change* -- every
    write to the event or anything that belongs to it is refused (see
    `app.access.check_access`) until it is unarchived. Staff only, and not a
    read-only admin.
    """
    event = readable_event(db, slug, principal)
    require_access(principal, event, Action.ARCHIVE)
    if event.archived_at is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Already archived")
    event.archived_at = utcnow()
    record(
        db,
        action=AuditAction.EVENT_ARCHIVED,
        summary=f"{principal.email} archived {event.slug} (frozen, read-only)",
        principal=principal,
        event=event,
        resource_type="event",
        resource_id=event.id,
        request=request,
    )
    db.commit()
    db.refresh(event)
    return event_out(event)


@router.post("/{slug}/unarchive", response_model=EventOut)
def unarchive_event(
    slug: str,
    request: Request,
    principal: Principal = Depends(get_current_principal),
    db: Session = Depends(get_db),
) -> EventOut:
    """Lift the freeze. The event returns to the default lists exactly as it was."""
    event = readable_event(db, slug, principal)
    require_access(principal, event, Action.ARCHIVE)
    if event.archived_at is None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Not archived")
    event.archived_at = None
    record(
        db,
        action=AuditAction.EVENT_UNARCHIVED,
        summary=f"{principal.email} unarchived {event.slug}",
        principal=principal,
        event=event,
        resource_type="event",
        resource_id=event.id,
        request=request,
    )
    db.commit()
    db.refresh(event)
    return event_out(event)


@router.delete("/{slug}", status_code=status.HTTP_204_NO_CONTENT)
def delete_event(
    slug: str,
    request: Request,
    principal: Principal = Depends(get_current_principal),
    db: Session = Depends(get_db),
) -> None:
    event = manageable_event(db, slug, principal)
    # Recorded before the delete, not after: `event_slug` has to be read off a
    # live ORM instance, and `event` is an expired, deleted one the moment
    # `db.delete()` + `db.commit()` have run. `AuditEntry.event_id` itself is
    # `ON DELETE SET NULL` rather than `CASCADE` precisely so this entry -- the
    # one recording the deletion -- survives it; the write just has to happen
    # while there is still an `Event` object to read the slug from.
    record(
        db,
        action=AuditAction.EVENT_DELETED,
        summary=f"{principal.email} deleted {event.slug}",
        principal=principal,
        event=event,
        resource_type="event",
        resource_id=event.id,
        request=request,
    )
    db.delete(event)
    db.commit()


# --------------------------------------------------------------------------- #
# Tracks
# --------------------------------------------------------------------------- #


@router.get("/{slug}/tracks", response_model=list[TrackOut])
def list_tracks(
    slug: str,
    principal: Principal = Depends(get_current_principal),
    db: Session = Depends(get_db),
) -> list[TrackOut]:
    event = readable_event(db, slug, principal)
    return [TrackOut.model_validate(t) for t in event.tracks]


@router.post("/{slug}/tracks", response_model=TrackOut, status_code=status.HTTP_201_CREATED)
def create_track(
    slug: str,
    payload: TrackIn,
    request: Request,
    principal: Principal = Depends(get_current_principal),
    db: Session = Depends(get_db),
) -> TrackOut:
    event = manageable_event(db, slug, principal)
    track = Track(event_id=event.id, **payload.model_dump())
    db.add(track)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A track with that key already exists in this event",
        ) from None
    record(
        db,
        action=AuditAction.EVENT_CONFIG_CHANGED,
        summary=f"{principal.email} added track {quote(track.name)} on {event.slug}",
        principal=principal,
        event=event,
        resource_type="track",
        resource_id=track.id,
        request=request,
    )
    db.commit()
    db.refresh(track)
    return TrackOut.model_validate(track)


@router.patch("/{slug}/tracks/{track_id}", response_model=TrackOut)
def update_track(
    slug: str,
    track_id: uuid.UUID,
    payload: TrackIn,
    request: Request,
    principal: Principal = Depends(get_current_principal),
    db: Session = Depends(get_db),
) -> TrackOut:
    event = manageable_event(db, slug, principal)
    track = next((t for t in event.tracks if t.id == track_id), None)
    if track is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Track not found")
    for field, value in payload.model_dump().items():
        setattr(track, field, value)
    record(
        db,
        action=AuditAction.EVENT_CONFIG_CHANGED,
        summary=f"{principal.email} edited track {quote(track.name)} on {event.slug}",
        principal=principal,
        event=event,
        resource_type="track",
        resource_id=track.id,
        request=request,
    )
    db.commit()
    db.refresh(track)
    return TrackOut.model_validate(track)


@router.delete("/{slug}/tracks/{track_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_track(
    slug: str,
    track_id: uuid.UUID,
    request: Request,
    principal: Principal = Depends(get_current_principal),
    db: Session = Depends(get_db),
) -> None:
    """Submissions in the track are not deleted with it -- the composite foreign
    key sets their `track_id` to NULL, which is the honest outcome."""
    event = manageable_event(db, slug, principal)
    track = next((t for t in event.tracks if t.id == track_id), None)
    if track is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Track not found")
    record(
        db,
        action=AuditAction.EVENT_CONFIG_CHANGED,
        summary=f"{principal.email} removed track {quote(track.name)} on {event.slug}",
        principal=principal,
        event=event,
        resource_type="track",
        resource_id=track.id,
        request=request,
    )
    db.delete(track)
    db.commit()


# --------------------------------------------------------------------------- #
# Prizes
# --------------------------------------------------------------------------- #


@router.get("/{slug}/prizes", response_model=list[PrizeOut])
def list_prizes(
    slug: str,
    principal: Principal = Depends(get_current_principal),
    db: Session = Depends(get_db),
) -> list[PrizeOut]:
    event = readable_event(db, slug, principal)
    return [PrizeOut.model_validate(p) for p in event.prizes]


@router.post("/{slug}/prizes", response_model=PrizeOut, status_code=status.HTTP_201_CREATED)
def create_prize(
    slug: str,
    payload: PrizeIn,
    request: Request,
    principal: Principal = Depends(get_current_principal),
    db: Session = Depends(get_db),
) -> PrizeOut:
    event = manageable_event(db, slug, principal)
    prize = Prize(event_id=event.id, **payload.model_dump())
    db.add(prize)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="That track does not belong to this event",
        ) from None
    record(
        db,
        action=AuditAction.EVENT_CONFIG_CHANGED,
        summary=f"{principal.email} added prize {quote(prize.title)} on {event.slug}",
        principal=principal,
        event=event,
        resource_type="prize",
        resource_id=prize.id,
        request=request,
    )
    db.commit()
    db.refresh(prize)
    return PrizeOut.model_validate(prize)


@router.patch("/{slug}/prizes/{prize_id}", response_model=PrizeOut)
def update_prize(
    slug: str,
    prize_id: uuid.UUID,
    payload: PrizeIn,
    request: Request,
    principal: Principal = Depends(get_current_principal),
    db: Session = Depends(get_db),
) -> PrizeOut:
    event = manageable_event(db, slug, principal)
    prize = next((p for p in event.prizes if p.id == prize_id), None)
    if prize is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Prize not found")
    for field, value in payload.model_dump().items():
        setattr(prize, field, value)
    record(
        db,
        action=AuditAction.EVENT_CONFIG_CHANGED,
        summary=f"{principal.email} edited prize {quote(prize.title)} on {event.slug}",
        principal=principal,
        event=event,
        resource_type="prize",
        resource_id=prize.id,
        request=request,
    )
    db.commit()
    db.refresh(prize)
    return PrizeOut.model_validate(prize)


@router.delete("/{slug}/prizes/{prize_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_prize(
    slug: str,
    prize_id: uuid.UUID,
    request: Request,
    principal: Principal = Depends(get_current_principal),
    db: Session = Depends(get_db),
) -> None:
    event = manageable_event(db, slug, principal)
    prize = next((p for p in event.prizes if p.id == prize_id), None)
    if prize is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Prize not found")
    record(
        db,
        action=AuditAction.EVENT_CONFIG_CHANGED,
        summary=f"{principal.email} removed prize {quote(prize.title)} on {event.slug}",
        principal=principal,
        event=event,
        resource_type="prize",
        resource_id=prize.id,
        request=request,
    )
    db.delete(prize)
    db.commit()


# --------------------------------------------------------------------------- #
# Custom questions
# --------------------------------------------------------------------------- #


@router.get("/{slug}/questions", response_model=list[QuestionOut])
def list_questions(
    slug: str,
    principal: Principal = Depends(get_current_principal),
    db: Session = Depends(get_db),
) -> list[QuestionOut]:
    event = readable_event(db, slug, principal)
    return [QuestionOut.model_validate(q) for q in event.questions]


@router.post(
    "/{slug}/questions", response_model=QuestionOut, status_code=status.HTTP_201_CREATED
)
def create_question(
    slug: str,
    payload: QuestionIn,
    request: Request,
    principal: Principal = Depends(get_current_principal),
    db: Session = Depends(get_db),
) -> QuestionOut:
    event = manageable_event(db, slug, principal)
    question = EventQuestion(event_id=event.id, **payload.model_dump())
    db.add(question)
    db.flush()
    record(
        db,
        action=AuditAction.EVENT_CONFIG_CHANGED,
        summary=f"{principal.email} added question {quote(question.prompt)} on {event.slug}",
        principal=principal,
        event=event,
        resource_type="question",
        resource_id=question.id,
        request=request,
    )
    db.commit()
    db.refresh(question)
    return QuestionOut.model_validate(question)


@router.patch("/{slug}/questions/{question_id}", response_model=QuestionOut)
def update_question(
    slug: str,
    question_id: uuid.UUID,
    payload: QuestionIn,
    request: Request,
    principal: Principal = Depends(get_current_principal),
    db: Session = Depends(get_db),
) -> QuestionOut:
    event = manageable_event(db, slug, principal)
    question = next((q for q in event.questions if q.id == question_id), None)
    if question is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Question not found")
    for field, value in payload.model_dump().items():
        setattr(question, field, value)
    record(
        db,
        action=AuditAction.EVENT_CONFIG_CHANGED,
        summary=f"{principal.email} edited question {quote(question.prompt)} on {event.slug}",
        principal=principal,
        event=event,
        resource_type="question",
        resource_id=question.id,
        request=request,
    )
    db.commit()
    db.refresh(question)
    return QuestionOut.model_validate(question)


@router.delete("/{slug}/questions/{question_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_question(
    slug: str,
    question_id: uuid.UUID,
    request: Request,
    principal: Principal = Depends(get_current_principal),
    db: Session = Depends(get_db),
) -> None:
    """Deleting a question deletes its answers. Said out loud because the
    cascade is a data-loss path an organizer should know about."""
    event = manageable_event(db, slug, principal)
    question = next((q for q in event.questions if q.id == question_id), None)
    if question is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Question not found")
    record(
        db,
        action=AuditAction.EVENT_CONFIG_CHANGED,
        summary=(
            f"{principal.email} removed a custom question "
            f"({quote(question.prompt)}) on {event.slug} -- its answers go with it"
        ),
        principal=principal,
        event=event,
        resource_type="question",
        resource_id=question.id,
        request=request,
    )
    db.delete(question)
    db.commit()


__all__ = ["router", "load_event", "readable_event", "manageable_event", "utcnow"]
