"""FastAPI application entrypoint.

Feature routers are mounted here as each tier is built, and every one of them
goes through `check_access()` server-side.

Phase 1 (T1 Core) mounts: auth, users, events, teams, submissions, gallery.
Phase 2 (T2 Judging) adds: judges (rubric/judges/assignment/progress), judging
(the judge's own queue and ballots), results, exports.
Phase 3 (T3 Public) adds: voting (ballots and the tally), comments, audit.
Phase 4 (T4 Stretch) adds: webhooks, certificates (with public verification),
imports, and the embeddable gallery widget.
Phase 5 adds: the cross-event audit view and chain verification, signing's
public keys (folded into certificates), and pairwise judging.
Phase 6 adds: per-event registration ahead of team formation, submission
disqualification for cause, staff removal of judges/team members/registrations
(all reasoned and audited), organizer announcements, the winner/
community-vote split (scoping an event's community voting round to the
judge-ranked tier between the outright winners and the rest, gated by a
registration cutoff), admin override of a submission's final tier (with
the judge-computed ranking always shown alongside it, never overwritten)
plus a read-only per-project scoring review grid, an anonymized per-project
judge report for a team's own project, a public results dashboard (every
submitted project, not just winners), and outbound email (registration
confirmation, a results-published notification) over provider-agnostic SMTP
with a console-log fallback.
"""

from __future__ import annotations

import logging
import time

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.exc import DataError, SQLAlchemyError

from . import __version__
from .config import settings
from .db import engine
from .logging_config import clean_request_id, configure_logging, new_request_id, request_id_var
from .routers import (
    announcements,
    audit,
    auth,
    calibration,
    certificates,
    comments,
    embed,
    events,
    exports,
    gallery,
    imports,
    judges,
    judging,
    pairwise,
    registrations,
    results,
    submissions,
    teams,
    users,
    voting,
    webhooks,
)

configure_logging()
log = logging.getLogger("dogfood")

app = FastAPI(
    title="Dogfood Hackathon Portal API",
    description="Submission and judging portal. Every action in the UI is available here.",
    version=__version__,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["X-Request-ID"],
)

# The bulk-import route already rejects an oversized CSV, but only after
# `await request.body()` has already buffered the whole thing -- fine for a
# 5MB cap, but every *other* route has no size check at all, buffered by
# FastAPI's own body parsing before a Pydantic schema (even a length-bounded
# one) gets a chance to reject anything. This runs before any of that: a
# `Content-Length` over the cap is refused without reading a byte of the body.
# 8 MB comfortably covers the importer's own 5 MB files; every other route's
# legitimate payloads are now bounded to a few KB by schemas.py's field limits,
# so this is a backstop against a body nothing legitimate ever sends, not a
# limit anything real is expected to bump into.
MAX_REQUEST_BYTES = 8 * 1024 * 1024


@app.middleware("http")
async def add_request_id(request: Request, call_next):
    """Every request gets an id, correlated across every log line it causes
    and echoed back in `X-Request-ID` so a caller who hits an error can quote
    the exact id an operator would grep for.

    Added first (outermost -- Starlette runs the *last*-registered
    `@app.middleware` first), so the id is set in `request_id_var` before
    `reject_oversized_requests`, `add_security_headers`, or any route handler
    runs, and every log line any of them emit is already correlated.
    """
    request_id = clean_request_id(request.headers.get("x-request-id")) or new_request_id()
    token = request_id_var.set(request_id)
    started = time.perf_counter()
    try:
        response = await call_next(request)
        response.headers["x-request-id"] = request_id
        duration_ms = round((time.perf_counter() - started) * 1000, 2)
        # Logged here, still inside the contextvar's scope, so
        # `RequestIdFilter` picks up this exact request's id -- resetting it
        # in `finally` below happens after this line, not before.
        log.info(
            "%s %s -> %d (%sms)",
            request.method,
            request.url.path,
            response.status_code,
            duration_ms,
        )
        return response
    finally:
        request_id_var.reset(token)


