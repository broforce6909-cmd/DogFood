"""Teams and invite links.

Team formation is by invite link, so the token *is* the credential. Three
consequences follow, and all three are implemented rather than assumed:

* it is generated with `secrets`, not from the team id or name;
* it is behind its own endpoint and its own `Action`, so reading it is a
  separately-authorized act rather than a field somebody forgets to strip;
* it is rotatable, so a link pasted into a public Discord can be killed without
  deleting the team.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from ..access import Action, Principal, check_access, require_access, require_authenticated
from ..audit import quote, record
from ..config import settings
from ..db import get_db
from ..deps import get_current_principal
from ..hooks import schedule
from ..models import (
    AuditAction,
    Role,
    SubmissionStatus,
    Team,
    TeamMember,
    TeamRole,
    User,
    WebhookEvent,
    new_token,
)
from ..pagination import Page, PageParams, count_of, pages_for
from ..schemas import InviteOut, JoinIn, TeamCreate, TeamMemberRemovalIn, TeamOut, TeamUpdate
from ..serializers import team_out
from .events import readable_event

router = APIRouter(prefix="/api", tags=["teams"])

_TEAM_LOADERS = (
    selectinload(Team.members).selectinload(TeamMember.user),
    selectinload(Team.event),
    selectinload(Team.submission),
)


def load_team(db: Session, team_id: uuid.UUID) -> Team:
    team = db.execute(
        select(Team).options(*_TEAM_LOADERS).where(Team.id == team_id)
    ).scalar_one_or_none()
    if team is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Team not found")
    return team


def readable_team(db: Session, team_id: uuid.UUID, user: Principal) -> Team:
    team = load_team(db, team_id)
    require_access(user, team, Action.READ, status_code=status.HTTP_404_NOT_FOUND)
    return team


def invite_url(token: str) -> str:
    return f"{settings.web_base_url.rstrip('/')}/join/{token}"


# --------------------------------------------------------------------------- #
# Within an event
# --------------------------------------------------------------------------- #


@router.get("/events/{slug}/teams", response_model=Page[TeamOut])
def list_teams(
    slug: str,
    principal: Principal = Depends(get_current_principal),
    db: Session = Depends(get_db),
    pagination: PageParams = Depends(),
) -> Page[TeamOut]:
    """The event's roster, paged. `GET .../teams/mine` is the dedicated route
    for "is this caller already on a team" -- a participant's own membership
    must not depend on which page of a 200-team event happens to include them.
    """
    event = readable_event(db, slug, principal)
    stmt = select(Team).where(Team.event_id == event.id)
    total = count_of(db, stmt)
    rows = (
        db.execute(
            stmt.options(*_TEAM_LOADERS)
            .order_by(Team.name)
            .offset(pagination.offset)
            .limit(pagination.per_page)
        )
        .scalars()
        .all()
    )
    return Page(
        items=[team_out(t) for t in rows if check_access(principal, t, Action.READ)],
        total=total,
        page=pagination.page,
        per_page=pagination.per_page,
        pages=pages_for(total, pagination.per_page),
    )


@router.get("/events/{slug}/teams/mine", response_model=TeamOut | None)
def my_team(
    slug: str,
    principal: Principal = Depends(get_current_principal),
    db: Session = Depends(get_db),
) -> TeamOut | None:
    """The caller's own team in this event, or `null` -- one row, regardless of
    how many teams the event has. Added alongside pagination on the list above:
    "am I on a team" was previously answered by fetching every team and
    scanning for membership client-side, which a page boundary would have
    silently broken for a participant on team 51 of a 200-team event."""
    event = readable_event(db, slug, principal)
    if not isinstance(principal, User):
        return None
    team = db.execute(
        select(Team)
        .options(*_TEAM_LOADERS)
        .join(TeamMember, TeamMember.team_id == Team.id)
        .where(Team.event_id == event.id, TeamMember.user_id == principal.id)
    ).scalar_one_or_none()
    return team_out(team) if team else None


@router.post(
    "/events/{slug}/teams", response_model=TeamOut, status_code=status.HTTP_201_CREATED
)
def create_team(
    slug: str,
    payload: TeamCreate,
    request: Request,
    background: BackgroundTasks,
    principal: Principal = Depends(get_current_principal),
    db: Session = Depends(get_db),
) -> TeamOut:
    """The creator becomes the owner. Owning a team is a relationship, not a
    role -- a participant owns their team and has no elevated global rank."""
    user = require_authenticated(principal)
    event = readable_event(db, slug, principal)
    require_access(principal, event, Action.CREATE_TEAM)

    team = Team(event_id=event.id, name=payload.name.strip(), created_by_id=user.id)
    team.members.append(
        TeamMember(user_id=user.id, event_id=event.id, team_role=TeamRole.OWNER)
    )
    db.add(team)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        # Either the name collides, or `uq_team_members_one_team_per_event` just
        # stopped this user being on two teams in one event. Both are 409s.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="That team name is taken, or you are already on a team in this event",
        ) from None
    record(
        db,
        action=AuditAction.TEAM_CREATED,
        summary=f"{user.email} created team {quote(team.name)} on {event.slug}",
        principal=user,
        event=event,
        resource_type="team",
        resource_id=team.id,
        request=request,
    )
    db.commit()

    # `team.created` was a documented, subscribable webhook topic that nothing
    # ever scheduled -- the same class of gap as `submission.updated`, found in
    # the same pass. See update_submission()'s comment for why it matters that
    # a subscribed-but-silent topic is worse than no topic at all.
    schedule(
        background,
        db,
        event.id,
        WebhookEvent.TEAM_CREATED,
        {
            "event": event.slug,
            "team_id": str(team.id),
            "name": team.name,
            "created_by": user.email,
        },
    )
    return team_out(load_team(db, team.id))


# --------------------------------------------------------------------------- #
# A single team
# --------------------------------------------------------------------------- #


@router.get("/teams/{team_id}", response_model=TeamOut)
def get_team(
    team_id: uuid.UUID,
    principal: Principal = Depends(get_current_principal),
    db: Session = Depends(get_db),
) -> TeamOut:
    return team_out(readable_team(db, team_id, principal))


@router.patch("/teams/{team_id}", response_model=TeamOut)
def update_team(
    team_id: uuid.UUID,
    payload: TeamUpdate,
    request: Request,
    principal: Principal = Depends(get_current_principal),
    db: Session = Depends(get_db),
) -> TeamOut:
    team = readable_team(db, team_id, principal)
    require_access(principal, team, Action.UPDATE)
    old_name = team.name
    if payload.name is not None:
        team.name = payload.name.strip()
    if payload.name is not None and payload.name.strip() != old_name:
        record(
            db,
            action=AuditAction.TEAM_CHANGED,
            summary=(
                f"{principal.email} renamed team {quote(old_name)} to "
                f"{quote(team.name)} on {team.event.slug}"
            ),
            principal=principal,
            event=team.event,
            resource_type="team",
            resource_id=team.id,
            request=request,
        )
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="That team name is taken"
        ) from None
    return team_out(load_team(db, team.id))


@router.delete("/teams/{team_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_team(
    team_id: uuid.UUID,
    request: Request,
    principal: Principal = Depends(get_current_principal),
    db: Session = Depends(get_db),
) -> None:
    """Deletes the team's submission with it. The cascade is declared on the
    foreign key, so it happens in one transaction or not at all."""
    team = readable_team(db, team_id, principal)
    require_access(principal, team, Action.DELETE)
    record(
        db,
        action=AuditAction.TEAM_DELETED,
        summary=(
            f"{principal.email} deleted team {quote(team.name)} on "
            f"{team.event.slug} (its submission, if any, went with it)"
        ),
        principal=principal,
        event=team.event,
        resource_type="team",
        resource_id=team.id,
        request=request,
    )
    db.delete(team)
    db.commit()


# --------------------------------------------------------------------------- #
# Invites
# --------------------------------------------------------------------------- #


@router.get("/teams/{team_id}/invite", response_model=InviteOut)
def get_invite(
    team_id: uuid.UUID,
    principal: Principal = Depends(get_current_principal),
    db: Session = Depends(get_db),
) -> InviteOut:
    team = readable_team(db, team_id, principal)
    require_access(principal, team, Action.READ_INVITE)
    return InviteOut(token=team.invite_token, url=invite_url(team.invite_token))


@router.post("/teams/{team_id}/invite/rotate", response_model=InviteOut)
def rotate_invite(
    team_id: uuid.UUID,
    request: Request,
    principal: Principal = Depends(get_current_principal),
    db: Session = Depends(get_db),
) -> InviteOut:
    """Owner-only, because it invalidates a link other members may have shared."""
    team = readable_team(db, team_id, principal)
    require_access(principal, team, Action.MANAGE)
    team.invite_token = new_token()
    record(
        db,
        action=AuditAction.INVITE_ROTATED,
        summary=(
            f"{principal.email} rotated the invite link for team "
            f"{quote(team.name)} on {team.event.slug}"
        ),
        principal=principal,
        event=team.event,
        resource_type="team",
        resource_id=team.id,
        request=request,
    )
    db.commit()
    return InviteOut(token=team.invite_token, url=invite_url(team.invite_token))


@router.post("/teams/join", response_model=TeamOut)
def join_team(
    payload: JoinIn,
    request: Request,
    principal: Principal = Depends(get_current_principal),
    db: Session = Depends(get_db),
) -> TeamOut:
    """Join by invite token.

    An unknown token is a 404 whether it is wrong or merely rotated; the
    difference is not something a stranger is entitled to learn.
    """
    user = require_authenticated(principal)
    team = db.execute(
        select(Team).options(*_TEAM_LOADERS).where(Team.invite_token == payload.token)
    ).scalar_one_or_none()
    if team is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="That invite link is not valid"
        )

    if user.id in team.member_ids():
        return team_out(team)  # joining twice is a no-op, not an error

    require_access(principal, team, Action.JOIN_TEAM)

    if len(team.members) >= team.event.max_team_size:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"This team is full ({team.event.max_team_size} members)",
        )

    db.add(
        TeamMember(
            team_id=team.id,
            user_id=user.id,
            event_id=team.event_id,
            team_role=TeamRole.MEMBER,
        )
    )
    record(
        db,
        action=AuditAction.TEAM_JOINED,
        summary=f"{user.email} joined team {quote(team.name)} on {team.event.slug}",
        principal=user,
        event=team.event,
        resource_type="team",
        resource_id=team.id,
        request=request,
    )
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="You are already on a team in this event",
        ) from None
    return team_out(load_team(db, team.id))


@router.delete(
    "/teams/{team_id}/members/{user_id}", status_code=status.HTTP_204_NO_CONTENT
)
def remove_member(
    team_id: uuid.UUID,
    user_id: uuid.UUID,
    request: Request,
    principal: Principal = Depends(get_current_principal),
    db: Session = Depends(get_db),
) -> None:
    """Leaving is removing yourself; removing somebody else needs the owner.

    The last owner cannot leave a team that still has members -- an ownerless
    team is a team nobody can rename, invite to, or delete.
    """
    user = require_authenticated(principal)
    team = readable_team(db, team_id, principal)

    leaving_self = user.id == user_id
    if not leaving_self:
        require_access(principal, team, Action.MANAGE)
        # Staff intervening in a roster they do not own is exactly the kind of
        # action a team will ask about later, so it needs a reason -- which
        # this route has no field for, on purpose, to keep the plain
        # self-leave/owner-manages-their-own-team path exactly as
        # justification-free as it always was. `POST .../remove` is that
        # reasoned path.
        if user.role.at_least(Role.ORGANIZER) and user.id not in team.owner_ids():
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Staff removal requires a reason -- use POST "
                f"/api/teams/{team_id}/members/{user_id}/remove instead",
            )

    member = next((m for m in team.members if m.user_id == user_id), None)
    if member is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not a member")

    if member.team_role is TeamRole.OWNER and len(team.owner_ids()) == 1 and len(team.members) > 1:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Promote another owner before leaving",
        )

    record(
        db,
        action=AuditAction.TEAM_MEMBER_REMOVED,
        summary=(
            f"{user.email} left team {quote(team.name)}"
            if leaving_self
            else (
                f"{user.email} removed {member.user.email} from team "
                f"{quote(team.name)}"
            )
        )
        + f" on {team.event.slug}",
        principal=user,
        event=team.event,
        resource_type="team",
        resource_id=team.id,
        request=request,
    )
    db.delete(member)
    db.commit()


@router.post(
    "/teams/{team_id}/members/{user_id}/remove", status_code=status.HTTP_204_NO_CONTENT
)
def remove_member_for_cause(
    team_id: uuid.UUID,
    user_id: uuid.UUID,
    payload: TeamMemberRemovalIn,
    request: Request,
    principal: Principal = Depends(get_current_principal),
    db: Session = Depends(get_db),
) -> None:
    """Staff removing a participant, for cause. Distinct from the plain
    `DELETE` above, which is for a member leaving on their own or an owner
    managing their own team's roster -- neither needs a justification, and
    the `DELETE` route refuses a staff caller who is not the team's own
    owner specifically so they end up here instead. Mirrors `remove_judge`'s
    own POST-with-reason redesign: staff intervening in a team's membership
    is exactly the kind of action a team will ask about later.

    If this was the team's only member, the team is not deleted -- a team is
    never hard-deleted just because its roster is momentarily empty, the same
    as the plain self-leave path already allows -- and neither is its
    submission, if it has one: it is disqualified instead, the same
    soft-removal `POST /submissions/{id}/disqualify` performs directly, for
    the same reason. Nobody is left to answer for an entry nobody owns.
    """
    if not principal.role.at_least(Role.ORGANIZER):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Staff only")
    user = require_authenticated(principal)
    team = readable_team(db, team_id, principal)

    member = next((m for m in team.members if m.user_id == user_id), None)
    if member is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not a member")

    if member.team_role is TeamRole.OWNER and len(team.owner_ids()) == 1 and len(team.members) > 1:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Promote another owner before removing this member",
        )

    was_solo = len(team.members) == 1
    removed_email = member.user.email

    record(
        db,
        action=AuditAction.TEAM_MEMBER_REMOVED,
        summary=(
            f"{user.email} removed {removed_email} from team {quote(team.name)} "
            f"on {team.event.slug}: {quote(payload.reason)}"
        ),
        principal=user,
        event=team.event,
        resource_type="team",
        resource_id=team.id,
        request=request,
    )
    db.delete(member)
    db.flush()

    if (
        was_solo
        and team.submission is not None
        and team.submission.status is not SubmissionStatus.DISQUALIFIED
    ):
        team.submission.status = SubmissionStatus.DISQUALIFIED
        record(
            db,
            action=AuditAction.SUBMISSION_DISQUALIFIED,
            summary=(
                f"{user.email} disqualified {quote(team.submission.name)} on "
                f"{team.event.slug}: its only member, {removed_email}, was "
                f"removed ({quote(payload.reason)})"
            ),
            principal=user,
            event=team.event,
            resource_type="submission",
            resource_id=team.submission.id,
            request=request,
        )

    db.commit()
