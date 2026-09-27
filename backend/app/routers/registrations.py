"""Per-event registration, ahead of team formation.

Registering is additive, not a gate: it does not block or require anything
from `Action.CREATE_TEAM`/`Action.JOIN_TEAM` in `app/access.py`, which keep
their own, unchanged rules. This is the event's own roster (who said they were
coming, on what Discord handle, and whether they mean to lead a team) and the
trigger for the registration confirmation email -- not a new authorization
layer team formation has to pass through. See `models.EventRegistration`'s
own docstring for the full reasoning.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from ..access import Action, Principal, require_access, require_authenticated
from ..audit import quote, record
from ..config import settings
from ..db import get_db
from ..deps import get_current_principal
from ..email import schedule_email
from ..hooks import schedule
from ..models import AuditAction, EventRegistration, TeamMember, WebhookEvent
from ..pagination import Page, PageParams, count_of, pages_for
from ..schemas import RegistrationIn, RegistrationOut, RegistrationRemovalIn
from .events import manageable_event, readable_event

router = APIRouter(prefix="/api/events", tags=["registration"])


def _has_team(db: Session, event_id: uuid.UUID, user_id: uuid.UUID) -> bool:
    return (
        db.execute(
            select(TeamMember.user_id).where(
                TeamMember.event_id == event_id, TeamMember.user_id == user_id
            )
        ).scalar_one_or_none()
        is not None
    )


def _registration_out(db: Session, row: EventRegistration) -> RegistrationOut:
    return RegistrationOut(
        id=row.id,
        event_id=row.event_id,
        user_id=row.user_id,
        user_display_name=row.user.display_name,
        email=row.email,
        discord_username=row.discord_username,
        is_team_leader=row.is_team_leader,
        leader_name=row.leader_name,
        registered_at=row.registered_at,
        has_team=_has_team(db, row.event_id, row.user_id),
    )


@router.post("/{slug}/register", response_model=RegistrationOut, status_code=201)
def register(
    slug: str,
    payload: RegistrationIn,
    request: Request,
    background: BackgroundTasks,
    principal: Principal = Depends(get_current_principal),
    db: Session = Depends(get_db),
) -> RegistrationOut:
    """Sign up for this event. One registration per person per event -- a
    second call is a 409, not a silent update; edit via a future PATCH if that
    turns out to be needed, not by re-posting."""
    user = require_authenticated(principal)
    event = readable_event(db, slug, principal)
    require_access(principal, event, Action.REGISTER)

    registration = EventRegistration(
        event_id=event.id,
        user_id=user.id,
        email=str(payload.email) if payload.email else user.email,
        discord_username=payload.discord_username.strip(),
        is_team_leader=payload.is_team_leader,
        leader_name=payload.leader_name,
    )
    db.add(registration)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="You are already registered for this event",
        ) from None

    leader_note = (
        f" (intending to lead as {quote(registration.leader_name)})"
        if registration.is_team_leader
        else ""
    )
    record(
        db,
        action=AuditAction.EVENT_REGISTERED,
        summary=f"{user.email} registered for {event.slug}{leader_note}",
        principal=principal,
        event=event,
        resource_type="event_registration",
        resource_id=registration.id,
        request=request,
    )
    db.commit()
    db.refresh(registration)

    # The webhook exists independent of this project's own email sending, so
    # an organizer's own integration (a Discord bot posting a welcome
    # message, a spreadsheet sync) can react to a signup either way.
    schedule(
        background,
        db,
        event.id,
        WebhookEvent.REGISTRATION_CREATED,
        {
            "event": event.slug,
            "user_id": str(user.id),
            "email": registration.email,
            "discord_username": registration.discord_username,
            "is_team_leader": registration.is_team_leader,
        },
    )

    schedule_email(
        background,
        to=registration.email,
        subject=f"You're registered for {event.name}",
        body=(
            f"Hi {user.display_name},\n\n"
            f"You're registered for {event.name}, using the Discord handle "
            f"{registration.discord_username}.\n\n"
            + (
                f"You told us you're planning to lead a team as "
                f"{registration.leader_name}.\n\n"
                if registration.is_team_leader
                else ""
            )
            + f"See you there.\n\n{settings.web_base_url.rstrip('/')}/events/{event.slug}"
        ),
    )

    registration.user = user  # avoid a re-query; we already have it
    return _registration_out(db, registration)


@router.get("/{slug}/registrations/me", response_model=RegistrationOut | None)
def my_registration(
    slug: str,
    principal: Principal = Depends(get_current_principal),
    db: Session = Depends(get_db),
) -> RegistrationOut | None:
    """Whether -- and how -- the caller is registered for this event."""
    user = require_authenticated(principal)
    event = readable_event(db, slug, principal)
    row = db.execute(
        select(EventRegistration)
        .options(selectinload(EventRegistration.user))
        .where(EventRegistration.event_id == event.id, EventRegistration.user_id == user.id)
    ).scalar_one_or_none()
    return _registration_out(db, row) if row is not None else None


@router.get("/{slug}/registrations", response_model=Page[RegistrationOut])
def list_registrations(
    slug: str,
    principal: Principal = Depends(get_current_principal),
    db: Session = Depends(get_db),
    pagination: PageParams = Depends(),
) -> Page[RegistrationOut]:
    """The event's roster. Staff-only, same as every other participant-list
    view in this codebase (`list_teams`, `list_users`, ...)."""
    event = manageable_event(db, slug, principal)
    stmt = select(EventRegistration).where(EventRegistration.event_id == event.id)
    total = count_of(db, stmt)
    rows = (
        db.execute(
            stmt.options(selectinload(EventRegistration.user))
            .order_by(EventRegistration.registered_at)
            .offset(pagination.offset)
            .limit(pagination.per_page)
        )
        .scalars()
        .all()
    )
    return Page(
        items=[_registration_out(db, r) for r in rows],
        total=total,
        page=pagination.page,
        per_page=pagination.per_page,
        pages=pages_for(total, pagination.per_page),
    )


@router.post(
    "/{slug}/registrations/{registration_id}/remove", status_code=status.HTTP_204_NO_CONTENT
)
def remove_registration(
    slug: str,
    registration_id: uuid.UUID,
    payload: RegistrationRemovalIn,
    request: Request,
    principal: Principal = Depends(get_current_principal),
    db: Session = Depends(get_db),
) -> None:
    """Staff removing a registration row, for cause -- a mistaken signup, a
    duplicate, a spam entry. Unlike a judge or a team member, nothing else in
    the schema references a registration (no ballots, no submission), so this
    is a real, audited hard delete rather than a status flip -- there is
    nothing here that later needs to be recovered or shown as "removed"
    alongside surviving data the way a disqualified submission's ballots are.
    """
    event = manageable_event(db, slug, principal)
    row = db.execute(
        select(EventRegistration)
        .options(selectinload(EventRegistration.user))
        .where(EventRegistration.id == registration_id, EventRegistration.event_id == event.id)
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Registration not found")

    record(
        db,
        action=AuditAction.REGISTRATION_REMOVED,
        summary=(
            f"{principal.email} removed {row.user.email}'s registration for "
            f"{event.slug}: {quote(payload.reason)}"
        ),
        principal=principal,
        event=event,
        resource_type="event_registration",
        resource_id=row.id,
        request=request,
    )
    db.delete(row)
    db.commit()