@app.middleware("http")
async def reject_oversized_requests(request: Request, call_next):
    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            too_big = int(content_length) > MAX_REQUEST_BYTES
        except ValueError:
            too_big = False  # let routing/validation handle a malformed header
        if too_big:
            return JSONResponse(
                status_code=413,
                content={"detail": f"Request body exceeds {MAX_REQUEST_BYTES // (1024 * 1024)} MB"},
            )
    return await call_next(request)


# Every response gets these unless the route already set its own -- `setdefault`,
# not an overwrite, so a route with a real reason to differ (there is exactly
# one: `embed.py`) is never fought with. `/embed/*` is excluded outright rather
# than relying on `setdefault` alone: that page's whole feature is being framed
# by someone else's site, and `X-Frame-Options: DENY` would break it even
# though embed.py never sets that header itself to override.
SECURITY_HEADERS = {
    "x-content-type-options": "nosniff",
    "x-frame-options": "DENY",
    "referrer-policy": "strict-origin-when-cross-origin",
    # Harmless to send over plain HTTP in dev -- browsers only act on this over
    # a connection that was already HTTPS, so it costs nothing locally and is
    # exactly the default a real deployment behind TLS needs.
    "strict-transport-security": "max-age=63072000; includeSubDomains",
    # Nothing in this API touches any of these browser capabilities; disabling
    # them by default costs nothing and closes off a class of embedding abuse.
    "permissions-policy": "camera=(), microphone=(), geolocation=(), payment=()",
}

# This API is JSON in, JSON out, with two documented exceptions: the
# interactive docs (which load their own bundle from a CDN and would break
# under a strict CSP) and `/embed/*` (which sets its own, deliberately
# permissive, frame-ancestors policy). Everything else gets locked down --
# there is no legitimate reason for a JSON response to execute a script.
_NO_CSP_PATHS = {"/docs", "/redoc", "/openapi.json"}


@app.middleware("http")
async def add_security_headers(request: Request, call_next):
    response = await call_next(request)
    if request.url.path.startswith("/embed/"):
        return response
    for key, value in SECURITY_HEADERS.items():
        response.headers.setdefault(key, value)
    if request.url.path not in _NO_CSP_PATHS:
        response.headers.setdefault(
            "content-security-policy", "default-src 'none'; frame-ancestors 'none'"
        )
    return response


for feature_router in (
    auth.router,
    users.router,
    events.router,
    teams.router,
    submissions.router,
    gallery.router,
    judges.router,
    judging.router,
    results.router,
    exports.router,
    voting.router,
    comments.router,
    audit.router,
    audit.global_router,
    webhooks.router,
    certificates.router,
    imports.router,
    embed.router,
    pairwise.router,
    registrations.router,
    announcements.router,
    calibration.router,
    calibration.judge_router,
):
    app.include_router(feature_router)


@app.exception_handler(DataError)
async def data_error_handler(request: Request, exc: DataError) -> JSONResponse:
    """A request that Pydantic accepted but Postgres would not.

    A NUL byte (`\\x00`) is valid JSON inside a string and passes every length
    and pattern check schemas.py has, but Postgres refuses it in `text`/
    `varchar` outright -- there is no way to write it, so there was nothing for
    a schema-level validator to strip. Without this handler that surfaces as an
    uncaught `sqlalchemy.exc.DataError`, i.e. a 500, for input that is not a
    server bug, just a byte the client should not have sent. `get_db`'s
    `finally: db.close()` still runs and rolls back the failed transaction;
    this only decides what the caller sees.
    """
    log.info("rejected request with invalid data for Postgres: %s", exc)
    return JSONResponse(
        status_code=422,
        content={
            "detail": "Invalid characters in request (e.g. a NUL byte is not valid in text fields)."
        },
    )


@app.get("/", tags=["meta"])
def root() -> dict[str, str]:
    """Service identity — the cheapest possible liveness probe."""
    return {"service": "dogfood-api", "status": "ok", "version": __version__}


@app.get("/health", tags=["meta"])
def health() -> dict[str, str]:
    """Readiness probe: confirms the API can actually reach Postgres."""
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except SQLAlchemyError as exc:
        log.warning("health check failed: %s", exc)
        return {"api": "ok", "database": "error", "detail": str(exc)}
    return {"api": "ok", "database": "ok", "detail": "all systems nominal"}
