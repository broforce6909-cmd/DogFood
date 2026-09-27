"""Runtime configuration, read from the environment.

Every value has a working local default so the stack comes up with no .env file
present. Compose supplies the real values.
"""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict

DEV_SESSION_SECRET = "dev-secret-change-me-in-production"
DEV_SIGNING_PRIVATE_KEY_PEM = (
    "-----BEGIN PRIVATE KEY-----\n"
    "MC4CAQAwBQYDK2VwBCIEIE8fkC6jbaw8MkD+yC1LRk0UJBc42MYppzgV6iiQYs3H\n"
    "-----END PRIVATE KEY-----\n"
)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # "development" (the default) is what makes `docker compose up` work with
    # no configuration at all -- every dev-default secret below is deliberately
    # committed for exactly that. "production" flips on the one check that
    # matters: that somebody actually changed them. See `model_post_init` below.
    environment: str = "development"

    database_url: str = "postgresql+psycopg://dogfood:dogfood@localhost:5432/dogfood"

    # Session cookies are signed+stored server-side; the secret protects the
    # token, and rotating it invalidates every live session.
    session_secret: str = DEV_SESSION_SECRET
    session_cookie_name: str = "dogfood_session"
    session_ttl_hours: int = 72

    # Certificates are signed with Ed25519, not HMAC -- asymmetric on purpose,
    # so a verifier only ever needs the *public* key (published at
    # GET /api/signing/public-keys) and never the server's own secret. See
    # app/signing.py.
    #
    # `signing_active_kid` names which key below signs new records.
    # `signing_active_private_key_pem` is that key's private half, PKCS8 PEM.
    # This dev keypair is fixed and committed, exactly like `session_secret`
    # above -- a working local install needs a working local default, and the
    # trade is the same one already made and documented for every other secret
    # in this file: change it before this goes anywhere but a laptop.
    #
    # `signing_retired_keys` is where an old key's *public* half goes after
    # rotation, so certificates it signed keep verifying. It is a JSON array of
    # `{"kid": "...", "public_key_pem": "..."}`; empty until a rotation has
    # actually happened. Retired keys are for verification only -- there is no
    # private half here, and there should not be: keeping it around defeats the
    # point of retiring it.
    signing_active_kid: str = "dev-2026-01"
    signing_active_private_key_pem: str = DEV_SIGNING_PRIVATE_KEY_PEM
    signing_retired_keys: str = "[]"

    cors_origins: str = "http://localhost:3000"
    seed_on_start: bool = True

    # `voters.ip_address`/`user_agent` exist for one reason -- abuse
    # investigation, per that column's own docstring in app/models.py -- and an
    # investigation window has an end. THREAT-MODEL.md named "kept indefinitely,
    # no retention policy" as a real gap; this is the retention. See
    # `app/routers/voting.py`'s `_anonymize_stale_voters`.
    voter_ip_retention_days: int = 90

    # Where the browser-facing portal lives. Only used to build invite links,
    # which have to be clickable outside the process that generated them.
    web_base_url: str = "http://localhost:3000"

    # Local development is plain HTTP; a real deployment sets this to true and
    # terminates TLS in front. Documented in README rather than defaulted on,
    # because a Secure cookie over http:// silently never arrives.
    session_cookie_secure: bool = False

    # Outbound email (Phase 6, Part 5): registration confirmation and a
    # results-published notification. Provider-agnostic -- plain SMTP against
    # whatever host this names, MailHog included (point it at MailHog's SMTP
    # port and mail shows up in MailHog's own UI, zero code change). Unset
    # `smtp_host` is not a "disabled" flag to check anywhere: `app/email.py`
    # falls back to logging the message instead of refusing to send it, which
    # is what makes a `docker compose up` with no mail server configured at
    # all -- the default state of this whole project -- still show every
    # email it would have sent, in the api container's own logs.
    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_username: str | None = None
    smtp_password: str | None = None
    smtp_use_tls: bool = True
    email_from: str = "Dogfood Hackathon <noreply@dogfood.example>"

    @property
    def cors_origin_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]

    def model_post_init(self, __context: object) -> None:
        """Refuse to start rather than serve a real event on dev secrets.

        Every default above exists so `docker compose up` works with no
        configuration at all -- that is right for a laptop and wrong for
        anything a stranger's data goes through. Checked once, here, rather
        than trusted to a README a deployer might not read: the dev secrets
        are public (they are committed, in this file, on GitHub), so a
        production instance that starts on any of them is not "insecure until
        someone gets around to it" -- it is already compromised the moment
        it's reachable.
        """
        if self.environment != "production":
            return
        problems = []
        if self.session_secret == DEV_SESSION_SECRET:
            problems.append("SESSION_SECRET is still the committed development default")
        if self.signing_active_private_key_pem == DEV_SIGNING_PRIVATE_KEY_PEM:
            problems.append(
                "SIGNING_ACTIVE_PRIVATE_KEY_PEM is still the committed development key -- "
                "anyone can forge a certificate with it"
            )
        if not self.session_cookie_secure:
            problems.append(
                "SESSION_COOKIE_SECURE is false -- the session cookie will be sent over plain HTTP"
            )
        if "*" in self.cors_origin_list:
            problems.append(
                "CORS_ORIGINS includes '*' -- combined with allow_credentials=True this is "
                "rejected by browsers anyway, but it also means every other origin in the "
                "list is pointless: name the real origin(s) the frontend is served from"
            )
        if problems:
            raise RuntimeError(
                "Refusing to start with ENVIRONMENT=production while:\n  - "
                + "\n  - ".join(problems)
                + "\nSet real values for these, or leave ENVIRONMENT unset (or \"development\") "
                "for a local/judge install, which is what docker-compose.yml does today."
            )


settings = Settings()
