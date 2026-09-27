"""Rate limiting, in Postgres.

`ARCHITECTURE.md` left this open through two phases: in-process is simplest but
wrong across replicas, Postgres-backed survives a restart. Phase 3 picks
**Postgres**, and the deciding argument is what is being limited. The thing on the
other end of these counters is vote and comment abuse, and an in-process limiter
resets when the container restarts -- which means an attacker who can make the app
restart, or who simply waits for a deploy, resets it for us. A counter that
survives is worth more than a counter that is fast.

Fixed window, not sliding. One row per `(key, window)`, so the whole limiter is a
single upsert and a comparison, and expired rows are deletable with one `DELETE`.
A sliding window is more accurate and needs either a row per request or a Lua
script in something this project deliberately does not run. The cost is the
standard fixed-window burst: a caller can spend a full budget at the end of one
window and again at the start of the next. Named in `THREAT-MODEL.md` rather than
hidden, because for vote abuse at hackathon scale it does not matter -- the limits
here exist to stop scripted ballot minting, not to shape traffic.

Limits are deliberately generous. A rate limiter that blocks a real voter is worse
than no rate limiter, because the organizer turns it off.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from fastapi import HTTPException, Request, status
from sqlalchemy import delete, text
from sqlalchemy.orm import Session

from .models import AuditAction, Event, RateLimit, utcnow


@dataclass(frozen=True)
class Limit:
    """`count` requests per `window`, under a name that appears in the audit log."""

    name: str
    count: int
    window: timedelta

    @property
    def retry_seconds(self) -> int:
        return int(self.window.total_seconds())


# The limits that exist, in one table so an organizer can be told what they are.
#
# Claiming a ballot is the one that matters: it is the only endpoint that mints an
# identity, and in open-link mode it is the whole attack surface. Casting votes is
# looser because a voter legitimately revises a ballot several times.
CLAIM_BALLOT = Limit("claim_ballot", count=20, window=timedelta(minutes=10))
CAST_VOTE = Limit("cast_vote", count=60, window=timedelta(minutes=10))
POST_COMMENT = Limit("post_comment", count=15, window=timedelta(minutes=10))
REGISTER = Limit("register", count=10, window=timedelta(hours=1))

# Login gets two limits, keyed differently, because one key alone leaves a gap:
#
# * `LOGIN_IP` stops a script hammering many accounts from one address.
# * `LOGIN_ACCOUNT` stops a distributed attack (many IPs, botnet, VPN rotation)
#   aimed at *one* account -- which `LOGIN_IP` alone would never see, because
#   each attacking IP looks fine on its own.
#
# This closes a gap that shipped all the way from Phase 1: the login route's own
# docstring said rate limiting would land "in Phase 3 with the rest of the
# anti-abuse work," and Phase 3 built the limiter for votes and comments but
# never came back for the one endpoint a hackathon platform's password-guessing
# attacker actually wants.
LOGIN_IP = Limit("login_ip", count=20, window=timedelta(minutes=15))
LOGIN_ACCOUNT = Limit("login_account", count=8, window=timedelta(minutes=15))


def client_key(request: Request, *parts: str) -> str:
    """Build a limiter key from the caller's address plus whatever scopes it.

    The IP is the only identifier an anonymous voter has, and it is both spoofable
    and shared -- a university NAT is one address for a thousand people. That cuts
    both ways and is why the limits are generous: this stops a script, not a
    determined person, and `THREAT-MODEL.md` says so.

    `request.client.host` is used rather than `X-Forwarded-For`, because trusting a
    header the caller controls would make the limiter bypassable by setting it. A
    deployment behind a proxy has to resolve the real address in the proxy.
    """
    host = request.client.host if request.client else "unknown"
    return ":".join([*parts, host])


def window_start(limit: Limit, now: datetime) -> datetime:
    """Floor `now` to the start of its window, so every caller in the same window
    shares a row and the count is comparable."""
    seconds = int(limit.window.total_seconds())
    epoch = int(now.timestamp())
    return datetime.fromtimestamp(epoch - (epoch % seconds), tz=now.tzinfo)


def hit(
    db: Session,
    limit: Limit,
    key: str,
    *,
    now: datetime | None = None,
) -> int | None:
    """Record one request. Returns None if allowed, or seconds to wait if not.

    The upsert is `ON CONFLICT ... DO UPDATE SET count = count + 1 RETURNING count`,
    which is atomic in one statement: two concurrent requests cannot both read 9
    and write 10. Doing this as a SELECT then an UPDATE would be a race, and a
    race in a rate limiter is a bypass.
    """
    now = now or utcnow()
    started = window_start(limit, now)

    count = db.execute(
        text(
            """
            INSERT INTO rate_limits (key, window_started_at, count, updated_at)
            VALUES (:key, :started, 1, now())
            ON CONFLICT (key, window_started_at)
            DO UPDATE SET count = rate_limits.count + 1, updated_at = now()
            RETURNING count
            """
        ),
        {"key": f"{limit.name}:{key}", "started": started},
    ).scalar_one()
    db.commit()

    if count > limit.count:
        elapsed = (now - started).total_seconds()
        return max(1, int(limit.window.total_seconds() - elapsed))
    return None


def _record_trip(
    db: Session,
    limit: Limit,
    tripped_keys: list[str],
    *,
    request: Request | None,
    event: Event | None,
) -> None:
    """Log the refusal, not just return it.

    `Limit`'s own docstring promises a name "that appears in the audit log", and
    until Phase 5 nothing kept that promise -- `AuditAction.RATE_LIMITED` existed
    and was never constructed. Committed immediately (`hit()` already forces a
    commit for the counter row in the same request, so this does not introduce a
    new mid-request commit that was not already happening) so the entry survives
    even though the route is about to end in a raised exception rather than its
    own normal commit.

    Scoped to `event` when the caller has one, so it shows up on that event's own
    `GET /api/events/{slug}/audit` -- the page an organizer investigating abuse on
    *their* event actually opens -- rather than only being reachable through the
    admin-only, cross-event `GET /api/audit`. Login and registration have no event
    to scope to and are recorded unscoped, same as any other platform-level action.
    """
    from .audit import record  # local import: audit.py does not import ratelimit.py

    record(
        db,
        action=AuditAction.RATE_LIMITED,
        summary=f"rate limit {limit.name!r} tripped for {', '.join(tripped_keys)}",
        actor_label=tripped_keys[0],
        event=event,
        request=request,
    )
    db.commit()


def enforce(
    db: Session,
    limit: Limit,
    key: str,
    *,
    now: datetime | None = None,
    request: Request | None = None,
    event: Event | None = None,
) -> None:
    """`hit`, but raises the 429 for you.

    `Retry-After` is set because a client that is told to back off and not told for
    how long will simply retry immediately.
    """
    retry = hit(db, limit, key, now=now)
    if retry is None:
        return
    _record_trip(db, limit, [key], request=request, event=event)
    raise HTTPException(
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        detail=(
            f"Too many requests. The limit is {limit.count} per "
            f"{limit.retry_seconds // 60} minutes. Try again in {retry}s."
        ),
        headers={"Retry-After": str(retry)},
    )


def enforce_dual(
    db: Session,
    limit: Limit,
    *keys: str,
    now: datetime | None = None,
    request: Request | None = None,
    event: Event | None = None,
) -> None:
    """Enforce the same limit under more than one key, all-or-nothing.

    Vote casting and comment posting are keyed by IP alone in the fixture-scale
    deployment this project ships, which leaves two gaps in opposite directions:
    everyone behind one NAT shares a single budget (a real Sybil defence would
    key by IP; a real UX would not), and one authenticated abuser who rotates
    address gets a fresh budget for free. Checking the IP key and the identity
    key together closes both: a NAT full of legitimate voters is still bounded
    by the identity key per person, and an abuser cannot outrun the IP key by
    switching accounts, or the identity key by switching addresses.

    Each key gets its own counter -- this is not one shared budget split two
    ways -- so hitting either one is enough to refuse the request. `hit()` still
    records the attempt against both keys even when it refuses, so a caller who
    retries immediately does not get free tries on the key that had not tripped
    yet.
    """
    now = now or utcnow()
    retries = [hit(db, limit, key, now=now) for key in keys]
    tripped_keys = [key for key, r in zip(keys, retries, strict=True) if r is not None]
    if tripped_keys:
        retry = max(r for r in retries if r is not None)
        _record_trip(db, limit, tripped_keys, request=request, event=event)
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=(
                f"Too many requests. The limit is {limit.count} per "
                f"{limit.retry_seconds // 60} minutes. Try again in {retry}s."
            ),
            headers={"Retry-After": str(retry)},
        )


def purge(db: Session, *, older_than: timedelta = timedelta(days=1)) -> int:
    """Drop windows nobody will read again.

    Not scheduled: there is no job runner in this stack, and adding one for this
    would be a dependency for housekeeping. Called opportunistically from the
    claim route, which is the highest-traffic limited endpoint, so the table stays
    small on any install that is actually being used.
    """
    cutoff = utcnow() - older_than
    result = db.execute(delete(RateLimit).where(RateLimit.window_started_at < cutoff))
    db.commit()
    return result.rowcount or 0
