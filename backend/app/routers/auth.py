"""Registration, login, logout, and the sessions a user can see and revoke.

Sessions are opaque server-side rows. The cookie carries a random token; the
table stores an HMAC of it. That makes logout a `DELETE`, makes "log out
everywhere" a `DELETE ... WHERE user_id = ...`, and makes a leaked database dump
useless for impersonation.
"""

from __future__ import annotations

import uuid
from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .. import ratelimit
from ..access import ANONYMOUS, Principal, require_authenticated
from ..config import settings
from ..db import get_db
from ..deps import client_ip, get_current_principal, session_token
from ..models import Role, User, UserSession, utcnow
from ..schemas import (
    LoginIn,
    MeOut,
    ProfileUpdateIn,
    RegisterIn,
    SessionOut,
    SessionRowOut,
    UserOut,
)
from ..security import (
    hash_password,
    hash_session_token,
    needs_rehash,
    new_session_token,
    normalize_email,
    verify_password,
)

router = APIRouter(prefix="/api/auth", tags=["auth"])


def _with_article(role: Role) -> str:
    """"an Admin" / "a Participant" -- the tab labels, with the right article."""
    label = role.value.capitalize()
    return f"{'an' if label[0] in 'AEIOU' else 'a'} {label}"


def _issue_session(db: Session, user: User, request: Request, response: Response) -> SessionOut:
    token = new_session_token()
    expires_at = utcnow() + timedelta(hours=settings.session_ttl_hours)
    row = UserSession(
        user_id=user.id,
        token_hash=hash_session_token(token),
        expires_at=expires_at,
        user_agent=(request.headers.get("user-agent") or "")[:400] or None,
        ip_address=client_ip(request),
    )
    db.add(row)
    db.commit()

    response.set_cookie(
        key=settings.session_cookie_name,
        value=token,
        max_age=settings.session_ttl_hours * 3600,
        httponly=True,
        samesite="lax",
        secure=settings.session_cookie_secure,
        path="/",
    )
    return SessionOut(token=token, expires_at=expires_at, user=UserOut.model_validate(user))


@router.post("/register", response_model=SessionOut, status_code=status.HTTP_201_CREATED)
def register(
    payload: RegisterIn,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
) -> SessionOut:
    """Self-registration always produces a `participant`.

    Judges, organizers and admins are promoted by an admin. A platform where
    picking your own role at signup works is not a platform with roles.
    """
    ratelimit.enforce(db, ratelimit.REGISTER, ratelimit.client_key(request), request=request)

    email = normalize_email(payload.email)
    user = User(
        email=email,
        display_name=payload.display_name.strip(),
        password_hash=hash_password(payload.password),
        role=Role.PARTICIPANT,
    )
    db.add(user)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="That email is already registered"
        ) from None
    db.commit()
    db.refresh(user)
    return _issue_session(db, user, request, response)


@router.post("/login", response_model=SessionOut)
def login(
    payload: LoginIn,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
) -> SessionOut:
    """One error message for every failure mode, so this endpoint cannot be used
    to enumerate which addresses have accounts.

    Two rate limits, keyed differently, checked before a single credential is
    compared: `LOGIN_IP` catches a script trying many accounts from one address,
    and `LOGIN_ACCOUNT` catches a distributed attack -- many addresses, one
    account -- that `LOGIN_IP` alone would never notice because each attacking
    address looks unremarkable on its own. Both are counted on every attempt,
    successful or not, so the limiter never has to know whether the password was
    right before deciding whether to answer.
    """
    email = normalize_email(payload.email)
    ratelimit.enforce(db, ratelimit.LOGIN_IP, ratelimit.client_key(request), request=request)
    ratelimit.enforce(db, ratelimit.LOGIN_ACCOUNT, f"account:{email}", request=request)

    user = db.execute(select(User).where(User.email == email)).scalar_one_or_none()

    if user is None or not verify_password(user.password_hash, payload.password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Incorrect email or password"
        )
    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="This account has been deactivated"
        )

    # Only reachable with a correct password, so naming the account's real role
    # here tells the caller nothing they could not learn by signing in -- and it
    # is placed before the rehash and before `_issue_session`, so a refused tab
    # leaves no session row, no cookie and no other trace on the account.
    if payload.expected_role is not None and payload.expected_role is not user.role:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "message": (
                    f"This account is {_with_article(user.role)} account, not "
                    f"{_with_article(payload.expected_role)} one — "
                    f"try the {user.role.value.capitalize()} tab."
                ),
                "code": "wrong_role_tab",
                "actual_role": user.role.value,
            },
        )

    if needs_rehash(user.password_hash):
        user.password_hash = hash_password(payload.password)
        db.commit()

    return _issue_session(db, user, request, response)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(request: Request, response: Response, db: Session = Depends(get_db)) -> Response:
    """Revocation is a row delete. Nothing to expire, nothing to blacklist."""
    token = session_token(request)
    if token:
        row = db.execute(
            select(UserSession).where(UserSession.token_hash == hash_session_token(token))
        ).scalar_one_or_none()
        if row is not None:
            db.delete(row)
            db.commit()

    response.delete_cookie(settings.session_cookie_name, path="/")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/me", response_model=MeOut)
def me(principal: Principal = Depends(get_current_principal)) -> MeOut:
    """Answers 200 for a visitor too. "Nobody" is a valid answer to "who am I"."""
    if principal is ANONYMOUS or not isinstance(principal, User):
        return MeOut(authenticated=False, user=None, role=Role.VISITOR)
    return MeOut(
        authenticated=True, user=UserOut.model_validate(principal), role=principal.role
    )


@router.patch("/me", response_model=UserOut)
def update_me(
    payload: ProfileUpdateIn,
    principal: Principal = Depends(get_current_principal),
    db: Session = Depends(get_db),
) -> UserOut:
    """A user may change their own name and password. Not their own role."""
    user = require_authenticated(principal)
    if payload.display_name is not None:
        user.display_name = payload.display_name.strip()
    if payload.password is not None:
        user.password_hash = hash_password(payload.password)
        # Changing a password invalidates every other session, which is the
        # behaviour people expect and the reason sessions are rows.
        for row in list(user.sessions):
            db.delete(row)
    db.commit()
    db.refresh(user)
    return UserOut.model_validate(user)


@router.get("/sessions", response_model=list[SessionRowOut])
def list_sessions(
    principal: Principal = Depends(get_current_principal),
    db: Session = Depends(get_db),
) -> list[SessionRowOut]:
    user = require_authenticated(principal)
    rows = db.execute(
        select(UserSession)
        .where(UserSession.user_id == user.id)
        .order_by(UserSession.last_seen_at.desc())
    ).scalars()
    return [SessionRowOut.model_validate(row) for row in rows]


@router.delete("/sessions/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
def revoke_session(
    session_id: uuid.UUID,
    principal: Principal = Depends(get_current_principal),
    db: Session = Depends(get_db),
) -> Response:
    user = require_authenticated(principal)
    row = db.get(UserSession, session_id)
    # Not `require_access`: a session is not a resource anybody but its owner
    # can name, and a 404 says less than a 403 would.
    if row is None or row.user_id != user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    db.delete(row)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
