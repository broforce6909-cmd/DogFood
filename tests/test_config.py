"""Production secret validation: the one check that has to run before the
server does, not the check a deployer might forget to read about in a README.

No database, no HTTP -- `Settings.model_post_init` is a pure validation step
over plain field values, so it is tested the same way by constructing
`Settings` directly with explicit keyword arguments (which pydantic-settings
takes over the environment) rather than through `app.config.settings`, the
one process-wide instance every other module imports.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1] / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from app.config import DEV_SESSION_SECRET, DEV_SIGNING_PRIVATE_KEY_PEM, Settings  # noqa: E402

REAL_SECRET = "a-real-random-secret-nobody-committed-to-git"
REAL_SIGNING_KEY = (
    "-----BEGIN PRIVATE KEY-----\n"
    "MC4CAQAwBQYDK2VwBCIEINotTheCommittedDevKeyAtAllReallyTrustMe==\n"
    "-----END PRIVATE KEY-----\n"
)


def test_development_defaults_start_cleanly() -> None:
    """`docker compose up` with zero configuration must not refuse to start --
    that is the entire point of committing working dev defaults."""
    Settings(environment="development")  # must not raise


def test_environment_unset_behaves_like_development() -> None:
    Settings()  # the field's own default is "development"


def test_production_with_every_dev_default_refuses_to_start() -> None:
    with pytest.raises(RuntimeError) as exc_info:
        Settings(
            environment="production",
            session_secret=DEV_SESSION_SECRET,
            signing_active_private_key_pem=DEV_SIGNING_PRIVATE_KEY_PEM,
            session_cookie_secure=False,
        )
    message = str(exc_info.value)
    assert "SESSION_SECRET" in message
    assert "SIGNING_ACTIVE_PRIVATE_KEY_PEM" in message
    assert "SESSION_COOKIE_SECURE" in message


def test_production_with_real_values_starts_cleanly() -> None:
    Settings(
        environment="production",
        session_secret=REAL_SECRET,
        signing_active_private_key_pem=REAL_SIGNING_KEY,
        session_cookie_secure=True,
        cors_origins="https://dogfood.example.com",
    )  # must not raise


@pytest.mark.parametrize(
    "field, value",
    [
        ("session_secret", DEV_SESSION_SECRET),
        ("signing_active_private_key_pem", DEV_SIGNING_PRIVATE_KEY_PEM),
    ],
)
def test_production_refuses_on_each_dev_secret_independently(field: str, value: str) -> None:
    """One bad field is enough -- an operator should not need to trip every
    check at once to find out any single one is wrong."""
    kwargs = {
        "environment": "production",
        "session_secret": REAL_SECRET,
        "signing_active_private_key_pem": REAL_SIGNING_KEY,
        "session_cookie_secure": True,
        field: value,
    }
    with pytest.raises(RuntimeError):
        Settings(**kwargs)


def test_production_refuses_an_insecure_session_cookie() -> None:
    with pytest.raises(RuntimeError, match="SESSION_COOKIE_SECURE"):
        Settings(
            environment="production",
            session_secret=REAL_SECRET,
            signing_active_private_key_pem=REAL_SIGNING_KEY,
            session_cookie_secure=False,
        )


def test_production_refuses_a_wildcard_cors_origin() -> None:
    """`allow_credentials=True` with `allow_origins=["*"]` is rejected by every
    browser anyway, but a deployer who set it meant to allow everything and
    got neither what they asked for nor what they need -- worth refusing
    rather than silently no-opping."""
    with pytest.raises(RuntimeError, match="CORS_ORIGINS"):
        Settings(
            environment="production",
            session_secret=REAL_SECRET,
            signing_active_private_key_pem=REAL_SIGNING_KEY,
            session_cookie_secure=True,
            cors_origins="*",
        )


def test_production_accepts_multiple_real_cors_origins() -> None:
    Settings(
        environment="production",
        session_secret=REAL_SECRET,
        signing_active_private_key_pem=REAL_SIGNING_KEY,
        session_cookie_secure=True,
        cors_origins="https://a.example.com, https://b.example.com",
    )  # must not raise
