"""Passwords and session tokens.

Auth-as-a-service is out of scope for this brief, so this is the whole of it:
Argon2id for passwords, an opaque random token for sessions, and an HMAC of that
token in the database. Nothing here is clever, which is the point.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError

from .config import settings

# Library defaults are the RFC 9106 low-memory profile; adequate here and fast
# enough that a seeded install does not take a minute to build.
_hasher = PasswordHasher()

MIN_PASSWORD_LENGTH = 8


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    """Constant-time-ish verify that never raises on a bad hash or a bad guess."""
    try:
        return _hasher.verify(password_hash, password)
    except (VerifyMismatchError, InvalidHashError, ValueError):
        return False


def needs_rehash(password_hash: str) -> bool:
    try:
        return _hasher.check_needs_rehash(password_hash)
    except (InvalidHashError, ValueError):
        return True


def generate_password() -> str:
    """A temporary password for an admin-provisioned account (a judge or
    organizer who never went through self-registration).

    Random rather than admin-typed: the account is emailed its own
    credential, so there is no reason for a human to invent (or read over
    someone's shoulder) a password at all. 24 URL-safe characters is
    comfortably over `MIN_PASSWORD_LENGTH` and, like `new_session_token`,
    generated with `secrets`, not `random`.
    """
    return secrets.token_urlsafe(18)


def new_session_token() -> str:
    """The value that goes in the cookie. 256 bits, URL-safe."""
    return secrets.token_urlsafe(32)


def hash_session_token(token: str) -> str:
    """The value that goes in the database.

    Keyed with `SESSION_SECRET`, so a stolen database dump does not yield usable
    session cookies, and rotating the secret logs everybody out.
    """
    digest = hmac.new(
        settings.session_secret.encode("utf-8"), token.encode("utf-8"), hashlib.sha256
    )
    return digest.hexdigest()


def normalize_email(email: str) -> str:
    """Emails are compared and stored lower-cased; the unique index does the rest."""
    return email.strip().lower()
