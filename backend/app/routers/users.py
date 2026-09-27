"""User administration.

The role model is only real if somebody can move a person between roles, and
only safe if that somebody is exactly one kind of person. Promotion is admin-
only: an organizer who could mint organizers is an admin with extra steps.

Self-registration produces a `participant` and nothing else, so this router is
the only path to `judge`, `organizer` or `admin` -- two ways in, both
admin-only: promote an existing account (`PATCH /{id}/role`), or provision a
brand new one directly (`POST /`, Phase 6) for someone who should never see
the public registration form in the first place, with a generated password
emailed to them rather than typed by the admin.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request, status
from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..access import PLATFORM, Action, Principal, require_access
from ..audit import record
from ..config import settings
from ..db import get_db
from ..deps import get_current_principal
from ..email import schedule_email
from ..models import AdminLevel, AuditAction, Role, User
from ..pagination import Page, PageParams, count_of, pages_for
from ..schemas import RoleUpdateIn, UserCreateIn, UserOut
from ..security import generate_password, hash_password, normalize_email

router = APIRouter(prefix="/api/users", tags=["users"])


def _load(db: Session, user_id: uuid.UUID) -> User:
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    return user


def _resolve_level(
    role: Role, requested: AdminLevel | None, current: User | None
) -> AdminLevel | None:
    """The admin level an account should hold after this change.

    None for a non-admin role (the stored column is then irrelevant). For an
    admin: what was asked for; else the level they already hold; else
    `MANAGER` for a fresh promotion -- least privilege that can still write.
    Asking for a level on a non-admin role is refused rather than ignored: a
    caller who thinks they set one should be told they did not.
    """
    if role is not Role.ADMIN:
        if requested is not None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="admin_level only applies to the admin role",
            )
        return None
    if requested is not None:
        return requested
    if current is not None and current.role is Role.ADMIN:
        return current.admin_level
    return AdminLevel.MANAGER


def _other_active_owners(db: Session, user: User) -> int:
    return len(
        db.execute(
            select(User.id).where(
                User.role == Role.ADMIN,
                User.admin_level == AdminLevel.OWNER,
                User.is_active.is_(True),
                User.id != user.id,
            )
        ).all()
    )


@router.get("", response_model=Page[UserOut])
def list_users(
    q: str | None = Query(default=None, description="Match on email or display name"),
    role: Role | None = Query(default=None),
    principal: Principal = Depends(get_current_principal),
    db: Session = Depends(get_db),
    pagination: PageParams = Depends(),
) -> Page[UserOut]:
    """Staff only, paged. Every row here carries an email address, which is why
    `check_access` is consulted before the query runs rather than after."""
    require_access(principal, PLATFORM, Action.LIST_USERS)

    stmt = select(User)
    if role is not None:
        stmt = stmt.where(User.role == role)
    if q:
        pattern = f"%{q.strip()}%"
        stmt = stmt.where(or_(User.email.ilike(pattern), User.display_name.ilike(pattern)))

    total = count_of(db, stmt)
    rows = db.execute(
        stmt.order_by(User.created_at).offset(pagination.offset).limit(pagination.per_page)
    ).scalars()
    return Page(
        items=[UserOut.model_validate(u) for u in rows],
        total=total,
        page=pagination.page,
        per_page=pagination.per_page,
        pages=pages_for(total, pagination.per_page),
    )


@router.post("", response_model=UserOut, status_code=status.HTTP_201_CREATED)
def create_user(
    payload: UserCreateIn,
    request: Request,
    background: BackgroundTasks,
    principal: Principal = Depends(get_current_principal),
    db: Session = Depends(get_db),
) -> UserOut:
    """Admin-provisioned account: a judge, organizer or another admin,
    created directly with no self-registration step at all.

    This is the one path to those roles that does not require the person to
    have an account already -- `PATCH /{user_id}/role` below still needs an
    existing row, which self-registration (`POST /auth/register`, always a
    `participant`) is otherwise the only way to get. A generated password is
    emailed to the new account and never returned by this endpoint or
    written to the audit log: the email is the one place it is ever visible
    in the clear, the same discipline every self-chosen password already
    gets via `hash_password`.
    """
    require_access(principal, PLATFORM, Action.CREATE_USER)
    if payload.role is Role.VISITOR:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="visitor is the anonymous role and cannot be assigned to an account",
        )

    level = _resolve_level(payload.role, payload.admin_level, None)
    email = normalize_email(payload.email)
    password = generate_password()
    user = User(
        email=email,
        display_name=payload.display_name.strip(),
        password_hash=hash_password(password),
        role=payload.role,
        admin_level=level or AdminLevel.OWNER,
    )
    db.add(user)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="That email is already registered"
        ) from None

    record(
        db,
        action=AuditAction.USER_CREATED,
        summary=(
            f"{principal.email} created a {payload.role.value} account for {email}"
            + (f" (level: {level.value})" if level else "")
        ),
        principal=principal,
        resource_type="user",
        resource_id=user.id,
        request=request,
    )
    db.commit()
    db.refresh(user)

    schedule_email(
        background,
        to=email,
        subject="Your Dogfood Hackathon Portal account",
        body=(
            f"Hi {user.display_name},\n\n"
            f"An admin has set up a {payload.role.value} account for you on "
            "the Dogfood Hackathon Portal.\n\n"
            f"Email: {email}\n"
            f"Temporary password: {password}\n\n"
            f"Sign in at {settings.web_base_url.rstrip('/')}/login and change "
            "your password once you're in (Account settings)."
        ),
    )
    return UserOut.model_validate(user)


@router.get("/{user_id}", response_model=UserOut)
def get_user(
    user_id: uuid.UUID,
    principal: Principal = Depends(get_current_principal),
    db: Session = Depends(get_db),
) -> UserOut:
    user = _load(db, user_id)
    require_access(principal, user, Action.READ)
    return UserOut.model_validate(user)


@router.patch("/{user_id}/role", response_model=UserOut)
def set_role(
    user_id: uuid.UUID,
    payload: RoleUpdateIn,
    request: Request,
    principal: Principal = Depends(get_current_principal),
    db: Session = Depends(get_db),
) -> UserOut:
    """Admin only.

    `visitor` is the role of a request with no session; assigning it to an
    account would create a logged-in user with less access than a stranger, so
    it is refused rather than quietly accepted.
    """
    user = _load(db, user_id)
    require_access(principal, user, Action.MANAGE)
    if payload.role is Role.VISITOR:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="visitor is the anonymous role and cannot be assigned to an account",
        )
    old_role = user.role
    old_level = user.admin_level if old_role is Role.ADMIN else None
    new_level = _resolve_level(payload.role, payload.admin_level, user)

    # The last owner cannot demote themselves: nobody would be left who may
    # administer accounts, and only a database client could fix it.
    was_owner = old_level is AdminLevel.OWNER
    stays_owner = payload.role is Role.ADMIN and new_level is AdminLevel.OWNER
    if was_owner and not stays_owner and _other_active_owners(db, user) == 0:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This is the last owner-level admin; make another admin an owner first",
        )

    user.role = payload.role
    if new_level is not None:
        user.admin_level = new_level
    if old_role is not payload.role or old_level is not new_level:
        # No event: a role is a global grant, not something one organizer's
        # event log should claim ownership of. Visible at GET /api/audit,
        # admin-only, which is exactly the audience for "who can do what."
        record(
            db,
            action=AuditAction.ROLE_CHANGED,
            summary=(
                f"{principal.email} changed {user.email}'s role from "
                f"{old_role.value}{f' ({old_level.value})' if old_level else ''} to "
                f"{payload.role.value}{f' ({new_level.value})' if new_level else ''}"
            ),
            principal=principal,
            resource_type="user",
            resource_id=user.id,
            request=request,
        )
    db.commit()
    db.refresh(user)
    return UserOut.model_validate(user)


@router.post("/{user_id}/deactivate", response_model=UserOut)
def deactivate(
    user_id: uuid.UUID,
    request: Request,
    principal: Principal = Depends(get_current_principal),
    db: Session = Depends(get_db),
) -> UserOut:
    """Deactivation is not a lesser role, it is no account: `check_access`
    refuses everything for an inactive user, and their live sessions are deleted
    here rather than left to expire."""
    user = _load(db, user_id)
    require_access(principal, user, Action.MANAGE)
    if user.id == getattr(principal, "id", None):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="You cannot deactivate yourself"
        )
    if (
        user.role is Role.ADMIN
        and user.admin_level is AdminLevel.OWNER
        and _other_active_owners(db, user) == 0
    ):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This is the last owner-level admin and cannot be deactivated",
        )
    user.is_active = False
    for row in list(user.sessions):
        db.delete(row)
    record(
        db,
        action=AuditAction.ACCOUNT_DEACTIVATED,
        summary=f"{principal.email} deactivated {user.email} (every live session revoked)",
        principal=principal,
        resource_type="user",
        resource_id=user.id,
        request=request,
    )
    db.commit()
    db.refresh(user)
    return UserOut.model_validate(user)


@router.post("/{user_id}/reactivate", response_model=UserOut)
def reactivate(
    user_id: uuid.UUID,
    request: Request,
    principal: Principal = Depends(get_current_principal),
    db: Session = Depends(get_db),
) -> UserOut:
    user = _load(db, user_id)
    require_access(principal, user, Action.MANAGE)
    user.is_active = True
    record(
        db,
        action=AuditAction.ACCOUNT_REACTIVATED,
        summary=f"{principal.email} reactivated {user.email}",
        principal=principal,
        resource_type="user",
        resource_id=user.id,
        request=request,
    )
    db.commit()
    db.refresh(user)
    return UserOut.model_validate(user)
