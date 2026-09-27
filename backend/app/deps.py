"""Request-scoped dependencies: the database session and the caller's identity.

Resolving *who* is asking happens here; deciding *what* they may do happens in
`app.access`. Keeping those apart is why there is exactly one place to audit
authorization.
"""

from __future__ import annotations

from datetime import timedelta

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from .access import ANONYMOUS, Principal
from .config import settings
from .db import get_db
from .models import AdminLevel, Role, User, UserSession, utcnow
from .security import hash_session_token

# HTTP methods that, by contract, do not change anything. A read-only admin
# (`AdminLevel.AUDITOR`) may use these anywhere and nothing else.
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

# The one place an auditor may still write: their own session and profile
# (sign out, change their own password). Everything else is refused.
AUDITOR_WRITABLE_PREFIX = "/api/auth/"

# Writing `last_seen_at` on every single request is a write amplification we do
# not need; this is enough resolution to spot a stale session in the UI.
LAST_SEEN_RESOLUTION = timedelta(minutes=5)


def bearer_token(request: Request) -> str | None:
    """`Authorization: Bearer <token>`.

    Supported alongside the cookie so that the API is usable from curl and from
    the acceptance suite without a cookie jar -- "every UI action is an API
    action" is not true if the API is only reachable from a browser.
    """
    header = request.headers.get("authorization")
    if not header:
        return None
    scheme, _, value = header.partition(" ")
    if scheme.lower() != "bearer" or not value.strip():
        return None
    return value.strip()


def session_token(request: Request) -> str | None:
    return bearer_token(request) or request.cookies.get(settings.session_cookie_name)


def get_current_principal(
    request: Request, db: Session = Depends(get_db)
) -> Principal:
    """The caller. "Not logged in" is a role, not an error, so this does not
    raise for it.

    Routes that require an account call `require_authenticated()`; routes that
    are merely restricted let `check_access()` decide, so a visitor hitting a
    public gallery is handled by the same path as everybody else.

    The one thing this does raise is for a read-only admin (`AUDITOR`) making an
    unsafe request. That is deliberately here and not left to each route:
    `check_access()` holds an auditor to read-only verbs, but a route that
    forgot to call it -- or a future one -- would still be open. Refusing the
    method here means "read-only" does not depend on every handler remembering.
    """
    token = session_token(request)
    if not token:
        return ANONYMOUS

    stmt = (
        select(UserSession)
        .options(selectinload(UserSession.user))
        .where(UserSession.token_hash == hash_session_token(token))
    )
    session_row = db.execute(stmt).scalar_one_or_none()
    if session_row is None:
        return ANONYMOUS

    now = utcnow()
    if not session_row.is_live(now):
        # Expired sessions are deleted on sight rather than swept on a timer.
        db.delete(session_row)
        db.commit()
        return ANONYMOUS

    user = session_row.user
    if user is None or not user.is_active:
        return ANONYMOUS

    if (
        user.role is Role.ADMIN
        and user.admin_level is AdminLevel.AUDITOR
        and request.method not in SAFE_METHODS
        and not request.url.path.startswith(AUDITOR_WRITABLE_PREFIX)
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This admin account is read-only (auditor level)",
        )

    if now - session_row.last_seen_at > LAST_SEEN_RESOLUTION:
        session_row.last_seen_at = now
        db.commit()

    return user


def client_ip(request: Request) -> str | None:
    """Best effort, and only recorded on the session row for an organizer's
    benefit. Not used for any access decision -- it is trivially spoofable."""
    if request.client is None:
        return None
    return request.client.host


__all__ = [
    "get_db",
    "get_current_principal",
    "session_token",
    "bearer_token",
    "client_ip",
    "User",
]
